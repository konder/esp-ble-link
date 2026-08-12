"""BleHub 的路由、投递语义与注册机制。

这里不起真进程 —— 要验的是「消息去了哪台设备、离线时是丢还是排队、hello 有没有
被框架吃掉」,这些和 CoreBluetooth 无关。真链路由 test_mailbox / 真机验证覆盖。
"""
import json

import pytest

import espble.hub as hubmod
from espble import BleHub


class FakeLink:
    """替掉 espble.BleLink。记录发了什么,并能模拟离线丢弃。"""

    instances = []

    def __init__(self, device, **kw):
        self.device = device
        self.kw = kw
        self.connected = True
        self.queued = []          # (line, queue_while_offline)
        self.blocking = []
        self.started = False
        self.closed = False
        self.on_connect = None
        self.keepalive_provider = None
        self.fatal_error = ""
        FakeLink.instances.append(self)

    def start(self):
        self.started = True

    def send_soon(self, line, *, queue_while_offline=False):
        if not queue_while_offline and not self.connected:
            return False
        self.queued.append((line, queue_while_offline))
        return True

    def send_blocking(self, line):
        self.blocking.append(line)
        return self.connected

    def clear_outbox(self):
        self.queued.clear()

    def close(self):
        self.closed = True


@pytest.fixture
def hub(tmp_path, monkeypatch):
    FakeLink.instances = []
    monkeypatch.setattr(hubmod, "BleLink", FakeLink)
    h = BleHub(
        app_path=str(tmp_path / "X.app"),
        device_type="m5paper",
        registry_path=str(tmp_path / "devices.json"),
        session_root=str(tmp_path / "sessions"),
    )
    yield h
    h.close()


def sent(dl):
    return [json.loads(l) for l, _ in dl.link.queued]


# ---- 一个 bundle,多个进程 ----

def test_one_bundle_many_devices(hub):
    a = hub.register("aaa111", alias="客厅")
    b = hub.register("bbb222", alias="书房")
    # 同一个 .app —— 一次 TCC 授权就够
    assert a.link.device.app_path == b.link.device.app_path


def test_session_dirs_must_differ(hub):
    """这是多实例互不干扰的关键:HelperSession._kill() 按 session-dir 匹配进程,
    分错了就会把兄弟设备的 helper 一起杀掉。"""
    a = hub.register("aaa111")
    b = hub.register("bbb222")
    assert a.link.device.session_dir != b.link.device.session_dir
    assert a.record.device_id in a.link.device.session_dir


def test_device_name_derived_from_type_and_id(hub):
    """固件按 `<type>-<id>` 广播,中枢照同样规则算回去 —— 不用额外配设备名。"""
    a = hub.register("c119cc")
    assert a.link.device.device_name == "m5paper-c119cc"


def test_register_starts_the_link(hub):
    a = hub.register("aaa111")
    assert a.link.started


def test_register_is_idempotent(hub):
    a = hub.register("aaa111", alias="客厅")
    b = hub.register("aaa111")
    assert a is b
    assert len(FakeLink.instances) == 1        # 没有重复起进程


# ---- 路由 ----

def test_send_routes_by_alias_and_by_id(hub):
    a = hub.register("aaa111", alias="客厅")
    b = hub.register("bbb222", alias="书房")

    assert hub.send("客厅", {"t": "usage", "n": 1})
    assert hub.send("bbb222", {"t": "usage", "n": 2})

    assert [x["n"] for x in sent(a)] == [1]
    assert [x["n"] for x in sent(b)] == [2]     # 没串台


def test_send_to_unknown_target_returns_false(hub):
    hub.register("aaa111")
    assert hub.send("不存在", {"t": "x"}) is False


def test_id_wins_over_alias(hub):
    """别名是可变标签,不该盖过身份 —— 万一有人把 A 的别名起成 B 的 id。"""
    a = hub.register("aaa111")
    hub.register("bbb222", alias="aaa111")
    assert hub._resolve("aaa111") is a


# ---- 广播 ----

def test_broadcast_hits_everyone(hub):
    a = hub.register("aaa111")
    b = hub.register("bbb222")
    ids = hub.broadcast({"t": "cmd", "cmd": "ota"})
    assert set(ids) == {"aaa111", "bbb222"}
    assert sent(a) == sent(b) == [{"t": "cmd", "cmd": "ota"}]


