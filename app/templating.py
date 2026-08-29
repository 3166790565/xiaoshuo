"""共享的 Jinja2 环境：过滤器、全局变量与占位封面配色。"""

from __future__ import annotations

from fastapi.templating import Jinja2Templates

from . import config
from .services.repo import SPLIT_MODE_LABELS

templates = Jinja2Templates(directory=str(config.TEMPLATE_DIR))


def word_count(value: int | None) -> str:
    number = int(value or 0)
    if number >= 10_000:
        return f"{number / 10_000:.1f} 万字"
    return f"{number} 字"


def mode_label(value: str) -> str:
    return SPLIT_MODE_LABELS.get(value, value or "-")


# 无封面时按书名生成稳定的渐变占位块，一共 6 套暖色调
_COVER_PALETTE = (
    ("#2f4f4a", "#6b8f83"),
    ("#4a3f5c", "#8a7aa3"),
    ("#5c3f3f", "#a37a72"),
    ("#3f4a5c", "#7a90a3"),
    ("#4f4a2f", "#8f8a6b"),
    ("#3f5c4a", "#7aa389"),
)


def cover_gradient(title: str) -> str:
    start, end = _COVER_PALETTE[sum(map(ord, title or "书")) % len(_COVER_PALETTE)]
    return f"linear-gradient(150deg, {start} 0%, {end} 100%)"


def cover_initial(title: str) -> str:
    return (title or "书")[:1]


templates.env.filters["word_count"] = word_count
templates.env.filters["mode_label"] = mode_label
templates.env.filters["cover_gradient"] = cover_gradient
templates.env.filters["cover_initial"] = cover_initial
templates.env.globals.update(
    site_name=config.SITE_NAME,
    site_tagline=config.SITE_TAGLINE,
    split_mode_labels=SPLIT_MODE_LABELS,
)
