"""txt 字节流解码：BOM 优先，charset-normalizer 探测，gb18030 兜底。"""

from __future__ import annotations

_BOMS: tuple[tuple[bytes, str], ...] = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)


def _try(data: bytes, encoding: str) -> str | None:
    try:
        return data.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        return None


def decode_bytes(data: bytes) -> tuple[str, str]:
    """返回 (文本, 实际使用的编码名)。任何输入都能解出文本，不抛异常。"""
    if not data:
        return "", "utf-8"

    for bom, encoding in _BOMS:
        if data.startswith(bom):
            text = _try(data, encoding)
            if text is not None:
                return text, encoding

    # 严格 utf-8 能过就直接用：中文 txt 里 utf-8 与 gb18030 互相误判的概率很低
    text = _try(data, "utf-8")
    if text is not None:
        return text, "utf-8"

    try:
        from charset_normalizer import from_bytes

        best = from_bytes(data).best()
        if best is not None and best.encoding:
            guessed = _try(data, best.encoding)
            if guessed is not None:
                return guessed, best.encoding
    except Exception:
        pass

    # gb18030 是 GBK/GB2312 的超集，作为中文兜底几乎不会失败
    for encoding in ("gb18030", "big5", "shift_jis"):
        guessed = _try(data, encoding)
        if guessed is not None:
            return guessed, encoding

    return data.decode("utf-8", errors="replace"), "utf-8/replace"
