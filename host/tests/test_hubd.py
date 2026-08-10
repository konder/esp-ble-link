"""hubd 的协议层:指令落到 hub 的哪个方法、事件长什么样、坏输入会不会掀翻循环。

这里**不测 BleHub 的行为**(那是 test_hub 的事),只测「JSON ↔ hub 调用」这一层
翻译对不对。用真 BleHub + FakeLink 而不是假 hub —— 假 hub 会把方法名写死两遍,
真 hub 改了签名这里就该红。
"""
import argparse
import io
import json
import sys

import pytest

import espble.hub as hubmod
from espble import BleHub
from espble.hubd import (apply_command, device_event, message_event,
                         parse_device_args, run, status_event)


class FakeLink:
    """替掉 espble.BleLink。只记录发了什么。"""

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
    monkeypatch.setattr(hubmod, "BleLink", FakeLink)
    h = BleHub(
        app_path=str(tmp_path / "X.app"),
        device_type="m5paper",
        registry_path=str(tmp_path / "devices.json"),
        session_root=str(tmp_path / "sessions"),
    )
    h.register("aaa111", "甲屏")
    yield h
    h.close()


def sent(hub, device_id="aaa111"):
    dl = hub._links[device_id]
    return [json.loads(line) for line, _ in dl.link.queued]


# ---- 指令路由 ----

def test_publish_all_reaches_the_device_and_needs_no_reply(hub):
    assert apply_command(hub, {"op": "publish_all", "obj": {"t": "ev", "msg": "嗨"}}) is None
    assert sent(hub) == [{"t": "ev", "msg": "嗨"}]


def test_set_retained_all_is_replayed_on_reconnect(hub):
    # retained 的意义在重连补推,所以这里验的是"存进去了",不是"立刻发出去了"
    apply_command(hub, {"op": "set_retained_all", "key": "usage", "obj": {"t": "usage", "pct": 60}})
    dl = hub._links["aaa111"]
    dl.link.clear_outbox()
    dl.channel._on_connect(dl.link)
    # 补推走 send_blocking 而不是 send_soon(见 RetainedChannel._on_connect)
    assert [json.loads(l) for l in dl.link.blocking] == [{"t": "usage", "pct": 60}]


def test_send_targets_one_device_by_alias(hub):
    hub.register("bbb222", "乙屏")
    assert apply_command(hub, {"op": "send", "target": "甲屏", "obj": {"t": "cmd", "cmd": "ota"}}) is None
    assert sent(hub, "aaa111") == [{"t": "cmd", "cmd": "ota"}]
    assert sent(hub, "bbb222") == []


def test_send_to_an_unknown_device_is_reported_not_swallowed(hub):
    # 静默失败会被误诊成"BLE 没送到",而这其实是消费方的配置写错了
    ev = apply_command(hub, {"op": "send", "target": "不存在", "obj": {"t": "cmd"}})
    assert ev["event"] == "error" and "不存在" in ev["message"]


def test_broadcast_reaches_every_device(hub):
    hub.register("bbb222", "乙屏")
    apply_command(hub, {"op": "broadcast", "obj": {"t": "cmd", "cmd": "ota"}})
    assert sent(hub, "aaa111") and sent(hub, "bbb222")


def test_register_is_idempotent_and_answers_with_status(hub):
    ev = apply_command(hub, {"op": "register", "id": "aaa111", "alias": "甲屏"})
    assert ev["event"] == "status"
    assert list(ev["devices"]) == ["aaa111"]


def test_unregister_removes_the_device(hub):
    assert apply_command(hub, {"op": "unregister", "target": "甲屏"})["devices"] == {}


# ---- 坏输入 ----

@pytest.mark.parametrize("cmd", [
    {},                                             # 没 op
    {"op": "没这个命令"},
    {"op": "register"},                             # 缺 id
    {"op": "send", "obj": {"a": 1}},                # 缺 target
    {"op": "publish_all"},                          # 缺 obj
    {"op": "publish_all", "obj": "不是对象"},
    {"op": "set_retained_all", "obj": {"a": 1}},    # 缺 key
])
def test_malformed_commands_come_back_as_errors_not_exceptions(hub, cmd):
    ev = apply_command(hub, cmd)
    assert ev["event"] == "error" and ev["message"]


# ---- 事件形状 ----

