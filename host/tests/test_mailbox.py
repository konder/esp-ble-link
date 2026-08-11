"""文件邮箱层的单测。

这里不启真的 helper 进程 —— 我们直接扮演 helper:往 events.jsonl 追加事件、
从 commands/ 取命令。要验的是**协议本身**(原子写、字节偏移 tail、半行处理、
ack 匹配),这些和 CoreBluetooth 一点关系都没有,不该依赖硬件才能测。
"""
import json
import os
import threading
import time

import pytest

from espble.helper_session import DeviceConfig, HelperSession


@pytest.fixture
def session(tmp_path):
    device = DeviceConfig(
        app_path=str(tmp_path / "Fake.app"),
        session_dir=str(tmp_path / "session"),
        device_name="FakeDevice",
    )
    s = HelperSession(device, connect_timeout=1.0, command_timeout=1.0)
    os.makedirs(os.path.join(s.session_dir, "commands"), exist_ok=True)
    open(os.path.join(s.session_dir, "events.jsonl"), "a").close()
    return s


def pretend_helper_is_running(session):
    """没有真进程时把存活检查短路掉。

    send_line 每轮都查 alive() —— 那是为了在 helper 崩了时立刻放弃而不是干等
    超时。这里要测的是邮箱协议本身,进程监护另有其人(_pids 靠 ps 输出,
    没法在单测里造)。
    """
    session.alive = lambda: True


