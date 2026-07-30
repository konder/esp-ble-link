"""RetainedChannel 的语义单测 + BleLink 的退避策略。

用一个假 link 代替真链路:我们要验的是「重连后补推什么、按什么顺序」,
和 CoreBluetooth 无关。
"""
import json

import pytest

from espble.channel import RetainedChannel
from espble.link import backoff_delay


class FakeLink:
    """只记录被发了什么,并能模拟「断连时即时消息丢弃」。"""

    def __init__(self, connected=True):
        self.connected = connected
        self.queued = []      # send_soon
        self.blocking = []    # send_blocking(补推走这条)
        self.cleared = 0
        self.started = False
        self.on_connect = None
        self.keepalive_provider = None

    def start(self):
        self.started = True

    def send_soon(self, line, *, queue_while_offline=False):
        if not queue_while_offline and not self.connected:
            return False
        self.queued.append(line)
        return True

    def send_blocking(self, line):
        self.blocking.append(line)
        return self.connected

    def clear_outbox(self):
        self.cleared += 1
        self.queued.clear()

    def close(self):
        pass


@pytest.fixture
def link():
    return FakeLink()


@pytest.fixture
def channel(link):
    return RetainedChannel(link, history_n=3)


def test_channel_wires_itself_into_the_link(link, channel):
    assert link.on_connect == channel._on_connect
    assert link.keepalive_provider == channel._keepalive_line


def test_link_started_only_after_callbacks_are_wired(link):
    """顺序要紧:先接钩子再启动。反过来的话链路可能在 on_connect 挂上之前
    就连上了,首次连接的 retained/history 补推就漏了。"""
    order = []
    link.start = lambda: order.append("start")

    class Probe(RetainedChannel):
        def _on_connect(self, lnk):
            order.append("on_connect")

    probe = Probe(link)
    assert order == ["start"]                    # 构造时就启动了
    assert link.on_connect == probe._on_connect  # 而且钩子已经在位


def test_retained_is_pushed_immediately(link, channel):
    channel.set_retained("state", {"t": "state", "rev": 1})
    assert json.loads(link.queued[0]) == {"t": "state", "rev": 1}


def test_reconnect_replays_retained_then_history(link, channel):
    channel.set_retained("state", {"t": "state", "rev": 3})
    channel.publish({"t": "ev", "n": 1})
    channel.publish({"t": "ev", "n": 2})

    link.blocking.clear()
    channel._on_connect(link)

    sent = [json.loads(x) for x in link.blocking]
    # 顺序要紧:设备通常先画状态再画列表,反过来会闪一下空状态
    assert sent[0]["t"] == "state"
    assert [x["n"] for x in sent[1:]] == [1, 2]      # 历史按旧→新


def test_replayed_events_are_marked_not_live(link, channel):
    channel.publish({"t": "ev", "n": 1})
    link.blocking.clear()
    channel._on_connect(link)
    replayed = json.loads(link.blocking[0])
    # 设备据此只把它放进列表,不再蜂鸣/弹全屏卡一次
    assert replayed["live"] is False


def test_replay_does_not_mutate_stored_history(link, channel):
    original = {"t": "ev", "n": 1}
    channel.publish(original)
    channel._on_connect(link)
    channel._on_connect(link)
    assert "live" not in original
    assert all("live" not in obj for obj in channel._history)


def test_outbox_cleared_before_replay(link, channel):
    channel.set_retained("state", {"t": "state", "rev": 1})
    channel._on_connect(link)
    # 断连期间攒的即时消息内容已被 retained/history 覆盖,不清会重复
    assert link.cleared == 1


def test_history_is_bounded(link, channel):
    for i in range(10):
        channel.publish({"t": "ev", "n": i})
    link.blocking.clear()
    channel._on_connect(link)
    replayed = [json.loads(x) for x in link.blocking]
    assert [x["n"] for x in replayed] == [7, 8, 9]     # history_n=3


def test_replay_stops_when_link_dies_midway(link, channel):
    channel.set_retained("state", {"t": "state"})
    for i in range(3):
        channel.publish({"t": "ev", "n": i})
    link.connected = False       # send_blocking 从此返回 False
    link.blocking.clear()
    channel._on_connect(link)
    # retained 那一条会尝试(失败),历史第一条失败后就该收手,不该硬发完
    assert len(link.blocking) == 2


def test_immediate_messages_dropped_while_offline(link, channel):
    link.connected = False
    channel.publish({"t": "ev", "n": 1})
    assert link.queued == []
    # 但它仍进了历史,重连时补得回来 —— 这正是不排队的底气
    assert len(channel._history) == 1


def test_send_now_can_opt_into_queueing(link, channel):
    link.connected = False
    channel.send_now({"t": "cmd", "cmd": "ota"}, queue_while_offline=True)
    assert len(link.queued) == 1          # 指令值得等
    assert len(channel._history) == 0     # 但不进历史


def test_keepalive_uses_retained_frame(link, channel):
    assert channel._keepalive_line() is None       # 还没有 retained
    channel.set_retained("state", {"t": "state", "rev": 5})
    assert json.loads(channel._keepalive_line())["rev"] == 5


def test_keepalive_key_selects_among_multiple_retained(link):
    ch = RetainedChannel(link, keepalive_key="usage")
    ch.set_retained("state", {"t": "state"})
    ch.set_retained("usage", {"t": "usage", "rev": 9})
    assert json.loads(ch._keepalive_line())["t"] == "usage"


def test_long_message_is_fitted(link):
    ch = RetainedChannel(link, line_limit=200)
    ch.publish({"t": "ev", "msg": "中" * 400})
    assert len(link.queued[0].encode("utf-8")) <= 200


# ---- 退避 ----

def test_backoff_is_two_tier_not_exponential():
    fast = [backoff_delay(n, 5.0, 5, 45.0) for n in (1, 2, 3, 4)]
    slow = [backoff_delay(n, 5.0, 5, 45.0) for n in (5, 6, 50)]
    assert fast == [5.0] * 4
    # 设备要么在、要么不在。指数退避唯一的作用是错过它回来的那一刻。
    assert slow == [45.0] * 3
