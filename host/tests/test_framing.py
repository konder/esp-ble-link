import json

from espble.framing import DEFAULT_LINE_LIMIT, encode, fit_line, limit_for_ring


def test_encode_keeps_utf8():
    line = encode({"msg": "中文"})
    assert "中文" in line          # 不能被转成 \uXXXX,那样体积翻三倍
    assert "\\u" not in line


def test_short_line_untouched():
    obj = {"t": "ev", "msg": "hi"}
    assert json.loads(fit_line(obj)) == obj


def test_limit_is_bytes_not_chars():
    # 400 个汉字 = 1200 字节,远超 200 字节的上限,但只有 400 个「字符」。
    obj = {"t": "ev", "msg": "中" * 400}
    line = fit_line(obj, limit=200)
    assert len(line.encode("utf-8")) <= 200
    assert len(obj["msg"]) == 400        # 原对象没被就地改坏


def test_truncation_lands_on_char_boundary():
    obj = {"t": "ev", "msg": "中" * 400}
    line = fit_line(obj, limit=200)
    # 关键:切出来的还得是合法 UTF-8 且能解析回 JSON —— 按字节硬切会切出半个汉字
    parsed = json.loads(line)
    assert parsed["msg"].endswith("…")
    assert parsed["t"] == "ev"


def test_truncation_binary_searches_to_the_largest_fit():
    obj = {"t": "ev", "msg": "中" * 400}
    limit = 200
    parsed = json.loads(fit_line(obj, limit=limit))
    kept = len(parsed["msg"]) - 1        # 减掉省略号
    # 再多留一个字就该超了,否则说明二分没找到最大值
    bigger = dict(obj, msg="中" * (kept + 1) + "…")
    assert len(encode(bigger).encode("utf-8")) > limit


def test_other_fields_survive_truncation():
    obj = {"t": "ev", "kind": "done", "project": "demo", "ts": 123, "msg": "中" * 400}
    parsed = json.loads(fit_line(obj, limit=220))
    assert parsed["kind"] == "done" and parsed["project"] == "demo" and parsed["ts"] == 123


def test_no_shrinkable_field_still_produces_valid_utf8():
    obj = {"blob": "中" * 400}           # shrink_field 默认是 msg,这里没有
    line = fit_line(obj, limit=100)
    assert len(line.encode("utf-8")) <= 100
    line.encode("utf-8").decode("utf-8")  # 不能有半个汉字


def test_custom_shrink_field():
    obj = {"t": "ev", "detail": "x" * 500}
    parsed = json.loads(fit_line(obj, limit=120, shrink_field="detail"))
    assert parsed["detail"].endswith("…")


def test_limit_for_ring_leaves_headroom():
    assert limit_for_ring(2048) < 2048
    assert DEFAULT_LINE_LIMIT == limit_for_ring(2048)