def emit(session, **payload):
    """扮演 helper 追加一条事件。"""
    with open(os.path.join(session.session_dir, "events.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


# ---- DeviceConfig ----

def test_helper_name_and_executable(tmp_path):
    d = DeviceConfig(app_path=str(tmp_path / "MyBLEHelper.app"), session_dir="/tmp/s")
    assert d.helper_name == "MyBLEHelper"
    assert d.executable.endswith("MyBLEHelper.app/Contents/MacOS/MyBLEHelper")


def test_helper_args_only_include_what_was_set(tmp_path):
    d = DeviceConfig(app_path=str(tmp_path / "H.app"), session_dir="/tmp/s",
                     device_name="Dev", scan_timeout=7.5)
    args = d.helper_args()
    assert "--device-name" in args and args[args.index("--device-name") + 1] == "Dev"
    assert "--name-prefix" not in args and "--device-id" not in args
    assert "--accept-unterminated" not in args
    assert args[args.index("--scan-timeout") + 1] == "7.5"


def test_helper_args_accept_unterminated_is_a_bare_flag(tmp_path):
    d = DeviceConfig(app_path=str(tmp_path / "H.app"), session_dir="/tmp/s",
                     name_prefix="Foo", accept_unterminated=True)
    assert "--accept-unterminated" in d.helper_args()


# ---- 命令原子写 ----

def test_command_written_atomically(session):
    seq = session._write_command("write_json", "hello")
    cmd_dir = os.path.join(session.session_dir, "commands")
    files = os.listdir(cmd_dir)
    # 只能看到 .json,不能有 .tmp 残留 —— helper 绝不该读到半个文件
    assert files == [f"{seq:08d}.json"]
    with open(os.path.join(cmd_dir, files[0]), encoding="utf-8") as fh:
        assert json.load(fh) == {"seq": seq, "op": "write_json", "line": "hello"}


def test_command_seq_increments_and_sorts_lexically(session):
    seqs = [session._write_command("write_json", str(i)) for i in range(3)]
    assert seqs == [1, 2, 3]
    names = sorted(os.listdir(os.path.join(session.session_dir, "commands")))
    # 补零到 8 位:helper 靠文件名字典序保证 FIFO,不补零 "10" 会排在 "2" 前面
    assert names == ["00000001.json", "00000002.json", "00000003.json"]


# ---- events.jsonl tail ----

def test_poll_returns_only_new_events(session):
    emit(session, event="launch", pid=1)
    assert [e["event"] for e in session.poll()] == ["launch"]
    assert session.poll() == []
    emit(session, event="scan_started")
    assert [e["event"] for e in session.poll()] == ["scan_started"]


def test_poll_defers_partial_last_line(session):
    path = os.path.join(session.session_dir, "events.jsonl")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"event":"scan_started"}\n{"event":"conn')   # 后半行还没写完
    assert [e["event"] for e in session.poll()] == ["scan_started"]
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('ected","name":"Dev","chunk":180}\n')          # 补完
    assert [e["event"] for e in session.poll()] == ["connected"]
    assert session.connected and session.peer_name == "Dev" and session.chunk_size == 180


def test_poll_recovers_from_file_truncation(session):
    emit(session, event="launch", pid=1)
    session.poll()
    # helper 重启会用 .atomic 重建 events.jsonl,文件变短
    with open(os.path.join(session.session_dir, "events.jsonl"), "w", encoding="utf-8") as fh:
        fh.write('{"event":"launch","pid":2}\n')
    assert [e["pid"] for e in session.poll()] == [2]


def test_poll_skips_malformed_lines(session):
    path = os.path.join(session.session_dir, "events.jsonl")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("not json at all\n")
        fh.write('{"event":"scan_started"}\n')
    assert [e["event"] for e in session.poll()] == ["scan_started"]


def test_missing_events_file_is_not_an_error(tmp_path):
    d = DeviceConfig(app_path=str(tmp_path / "H.app"), session_dir=str(tmp_path / "nope"))
    assert HelperSession(d).poll() == []


# ---- 状态吸收 ----

def test_disconnect_kills_the_session_permanently(session):
    emit(session, event="connected", name="Dev", chunk=180)
    session.poll()
    assert session.connected
    emit(session, event="disconnected", name="Dev", error="peer removed pairing")
    session.poll()
    # 掉线后这个会话就作废了:恢复必须靠新建 HelperSession(新进程)
    assert not session.connected
    assert not session.fatal          # 普通掉线不是致命错误,可以重连


def test_unauthorized_is_fatal_so_we_stop_retrying(session):
    emit(session, event="error", message="bluetooth unauthorized: 需在 系统设置 …")
    session.poll()
    assert session.fatal
    assert "unauthorized" in session.last_error


def test_notifications_are_buffered_not_dropped(session):
    emit(session, event="connected", name="Dev", chunk=180)
    for i in range(3):
        emit(session, event="notification", line=f'{{"n":{i}}}')
    session.poll()
    # send_line 内部也会 poll,通知必须缓冲住,否则会被顺手吞掉
    assert list(session.notifications) == ['{"n":0}', '{"n":1}', '{"n":2}']


# ---- send_line 的 ack 语义 ----

def test_send_line_returns_true_on_ack(session):
    emit(session, event="connected", name="Dev", chunk=180)
    session.poll()
    pretend_helper_is_running(session)

    def fake_helper():
        # 扮演 helper:看到命令文件就回 ack
        for _ in range(100):
            cmd_dir = os.path.join(session.session_dir, "commands")
            files = sorted(f for f in os.listdir(cmd_dir) if f.endswith(".json"))
            if files:
                path = os.path.join(cmd_dir, files[0])
                with open(path, encoding="utf-8") as fh:
                    seq = json.load(fh)["seq"]
                os.remove(path)
                emit(session, event="ack", seq=seq)
                return
            time.sleep(0.01)

    threading.Thread(target=fake_helper, daemon=True).start()
    assert session.send_line('{"hi":1}') is True


def test_send_line_gives_up_immediately_when_helper_process_is_gone(session):
    """helper 崩了就别干等超时 —— 立刻返回让上层重开进程。"""
    emit(session, event="connected", name="Dev", chunk=180)
    session.poll()
    session.alive = lambda: False
    start = time.time()
    assert session.send_line('{"hi":1}') is False
    assert time.time() - start < 0.5      # 不该等满 command_timeout


def test_send_line_times_out_when_helper_never_acks(session):
    emit(session, event="connected", name="Dev", chunk=180)
    session.poll()
    pretend_helper_is_running(session)
    session.command_timeout = 0.3
    start = time.time()
    assert session.send_line('{"hi":1}') is False
    assert 0.25 <= time.time() - start < 2.0
    assert "等 ack 超时" in session.last_error


def test_send_line_refuses_when_disconnected(session):
    assert session.send_line('{"hi":1}') is False


def test_start_fails_clearly_when_helper_not_built(session):
    assert session.start() is False
    assert "helper 未编译" in session.last_error
    assert session.fatal          # 没编译不是「等一等就好」的错误


# ---- 进程消失(helper 崩了/被 kill -9,没机会写 disconnected)----

def test_a_vanished_helper_counts_as_disconnected(session):
    """这条是补一个真事故:kill -9 worker 之后 3.5 分钟零恢复。

    成因是 connected 只看事件流,而崩掉的进程压根写不出 disconnected 事件。
    """
    emit(session, event="connected", name="Dev", chunk=180)
    session.poll()
    assert session.connected

    session.alive = lambda: False          # 进程没了,事件流里一个字都没多
    assert session.check_process_alive() is False
    assert session.connected is False      # ← 以前这里是 True,于是永不重开
    assert "进程消失" in session.last_error


def test_liveness_is_not_checked_before_the_first_connect(session):
    """启动阶段不能判存活:open 一收下请求就返回,进程还没出现(坑 2)。

    那时判「已退出」会触发重连,而重连时的 kill 又把正要起来的 helper 杀掉 ——
    永不收敛。所以这里连 alive() 都不该被调到。
    """
    session.alive = lambda: pytest.fail("连上之前不该查存活")
    assert session.check_process_alive() is True


def test_liveness_check_is_throttled(session):
    # _pids() 要 fork 一个 ps,而监护循环 0.2s 一轮 —— 不节流就是每秒 5 次 fork
    emit(session, event="connected", name="Dev", chunk=180)
    session.poll()
    calls = []
    session.alive = lambda: (calls.append(1), True)[1]

    session.check_process_alive(now=1000.0)
    session.check_process_alive(now=1001.0)     # 1 秒后:还在节流窗口内
    assert len(calls) == 1
    session.check_process_alive(now=1002.5)     # 超过 2 秒:该查了
    assert len(calls) == 2
