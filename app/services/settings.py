"""可持久化的解析选项，存在 app_settings 表里，改一次长期生效。"""

from __future__ import annotations

from .. import db
from .txt_parser import ParseOptions

DEFAULTS: dict[str, str] = {
    "target_chars": "3000",
    "strip_header_block": "0",  # 方案里定为默认关闭
    "fix_yao_variant": "0",  # 同上
    "add_subtitle": "1",
}

_BOOL_KEYS = frozenset({"strip_header_block", "fix_yao_variant", "add_subtitle"})


def get_all() -> dict[str, str]:
    values = dict(DEFAULTS)
    for row in db.query("SELECT key, value FROM app_settings"):
        if row["key"] in DEFAULTS:
            values[row["key"]] = row["value"]
    return values


def save(values: dict[str, object]) -> None:
    with db.transaction() as conn:
        for key, value in values.items():
            if key not in DEFAULTS:
                continue
            if key in _BOOL_KEYS:
                stored = "1" if value else "0"
            else:
                try:
                    stored = str(int(value))  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    continue
            conn.execute(
                "INSERT INTO app_settings(key, value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, stored),
            )


def default_target_chars() -> int:
    return int(get_all()["target_chars"])


def load_options(force_mode: str = "", target_chars: int | None = None) -> ParseOptions:
    values = get_all()
    return ParseOptions(
        target_chars=int(target_chars or values["target_chars"]),
        strip_header_block=values["strip_header_block"] == "1",
        fix_yao_variant=values["fix_yao_variant"] == "1",
        add_subtitle=values["add_subtitle"] == "1",
        force_mode=force_mode,
    ).normalized()