def test_message_event_carries_both_id_and_label(hub):
    rec = hub.registry.get("aaa111")
    ev = message_event(rec, '{"pct":100}')
    # 只有 id 的话多设备日志读不了,只有 label 的话消费方没法路由 —— 两个都要
    assert ev == {"event": "message", "device": "aaa111",
                  "label": "甲屏", "line": '{"pct":100}'}


def test_device_event_reports_live_connectivity(hub):
    rec = hub.registry.get("aaa111")
    assert device_event(hub, rec, "connected")["connected"] is True
    hub._links["aaa111"].link.connected = False
    assert device_event(hub, rec, "disconnected")["connected"] is False


def test_status_event_exposes_identity_and_link_state(hub):
    devices = status_event(hub)["devices"]
    assert devices["aaa111"]["alias"] == "甲屏"
    assert devices["aaa111"]["connected"] is True


@pytest.mark.parametrize("items, want", [
    (["c119cc:看板"], [("c119cc", "看板")]),
    (["c119cc"], [("c119cc", "")]),
    (["a:甲", "b:乙"], [("a", "甲"), ("b", "乙")]),
    ([" c119cc : 看板 "], [("c119cc", "看板")]),
    ([""], []),
    (None, []),
])
def test_parse_device_args(items, want):
    assert parse_device_args(items) == want


# ---- 主循环 ----

def _args(tmp_path, **kw):
    d = dict(app=str(tmp_path / "X.app"), device=[], device_type="m5paper",
             registry=str(tmp_path / "devices.json"),
             session_root=str(tmp_path / "sessions"),
             history_n=8, keepalive_sec=30.0, reconnect_sec=5.0,
             backoff_max_sec=45.0, scan_timeout=20.0)
    d.update(kw)
    return argparse.Namespace(**d)


def _drive(monkeypatch, tmp_path, stdin_text, **kw):
    """把 stdin/stdout 换成内存管道跑一遍 run(),返回它吐出的事件列表。"""
    monkeypatch.setattr(hubmod, "BleLink", FakeLink)
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
    monkeypatch.setattr(sys, "stdout", out)
    assert run(_args(tmp_path, **kw)) == 0
    return [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]


def test_run_announces_ready_with_the_initial_device_table(monkeypatch, tmp_path):
    events = _drive(monkeypatch, tmp_path, "", device=["aaa111:甲屏"])
    ready = [e for e in events if e["event"] == "ready"]
    assert len(ready) == 1 and "aaa111" in ready[0]["devices"]


def test_run_keeps_going_after_a_bad_line(monkeypatch, tmp_path):
    # 一行烂输入就断链路是不可接受的 —— 后面那条 status 必须照样有回应
    events = _drive(monkeypatch, tmp_path, '这不是 JSON\n[1,2,3]\n\n{"op":"status"}\n',
                    device=["aaa111:甲屏"])
    kinds = [e["event"] for e in events]
    assert kinds.count("error") == 2          # 坏 JSON + 不是对象;空行被忽略
    assert kinds[-1] == "status"


def test_run_exits_cleanly_on_eof_and_takes_the_links_down(monkeypatch, tmp_path):
    # 消费方走了 hubd 就该收摊,否则留一堆连着设备的孤儿进程
    monkeypatch.setattr(hubmod, "BleLink", FakeLink)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    args = _args(tmp_path, device=["aaa111:甲屏"])
    links = []
    real_init = FakeLink.__init__

    def spy(self, device, **kw):
        real_init(self, device, **kw)
        links.append(self)
    monkeypatch.setattr(FakeLink, "__init__", spy)
    assert run(args) == 0
    assert links and all(l.closed for l in links)


def test_device_changes_are_pushed_without_being_asked(monkeypatch, tmp_path):
    # 消费方读 connected 必须是本地缓存(它在主循环里),所以状态得主动推
    events = _drive(monkeypatch, tmp_path, "", device=["aaa111:甲屏"])
    assert any(e["event"] == "device" and e["what"] == "registered" for e in events)
    assert any(e["event"] == "status" for e in events)


def test_cli_wires_the_hubd_subcommand(monkeypatch, tmp_path):
    import espble.cli as cli
    seen = {}
    monkeypatch.setattr(cli.hubd_mod, "run", lambda args: seen.update(vars(args)) or 0)
    assert cli.main(["hubd", "--app", "X.app", "--device", "c119cc:看板",
                     "--device-type", "m5paper"]) == 0
    assert seen["device"] == ["c119cc:看板"] and seen["device_type"] == "m5paper"
