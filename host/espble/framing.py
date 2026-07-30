"""线协议的编码与长度约束。

BLE 这条链路上,「一行太长」不会报错,只会**悄悄少几个字节**然后帧就烂了。
所以长度约束必须在发送端做,而且要做在正确的数字上 —— 见 fit_line 的注释。
"""
from __future__ import annotations

import json
from typing import Any, Mapping

# 设备侧 LinkConfig.rxRingBytes 的默认值。
DEFAULT_RING_BYTES = 2048

# 留 10% 余量:环形缓冲是「中枢写入速度 vs 主循环排空速度」的缓冲区,
# 一行刚好等于容量意味着主循环但凡慢一拍就溢出。
RING_HEADROOM = 0.9


def limit_for_ring(ring_bytes: int = DEFAULT_RING_BYTES) -> int:
    """由设备的接收环形缓冲大小推出单行字节上限。"""
    return max(64, int(ring_bytes * RING_HEADROOM))


DEFAULT_LINE_LIMIT = limit_for_ring()


def encode(obj: Mapping[str, Any]) -> str:
    """序列化成一行。ensure_ascii=False —— 中文按 UTF-8 原样走,别膨胀成 \\uXXXX。"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def fit_line(obj: Mapping[str, Any], limit: int = DEFAULT_LINE_LIMIT,
             shrink_field: str = "msg") -> str:
    """序列化并保证不超过 limit **字节**,超了就截 shrink_field。

    两个容易踩的点:

    1. **限的是字节不是字符。** 中文一个字 3 字节,1200 字的消息 = 3600 字节,
       直接冲爆设备 2048 的环形缓冲。按字符数限长等于没限。
    2. **截断要落在字符边界上。** 直接切字节会切出半个汉字,设备侧 UTF-8 解码
       得到乱码。这里用二分找最长能放下的**字符数**。
    """
    line = encode(obj)
    if len(line.encode("utf-8")) <= limit:
        return line

    value = obj.get(shrink_field)
    if isinstance(value, str) and value:
        trimmed = dict(obj)
        lo, hi = 0, len(value)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            trimmed[shrink_field] = value[:mid] + "…"
            if len(encode(trimmed).encode("utf-8")) <= limit:
                lo = mid
            else:
                hi = mid - 1
        trimmed[shrink_field] = value[:lo] + "…"
        return encode(trimmed)

    # 兜底:没有可截的字段。按**字节**切再丢掉尾部的不完整字符,
    # 结果多半不是合法 JSON,但至少不会因为半个汉字让设备侧解码器炸掉。
    return line.encode("utf-8")[:limit].decode("utf-8", "ignore")
