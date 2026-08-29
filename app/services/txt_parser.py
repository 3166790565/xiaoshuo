"""txt 解析核心：解码后的文本 → 章节列表。

三态分章（样本实测 120 个 txt 里 70% 完全没有章节标记，所以无标记是主路径）：

- ``marked``：正文有可信的章节标记，按标记行切
- ``single``：整本一章（短篇；或只有一个标记且落在开头）
- ``auto``  ：无标记的长文，按目标字数在段落边界切节

对外只有 :func:`parse_bytes` 与 :func:`parse_text` 两个入口，
输出统一的 :class:`ParsedBook`，以便将来接 epub 时不动其余代码。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from urllib.parse import unquote

from .decoder import decode_bytes

# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class ParseOptions:
    """解析选项。前三项来自后台设置页，force_mode 来自上传表单/重新分章。"""

    target_chars: int = 3000
    strip_header_block: bool = False
    fix_yao_variant: bool = False
    add_subtitle: bool = True
    strip_ads: bool = True
    merge_hard_wraps: bool = True
    force_mode: str = ""  # "" 自动判定 | "single" 强制单章 | "auto" 强制按字数切

    def normalized(self) -> "ParseOptions":
        target = int(self.target_chars or 3000)
        target = max(500, min(50_000, target))
        mode = self.force_mode if self.force_mode in ("single", "auto") else ""
        return replace(self, target_chars=target, force_mode=mode)


@dataclass
class Chapter:
    idx: int
    title: str
    content: str  # 段落之间用单个 \n 分隔，正文不含缩进空格
    word_count: int


@dataclass
class ParsedBook:
    title: str
    author: str
    chapters: list[Chapter]
    split_mode: str
    word_count: int
    encoding: str = ""
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 规范化与行清理
# ---------------------------------------------------------------------------

# 样本里普遍是 \r\r\n，逐个 replace 会多出空行，所以一次性用正则收敛
_NEWLINES = re.compile(r"\r+\n|\r+")
_ZERO_WIDTH = re.compile(r"[﻿​-‏⁠᠎]")
# 整行都是装饰字符的分隔线，含开头标题块的 ###### 围栏
_SEPARATOR = re.compile(r"^[#=*\-_~—＝－—…·.\s]{3,}$")
_INDENT = re.compile(r"^[ \t　\xa0]{2,}")

# 命中且整行短于 60 字才删，避免误伤正文里出现的网址式比喻
_AD_PATTERNS = (
    re.compile(r"https?://", re.I),
    re.compile(r"www\.", re.I),
    re.compile(r"\.(com|net|org|cn|cc|xyz|top|info|me)\b", re.I),
    re.compile(r"更新最快"),
    re.compile(r"请收藏"),
    re.compile(r"本书由.{0,20}整理"),
    re.compile(r"最新章节.{0,10}(阅读|下载|访问)"),
    re.compile(r"免费(阅读|下载|观看)"),
    re.compile(r"(书友|读者|微信|QQ)\s*群?\s*[:：]?\s*\d{5,}"),
)

_WS = re.compile(r"\s+")


def count_words(text: str) -> int:
    """字数统计：去掉所有空白后的字符数，中文按字计。"""
    return len(_WS.sub("", text))


@dataclass(slots=True)
class _Line:
    text: str  # 已 strip；空串代表段落分隔的空行
    indented: bool  # 原行有缩进，是"新段落开始"的强信号


def normalize_text(text: str) -> str:
    text = _NEWLINES.sub("\n", text)
    text = _ZERO_WIDTH.sub("", text)
    text = text.replace("\xa0", " ")
    return re.sub(r"\n{3,}", "\n\n", text)


def _is_ad(line: str) -> bool:
    if len(line) >= 60:
        return False
    return any(p.search(line) for p in _AD_PATTERNS)


def _to_lines(text: str, opts: ParseOptions) -> list[_Line]:
    """切行 → 丢弃分隔线与广告行 → 连续空行压成一个。"""
    out: list[_Line] = []
    for raw in text.split("\n"):
        indented = bool(_INDENT.match(raw))
        line = raw.strip()
        if not line:
            if out and out[-1].text:
                out.append(_Line("", False))
            continue
        if _SEPARATOR.match(line):
            continue
        if opts.strip_ads and _is_ad(line):
            continue
        out.append(_Line(line, indented))
    while out and not out[-1].text:
        out.pop()
    return out


def extract_header_block(text: str) -> tuple[str, str, str]:
    """剥出开头的 ``######`` 标题块。

    返回 ``(块原文, 去掉块之后的正文, 块内书名)``；没有块时块与书名为空串。
    重新分章时正文里的 ``######`` 围栏早已被清掉、只剩 ``#  书名`` 一行，
    所以这里只要求首个非空行以 ``#`` 开头，两种形态都能认出来。
    """
    lines = text.split("\n")
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    if start >= len(lines) or not lines[start].strip().startswith("#"):
        return "", text, ""

    titles: list[str] = []
    end = start
    cursor = start
    limit = min(len(lines), start + 12)
    while cursor < limit:
        stripped = lines[cursor].strip()
        if not stripped:
            cursor += 1
            continue
        if not stripped.startswith("#"):
            break
        inner = stripped.strip("#").strip()
        if inner:
            titles.append(inner)
        end = cursor
        cursor += 1

    block = "\n".join(lines[start : end + 1])
    rest = "\n".join(lines[end + 1 :])
    return block, rest, titles[0] if titles else ""


# ---------------------------------------------------------------------------
# 章节标记识别
# ---------------------------------------------------------------------------

_NUM = "0-9０-９零一二三四五六七八九十百千万两壹贰叁肆伍陆柒捌玖拾廿"

# 强标记：单独成行即可采信
_STRONG_MARKS = (
    re.compile(rf"^第[{_NUM}]{{1,8}}[章节節回卷篇集话話幕][\s：:、.．\-—]*.{{0,36}}$"),
    re.compile(r"^(序章|序|楔子|引子|前言|后记|後記|尾声|终章|番外.{0,20})$"),
    re.compile(r"^[Cc][Hh][Aa][Pp][Tt][Ee][Rr]\s*\d{1,4}\b.{0,36}$"),
)

# 弱标记：同一样式命中 3 次以上才启用，否则编号列表会被当成章节
_WEAK_MARKS = (
    re.compile(r"^\d{1,4}[、.。．,，]\s*.{0,36}$"),
    re.compile(rf"^[（(【\[]\s*[{_NUM}]{{1,4}}\s*[）)】\]]\s*$"),
)

_MARK_MAX_LEN = 40  # 整行去空白后不超过 40 字，避免误伤正文里提到的"第一章"
_WEAK_MIN_HITS = 3
_MIN_GAP_CHARS = 200  # 相邻标记之间正文少于此值算"过密"
_MAX_PREAMBLE_RATIO = 0.5  # 首个标记之前的正文占比上限，超了说明标记不是章节结构
_HEAD_RATIO = 0.05  # 单个标记落在正文前 5% 才算书名式标记


def find_marks(lines: list[_Line]) -> list[int]:
    """返回被判定为章节标题的行下标。"""
    strong: list[int] = []
    weak: dict[int, list[int]] = {}
    for i, line in enumerate(lines):
        text = line.text
        if not text or len(text) > _MARK_MAX_LEN:
            continue
        if any(p.match(text) for p in _STRONG_MARKS):
            strong.append(i)
            continue
        for kind, pattern in enumerate(_WEAK_MARKS):
            if pattern.match(text):
                weak.setdefault(kind, []).append(i)
                break
    # 有可信强标记时不再混入弱标记，否则正文里的编号列表会把章切碎
    if len(strong) >= 2:
        return strong
    marks = set(strong)
    for hits in weak.values():
        if len(hits) >= _WEAK_MIN_HITS:
            marks.update(hits)
    return sorted(marks)


def _body_chars(lines: list[_Line]) -> int:
    return count_words("".join(line.text for line in lines))


def marks_are_plausible(lines: list[_Line], marks: list[int]) -> bool:
    """间距校验：过密的标记多半是目录页或人物列表，不是真章节。"""
    if len(marks) < 2:
        return False
    total = _body_chars(lines)
    preamble = count_words("".join(line.text for line in lines[: marks[0]]))
    # 实测有整本 4 万字、只在末尾冒出两个标记的文件。这种标记不是章节结构，
    # 采信它会把前面全部正文攒成一个读不下去的超长章，退回按字数分节更好。
    if total and preamble > total * _MAX_PREAMBLE_RATIO:
        return False
    bounds = marks + [len(lines)]
    gaps = [
        count_words("".join(lines[i].text for i in range(a + 1, b)))
        for a, b in zip(bounds, bounds[1:])
    ]
    thin = sum(1 for gap in gaps if gap < _MIN_GAP_CHARS)
    return thin * 2 <= len(gaps)


def _mark_at_head(lines: list[_Line], pos: int) -> bool:
    total = _body_chars(lines)
    if total == 0:
        return True
    before = count_words("".join(line.text for line in lines[:pos]))
    return before <= 200 or before <= total * _HEAD_RATIO


# ---------------------------------------------------------------------------
# 段落格式化
# ---------------------------------------------------------------------------

# 行尾出现这些字符说明句子已收，下一行是新段落
_SENT_END = set("。！？；…”\"』」）)】》!?;：:～~—")


def _continues(prev: str, line: _Line) -> bool:
    """判断 line 是否是被硬换行截断的 prev 的后续。"""
    if line.indented:
        return False
    if prev[-1] in _SENT_END:
        return False
    return len(prev) <= 4000


def build_paragraphs(lines: list[_Line], opts: ParseOptions) -> list[str]:
    paragraphs: list[str] = []
    current = ""
    for line in lines:
        if not line.text:
            if current:
                paragraphs.append(current)
                current = ""
            continue
        if not current:
            current = line.text
        elif opts.merge_hard_wraps and _continues(current, line):
            current += line.text
        else:
            paragraphs.append(current)
            current = line.text
    if current:
        paragraphs.append(current)
    return paragraphs


# ---------------------------------------------------------------------------
# 分节（auto 模式）
# ---------------------------------------------------------------------------

_CUT_PUNCT = set("。！？…")
_CLOSERS = set("”\"』」）)】》…")
_LONG_PARA_RATIO = 1.5  # 超过目标的这个倍数就在段内二次切分
_TAIL_MIN_RATIO = 0.3  # 末节不足目标的这个比例就并入上一节
_EARLY_CLOSE_RATIO = 0.8  # 已攒到目标的这个比例，就允许提前断章
_OVERSHOOT_RATIO = 1.3  # 再加一段会超过目标的这个倍数，那就先断


def split_long_paragraph(text: str, target: int) -> list[str]:
    """超长单段（实测最长 6737 字）在句末标点处二次切分。

    切点取窗口内最接近目标字数的句末标点；找不到标点就按字数硬切。
    """
    pieces: list[str] = []
    start = 0
    total = len(text)
    window = int(target * _LONG_PARA_RATIO)
    while total - start > window:
        ideal = start + target
        lo = start + max(1, target // 2)
        hi = min(total, start + window)
        best = -1
        for i in range(lo, hi):
            if text[i] in _CUT_PUNCT and (best < 0 or abs(i - ideal) < abs(best - ideal)):
                best = i
        if best < 0:
            cut = min(total, ideal)
        else:
            cut = best + 1
            while cut < total and text[cut] in _CLOSERS:  # 别把收尾引号留到下一节
                cut += 1
        pieces.append(text[start:cut])
        start = cut
    if start < total:
        pieces.append(text[start:])
    return pieces


_SUB_SPLIT = re.compile(r"[。！？…!?]")


def _subtitle(paragraph: str) -> str:
    """取首段第一个短句当副标题，纯装饰。"""
    head = _SUB_SPLIT.split(paragraph.strip(), 1)[0]
    head = head.strip().strip("“”\"'‘’『』「」()（）　#*·-—_").strip()
    return head if 2 <= len(head) <= 20 else ""


def _make_chapter(idx: int, title: str, paragraphs: list[str]) -> Chapter:
    content = "\n".join(paragraphs)
    return Chapter(idx=idx, title=title or f"第 {idx} 节", content=content,
                   word_count=count_words(content))


def _build_auto(paragraphs: list[str], opts: ParseOptions, notes: list[str]) -> list[Chapter]:
    target = opts.target_chars
    units: list[str] = []
    long_paras = 0
    for para in paragraphs:
        if len(para) > target * _LONG_PARA_RATIO:
            pieces = split_long_paragraph(para, target)
            units.extend(pieces)
            long_paras += 1
        else:
            units.append(para)
    if long_paras:
        notes.append(f"{long_paras} 个超长段落已在句末标点二次切分")

    sections: list[list[str]] = []
    current: list[str] = []
    current_len = 0
    for unit in units:
        unit_len = count_words(unit)
        # 已经差不多够一节了，再加这一段会明显超标：先断，别攒出双倍长的章
        if (
            current
            and current_len >= target * _EARLY_CLOSE_RATIO
            and current_len + unit_len > target * _OVERSHOOT_RATIO
        ):
            sections.append(current)
            current = []
            current_len = 0
        current.append(unit)
        current_len += unit_len
        if current_len >= target:  # 总在段落边界断章
            sections.append(current)
            current = []
            current_len = 0
    if current:
        sections.append(current)

    if len(sections) >= 2:
        tail = count_words("".join(sections[-1]))
        if tail < target * _TAIL_MIN_RATIO:
            sections[-2].extend(sections.pop())
            notes.append("末节过短，已并入上一节")

    chapters: list[Chapter] = []
    for i, section in enumerate(sections, 1):
        title = f"第 {i} 节"
        if opts.add_subtitle:
            hint = _subtitle(section[0])
            if hint:
                title = f"{title} · {hint}"
        chapters.append(_make_chapter(i, title, section))
    return chapters


def _build_marked(
    lines: list[_Line], marks: list[int], opts: ParseOptions, notes: list[str]
) -> list[Chapter]:
    chapters: list[Chapter] = []
    preamble = build_paragraphs(lines[: marks[0]], opts)
    preamble_chars = count_words("".join(preamble))

    if preamble and preamble_chars >= 100:
        if preamble_chars > opts.target_chars:
            # 首个标记之前是无标记正文，按字数切开，别攒成一个读不下去的超长章
            plain = replace(opts, add_subtitle=False)
            sections = _build_auto(preamble, plain, notes)
            for section in sections:
                chapters.append(
                    _make_chapter(len(chapters) + 1, f"开篇 {section.title}", [section.content])
                )
            notes.append(f"首个标记前有 {preamble_chars} 字正文，已切成 {len(sections)} 节")
        else:
            chapters.append(_make_chapter(1, "开篇", preamble))
        preamble = []

    bounds = marks + [len(lines)]
    for a, b in zip(bounds, bounds[1:]):
        paragraphs = build_paragraphs(lines[a + 1 : b], opts)
        if preamble:  # 太短的引言并进第一章，不单独成章
            paragraphs = preamble + paragraphs
            preamble = []
        if not paragraphs:  # 卷标题这类空章不入库
            continue
        chapters.append(_make_chapter(len(chapters) + 1, lines[a].text, paragraphs))
    return chapters


# ---------------------------------------------------------------------------
# 书名与作者
# ---------------------------------------------------------------------------

_AUTHOR_RE = re.compile(r"作\s*者\s*[:：]?\s*([^\s,，。/\\|、]{1,20})")
_EXT_RE = re.compile(r"\.(txt|text)$", re.I)
_TITLE_AUTHOR_RE = re.compile(r"^(?P<title>[^-－—_]{1,60})\s*[-－—_]\s*(?P<author>[^-－—_\s]{1,20})$")


def clean_filename(name: str) -> str:
    """旧目录里的文件名是 URL 编码的中文，导入前先解码。"""
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    if "%" in name:
        try:
            name = unquote(name, encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, ValueError):
            pass
    return name


def filename_stem(name: str) -> str:
    return _EXT_RE.sub("", clean_filename(name)).strip()


def _pick_meta(text: str) -> tuple[str, str]:
    """从一小段文本里尽量抽出 (书名, 作者)。"""
    body = text.strip()
    author = ""
    match = _AUTHOR_RE.search(body)
    if match:
        author = match.group(1)
        body = (body[: match.start()] + " " + body[match.end() :]).strip()
    match = re.search(r"《([^》]{1,60})》", body)
    if match:
        return match.group(1).strip(), author
    match = _TITLE_AUTHOR_RE.match(body)
    if match:
        return match.group("title").strip(), author or match.group("author").strip()
    body = body.strip(" #*=-_·　")
    if "\n" in body or len(body) > 60:
        return "", author
    return body, author


def extract_meta(filename: str, header_block: str, header_title: str) -> tuple[str, str]:
    """书名抽取始终执行，与是否剥离标题块无关。"""
    title = author = ""
    for source in (header_title, header_block, filename_stem(filename)):
        if not source:
            continue
        found_title, found_author = _pick_meta(source)
        title = title or found_title
        author = author or found_author
    return (title or filename_stem(filename) or "未命名")[:80], (author or "未知")[:40]


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def parse_text(
    text: str,
    filename: str = "",
    options: ParseOptions | None = None,
    encoding: str = "",
) -> ParsedBook:
    opts = (options or ParseOptions()).normalized()
    notes: list[str] = []

    text = normalize_text(text)
    if opts.fix_yao_variant:
        # 在切块之前替换，书名跟着一起纠正
        text = text.replace("幺", "么")
    block, rest, header_title = extract_header_block(text)
    body = rest if (block and opts.strip_header_block) else text

    lines = _to_lines(body, opts)
    title, author = extract_meta(filename, block, header_title)
    if opts.fix_yao_variant:  # 文件名来的书名也要纠正
        title, author = title.replace("幺", "么"), author.replace("幺", "么")

    mode = opts.force_mode
    marks: list[int] = []
    single_title = ""

    if not mode:
        marks = find_marks(lines)
        if marks_are_plausible(lines, marks):
            mode = "marked"
        else:
            if len(marks) == 1 and _mark_at_head(lines, marks[0]):
                # 开头孤零零一个标记是书名式标题，不是章节分界：摘出来当章名
                single_title = lines[marks[0]].text
                lines = lines[: marks[0]] + lines[marks[0] + 1 :]
            marks = []
            mode = "auto" if _body_chars(lines) > opts.target_chars else "single"

    if mode == "marked":
        chapters = _build_marked(lines, marks, opts, notes)
        if not chapters:  # 标记全是空章，退回单章
            mode = "single"
    if mode == "single":
        paragraphs = build_paragraphs(lines, opts)
        chapters = [_make_chapter(1, single_title or title, paragraphs)] if paragraphs else []
    elif mode == "auto":
        chapters = _build_auto(build_paragraphs(lines, opts), opts, notes)

    return ParsedBook(
        title=title,
        author=author,
        chapters=chapters,
        split_mode=mode,
        word_count=sum(c.word_count for c in chapters),
        encoding=encoding,
        notes=notes,
    )


def parse_bytes(
    data: bytes, filename: str = "", options: ParseOptions | None = None
) -> ParsedBook:
    text, encoding = decode_bytes(data)
    return parse_text(text, filename, options, encoding=encoding)

