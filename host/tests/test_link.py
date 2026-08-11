"""监护循环:什么时候该丢掉 session 重开。

这一层以前一个单测都没有,而**一个真事故正好住在这里** —— `kill -9` 掉 helper
之后 3.5 分钟零恢复:`connected` 只看事件流,崩掉的进程写不出 `disconnected`,
于是「没连上就重开」那条判据永远不成立;而 keepalive 失败那行日志写着
「触发重连」,代码却什么都没做。三条判据(进程消失 / 发送失败 / keepalive 失败)
各来一个用例,别再让它们只靠日志文案兜着。

不起真进程:要验的是**决策**,不是 CoreBluetooth。
"""
import threading
import time

import pytest

import espble.link as linkmod
from espble.helper_session import DeviceConfig


class FakeSession:
    """扮演 HelperSession。默认一切正常,按需让某一条判据坏掉。"""

    created = []

    def __init__(self, device, **kw):
        self.device = device
        self.kw = kw
        self._connected = False
        self.stopped = False
        self.sent = []
        self.notifications = __import__("collections").deque()
        self.fatal = False
        self.last_error = ""
        self.vanished = False          # 模拟进程被 kill -9(事件流里什么都没有)
        self.send_ok = True
        FakeSession.created.append(self)

    # ---- 生命周期 ----
    def start(self):
        self._connected = True
        return True

    def stop(self):
        self.stopped = True
        self._connected = False

    @property
    def connected(self):
        return self._connected

    # ---- 监护方问的三件事 ----
    def check_process_alive(self, now=None):
        if self.vanished:
            self.last_error = "helper 进程消失了(崩溃或被杀)"
            self._connected = False    # 真实实现里是把 _dead 置位
            return False
        return True

    def send_line(self, line):
        if not self.send_ok:
            self.last_error = "等 ack 超时"
            return False
        self.sent.append(line)
        return True

    def poll(self):
        return []


@pytest.fixture
def link_factory(tmp_path, monkeypatch):
    FakeSession.created = []
    monkeypatch.setattr(linkmod, "HelperSession", FakeSession)
    made = []

    def make(**kw):
        device = DeviceConfig(app_path=str(tmp_path / "F.app"),
                              session_dir=str(tmp_path / "s"),
                              device_name="Dev")
        lk = linkmod.BleLink(device, reconnect_sec=0.05, backoff_max_sec=0.05, **kw)
        made.append(lk)
        assert lk.wait_connected(3.0), "夹具本身就没连上"
        return lk

    yield make
    for lk in made:
        lk.close()


def wait_for(pred, timeout=4.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_a_vanished_helper_is_respawned(link_factory):
    """这条就是那个事故:进程被 kill -9,事件流里一个字都没有。"""
    lk = link_factory()
    first = FakeSession.created[-1]
    first.vanished = True
    assert wait_for(lambda: len(FakeSession.created) > 1), "helper 消失后没有重开 —— 永久失联"
    assert first.stopped
    assert lk.wait_connected(3.0)


def test_a_failed_keepalive_actually_reconnects(link_factory):
    """keepalive 是唯一能探出「helper 活着但链路死了」的手段,探出来了必须动。"""
    lk = link_factory(keepalive_sec=0.05, keepalive_provider=lambda: '{"t":"ka"}')
    first = FakeSession.created[-1]
    first.send_ok = False
    assert wait_for(lambda: len(FakeSession.created) > 1), "keepalive 失败后没重开(日志在撒谎)"
    assert first.stopped


def test_a_failed_send_actually_reconnects(link_factory):
    lk = link_factory()
    first = FakeSession.created[-1]
    first.send_ok = False
    lk.send_soon('{"hi":1}')
    assert wait_for(lambda: len(FakeSession.created) > 1), "发送失败后没重开"
    assert first.stopped


def test_disconnect_callback_fires_once_per_drop(link_factory):
    drops = []
    lk = link_factory(on_disconnect=lambda: drops.append(1))
    FakeSession.created[-1].vanished = True
    assert wait_for(lambda: drops)
    # 掉一次就报一次,不该因为重开循环里多调了 _drop_session 而重复报
    assert len(drops) == 1


def test_fatal_error_stops_retrying(tmp_path, monkeypatch):
    """蓝牙没授权之类:再试一万次也一样,要停下来等人 —— 不能变成无限重开。"""
    FakeSession.created = []

    class FatalSession(FakeSession):
        def start(self):
            self.fatal = True
            self.last_error = "蓝牙未授权"
            return False

    monkeypatch.setattr(linkmod, "HelperSession", FatalSession)
    device = DeviceConfig(app_path=str(tmp_path / "F.app"),
                          session_dir=str(tmp_path / "s"), device_name="Dev")
    lk = linkmod.BleLink(device, reconnect_sec=0.05)
    assert wait_for(lambda: lk.fatal_error == "蓝牙未授权")
    n = len(FakeSession.created)
    time.sleep(0.5)
    assert len(FakeSession.created) == n, "致命错误之后还在重试"
    lk.close()