def test_broadcast_filters_by_cap(hub):
    a = hub.register("aaa111")
    b = hub.register("bbb222")
    hub.registry.absorb_hello({"id": "aaa111", "type": "m5paper", "caps": "usage,cmd"})
    hub.registry.absorb_hello({"id": "bbb222", "type": "m5paper", "caps": "usage"})

    ids = hub.broadcast({"t": "cmd", "cmd": "ota"}, cap="cmd")
    assert ids == ["aaa111"]
    assert sent(b) == []


def test_broadcast_includes_devices_that_never_said_hello(hub):
    """没上报 caps 就当它支持 —— 宁可多发一条它会忽略的,也不要静默漏发。"""
    a = hub.register("aaa111")
    assert hub.broadcast({"t": "cmd"}, cap="随便什么") == ["aaa111"]


def test_broadcast_filters_by_type(hub):
    a = hub.register("aaa111")
    b = hub.register("bbb222", device_type="cardputer")
    assert hub.broadcast({"t": "x"}, device_type="cardputer") == ["bbb222"]


# ---- 投递语义:单点与广播分开配 ----

def test_default_unicast_drops_offline_broadcast_queues(hub):
    """默认值的取舍:单点多是状态帧(过期即无用,重连有 retained 补),
    广播多是指令(值得等)。"""
    a = hub.register("aaa111")
    a.link.connected = False

    hub.send("aaa111", {"t": "usage"})            # 单点 → 丢
    assert a.link.queued == []
    assert len(a.channel._pending) == 0

    hub.broadcast({"t": "cmd", "cmd": "ota"})     # 广播 → 排队
    # ⚠️ 排在 **channel 层**,不在 link 的 outbox。这条以前断言的是 outbox ——
    #    而 _on_connect 会 clear_outbox(),指令一连上就被清掉。断言错了层,
    #    所以用例一直绿着、功能一直坏着。
    assert a.link.queued == []
    assert len(a.channel._pending) == 1

    # 而且必须真的在重连后送出去 —— 这才是「值得等」的意思
    a.link.connected = True
    a.channel._on_connect(a.link)
    assert any(json.loads(x) == {"t": "cmd", "cmd": "ota"} for x in a.link.blocking)


def test_policy_is_configurable(tmp_path, monkeypatch):
    FakeLink.instances = []
    monkeypatch.setattr(hubmod, "BleLink", FakeLink)
    h = BleHub(app_path=str(tmp_path / "X.app"), device_type="m5paper",
               registry_path=str(tmp_path / "d.json"),
               session_root=str(tmp_path / "s"),
               unicast_queue_offline=True, broadcast_queue_offline=False)
    a = h.register("aaa111")
    a.link.connected = False
    h.send("aaa111", {"t": "usage"})
    assert len(a.channel._pending) == 1           # 反过来了(排队的都在 channel 层)
    a.channel._pending.clear()
    h.broadcast({"t": "cmd"})
    assert len(a.channel._pending) == 0
    assert a.link.queued == []
    h.close()


def test_per_call_override_beats_default(hub):
    a = hub.register("aaa111")
    a.link.connected = False
    hub.send("aaa111", {"t": "usage"}, queue_offline=True)
    assert len(a.channel._pending) == 1


# ---- hello / 注册 ----

def test_hello_is_consumed_by_framework_not_forwarded(hub):
    """hello 是框架自己的协议。应用层不该看到它 —— 这是「框架只管传输」原则里
    刻意的例外,所以更要把边界钉死。"""
    seen = []
    hub.on_message = lambda rec, line: seen.append((rec.device_id, line))
    hub.register("aaa111")

    hub._on_line("aaa111", '{"t":"hello","id":"aaa111","type":"m5paper","fw":40,"caps":"usage,cmd"}')
    assert seen == []                                    # 没往上转发
    rec = hub.registry.get("aaa111")
    assert rec.fw == 40 and rec.caps == ["usage", "cmd"]  # 但注册表更新了


def test_non_hello_lines_go_to_the_app_untouched(hub):
    seen = []
    hub.on_message = lambda rec, line: seen.append((rec.label, line))
    hub.register("aaa111", alias="客厅")
    hub._on_line("aaa111", '{"t":"stats","fw":40}')
    assert seen == [("客厅", '{"t":"stats","fw":40}')]


def test_malformed_hello_does_not_crash(hub):
    hub.register("aaa111")
    hub._on_line("aaa111", '{"t":"hello", 这不是合法 JSON')
    assert hub.registry.get("aaa111").fw == 0


# ---- 注册表落盘 ----

