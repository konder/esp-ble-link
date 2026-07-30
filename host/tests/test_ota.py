import http.client
import os
import threading

import pytest

from espble import ota


def test_publish_writes_image_and_version(tmp_path):
    image = tmp_path / "firmware.bin"
    image.write_bytes(b"\xe9" + b"x" * 100)
    fw = tmp_path / "fw"

    ota.publish(str(image), 7, str(fw))

    assert (fw / ota.IMAGE_FILE).read_bytes() == image.read_bytes()
    assert (fw / ota.VERSION_FILE).read_text().strip() == "7"
    # 临时文件不能留下 —— 设备正好这时来拉会读到半个镜像
    assert not any(n.endswith(".tmp") for n in os.listdir(fw))


def test_publish_replaces_previous_release(tmp_path):
    fw = tmp_path / "fw"
    for version, blob in ((1, b"old"), (2, b"newer")):
        image = tmp_path / f"v{version}.bin"
        image.write_bytes(blob)
        ota.publish(str(image), version, str(fw))
    assert (fw / ota.IMAGE_FILE).read_bytes() == b"newer"
    assert (fw / ota.VERSION_FILE).read_text().strip() == "2"


def test_publish_rejects_missing_image(tmp_path):
    with pytest.raises(FileNotFoundError):
        ota.publish(str(tmp_path / "nope.bin"), 1, str(tmp_path / "fw"))


@pytest.fixture
def server(tmp_path):
    fw = tmp_path / "fw"
    image = tmp_path / "firmware.bin"
    image.write_bytes(b"BINARY")
    ota.publish(str(image), 42, str(fw))

    handler = type("H", (ota._Handler,), {"fw_dir": str(fw), "base_path": "/fw"})
    httpd = ota._Server(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address
    httpd.shutdown()
    httpd.server_close()


def _get(addr, path):
    conn = http.client.HTTPConnection(addr[0], addr[1], timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp.status, body


def test_serves_the_two_urls_the_device_needs(server):
    # 设备侧 EspBleOta 只认这两个 URL,签名变了固件就升不动
    assert _get(server, "/fw/version") == (200, b"42")
    assert _get(server, "/fw/current.bin") == (200, b"BINARY")


def test_unknown_path_is_404(server):
    status, _ = _get(server, "/whatever")
    assert status == 404


def test_version_is_plain_integer_no_trailing_newline(server):
    _, body = _get(server, "/fw/version")
    # 设备侧用 toInt() 解析;前后有空白无所谓,但别返回 HTML 之类
    assert body.strip().isdigit()


def test_missing_release_is_404_not_500(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    handler = type("H", (ota._Handler,), {"fw_dir": str(empty), "base_path": "/fw"})
    httpd = ota._Server(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        assert _get(httpd.server_address, "/fw/version")[0] == 404
        assert _get(httpd.server_address, "/fw/current.bin")[0] == 404
    finally:
        httpd.shutdown()
        httpd.server_close()
