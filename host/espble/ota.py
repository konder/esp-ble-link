"""固件分发:一个够用的静态服务 + 一条发布命令。

设备侧(EspBleOta)只要求两个 URL:

    GET {base}/version      纯文本整数,例如 "40"
    GET {base}/current.bin  固件镜像

刻意做得很薄 —— 家里局域网、明文 HTTP、无鉴权。要跨公网分发请自己套 TLS
和签名校验,别直接把这个开到公网上。
"""
from __future__ import annotations

import http.server
import os
import shutil
import socketserver
import sys
from typing import Optional

VERSION_FILE = "version.txt"
IMAGE_FILE = "current.bin"


def publish(image_path: str, version: int, fw_dir: str) -> str:
    """把编译产物发布成「可被设备拉取」的一对文件。"""
    if not os.path.isfile(image_path):
        raise FileNotFoundError(image_path)
    os.makedirs(fw_dir, exist_ok=True)
    target = os.path.join(fw_dir, IMAGE_FILE)
    # 先写临时文件再 rename:设备正好在这一刻拉取时,不会读到写了一半的镜像。
    tmp = target + ".tmp"
    shutil.copyfile(image_path, tmp)
    os.replace(tmp, target)
    # 版本号最后写:先有完整镜像,再宣布新版本,顺序反了会让设备拉到旧镜像当新版本。
    with open(os.path.join(fw_dir, VERSION_FILE), "w", encoding="utf-8") as fh:
        fh.write(f"{version}\n")
    return target


class _Handler(http.server.BaseHTTPRequestHandler):
    fw_dir = ""
    base_path = "/fw"

    def log_message(self, fmt, *args):
        sys.stderr.write("[espble-ota] %s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):     # noqa: N802
        self.do_GET()

    def do_GET(self):      # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == f"{self.base_path}/version":
            try:
                with open(os.path.join(self.fw_dir, VERSION_FILE), encoding="utf-8") as fh:
                    body = fh.read().strip().encode()
            except OSError:
                self._send(404, b"no version published\n", "text/plain")
                return
            self._send(200, body, "text/plain")
        elif path == f"{self.base_path}/{IMAGE_FILE}":
            try:
                with open(os.path.join(self.fw_dir, IMAGE_FILE), "rb") as fh:
                    body = fh.read()
            except OSError:
                self._send(404, b"no image published\n", "text/plain")
                return
            self._send(200, body, "application/octet-stream")
        else:
            self._send(404, b"not found\n", "text/plain")


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve(fw_dir: str, port: int = 8899, base_path: str = "/fw",
          bind: str = "0.0.0.0") -> Optional[int]:
    handler = type("Handler", (_Handler,), {"fw_dir": fw_dir, "base_path": base_path})
    with _Server((bind, port), handler) as httpd:
        version = "(未发布)"
        try:
            with open(os.path.join(fw_dir, VERSION_FILE), encoding="utf-8") as fh:
                version = fh.read().strip()
        except OSError:
            pass
        print(f"[espble-ota] {fw_dir} → http://{bind}:{port}{base_path}/  当前版本 {version}",
              file=sys.stderr, flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("[espble-ota] 停止", file=sys.stderr)
    return 0