def test_registry_survives_restart(tmp_path, monkeypatch):
    FakeLink.instances = []
    monkeypatch.setattr(hubmod, "BleLink", FakeLink)
    path, root = str(tmp_path / "d.json"), str(tmp_path / "s")
    h1 = BleHub(app_path=str(tmp_path / "X.app"), device_type="m5paper",
                registry_path=path, session_root=root)
    h1.register("aaa111", alias="客厅")
    h1.registry.absorb_hello({"id": "aaa111", "type": "m5paper", "fw": 40, "caps": "usage"})
    h1.close()

    h2 = BleHub(app_path=str(tmp_path / "X.app"), device_type="m5paper",
                registry_path=path, session_root=root)
    rec = h2.registry.get("客厅")
    assert rec is not None and rec.device_id == "aaa111" and rec.fw == 40
    h2.close()


def test_alias_binds_to_id_and_rejects_duplicates(hub):
    hub.register("aaa111")
    hub.register("bbb222")
    hub.alias("aaa111", "客厅")
    with pytest.raises(ValueError):
        hub.alias("bbb222", "客厅")       # 重名会让路由无法确定


def test_adopt_registry_rehydrates_after_restart(tmp_path, monkeypatch):
    """hub 进程重启后必须能把注册表里的设备重新接管起来。

    这是真实故障场景:launchd 拉起 / 改配置 / 崩溃恢复都会换进程。
    没有 adopt_registry 的话现象很隐蔽 —— 注册表里有设备、菜单栏也列得出来,
    但一个 worker 都没在跑、一台都连不上。
    """
    FakeLink.instances = []
    monkeypatch.setattr(hubmod, "BleLink", FakeLink)
    path, root = str(tmp_path / "d.json"), str(tmp_path / "s")
    h1 = BleHub(app_path=str(tmp_path / "X.app"), device_type="m5paper",
                registry_path=path, session_root=root)
    h1.register("aaa111", alias="客厅")
    h1.register("bbb222")
    h1.close()

    FakeLink.instances = []
    h2 = BleHub(app_path=str(tmp_path / "X.app"), device_type="m5paper",
                registry_path=path, session_root=root)
    assert h2.status() == {}                          # 刚起来时确实什么都没接管
    assert set(h2.adopt_registry()) == {"aaa111", "bbb222"}
    assert set(h2.status()) == {"aaa111", "bbb222"}
    assert len(FakeLink.instances) == 2               # 每台一个链路
    assert all(l.started for l in FakeLink.instances)
    # alias 和 type 得跟着回来,否则路由和广播名都会错
    assert h2.registry.get("客厅").device_id == "aaa111"
    assert all(l.device.device_name.startswith("m5paper-") for l in FakeLink.instances)
    h2.close()


def test_adopt_registry_is_idempotent(hub):
    hub.register("aaa111")
    n = len(FakeLink.instances)
    assert hub.adopt_registry() == []                 # 已经在管的不重复接管
    assert len(FakeLink.instances) == n               # 更不会重复起进程


# ---- 全体 fan-out ----

def test_set_retained_all_hits_everyone(hub):
    a = hub.register("aaa111")
    b = hub.register("bbb222")
    assert set(hub.set_retained_all("usage", {"t": "usage", "rev": 7})) == {"aaa111", "bbb222"}
    # retained 的语义是「(重)连上补推」,所以要在重连点上验。
    # 补推走 send_blocking(见 _on_connect),不是 send_soon —— 所以看 blocking 不是 queued。
    for dl in (a, b):
        dl.link.blocking.clear()
        dl.channel._on_connect(dl.link)
        assert {"t": "usage", "rev": 7} in [json.loads(l) for l in dl.link.blocking]


def test_publish_all_enters_every_history_ring(hub):
    """publish_all 必须进历史环 —— 这正是它不能用 broadcast 代替的原因。

    broadcast 走 channel.send_now,只做「现在发出去」;设备离线期间错过的事件
    不会在重连后补上。事件要的恰恰是补上。
    """
    a = hub.register("aaa111")
    b = hub.register("bbb222")
    assert set(hub.publish_all({"t": "ev", "msg": "done"})) == {"aaa111", "bbb222"}
    for dl in (a, b):
        dl.link.blocking.clear()
        dl.channel._on_connect(dl.link)               # 模拟重连
        replayed = [json.loads(l) for l in dl.link.blocking]
        assert any(r.get("msg") == "done" and r.get("live") is False for r in replayed), \
            "重连后该以 live=false 重放,broadcast 做不到"


def test_status_reports_每台(hub):
    hub.register("aaa111", alias="客厅")
    hub.register("bbb222")
    st = hub.status()
    assert set(st) == {"aaa111", "bbb222"}
    assert st["aaa111"]["alias"] == "客厅"
    assert st["aaa111"]["name"] == "m5paper-aaa111"
