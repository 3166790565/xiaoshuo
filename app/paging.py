"""分页计算：页码钳制与页码窗口，前台后台共用。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Page:
    number: int
    size: int
    total: int

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.size))

    @property
    def has_prev(self) -> bool:
        return self.number > 1

    @property
    def has_next(self) -> bool:
        return self.number < self.pages

    @property
    def prev(self) -> int:
        return max(1, self.number - 1)

    @property
    def next(self) -> int:
        return min(self.pages, self.number + 1)

    @property
    def offset(self) -> int:
        return (self.number - 1) * self.size

    def window(self, span: int = 2) -> list[int]:
        low = max(1, self.number - span)
        high = min(self.pages, self.number + span)
        return list(range(low, high + 1))


def clean_page(raw: int | str | None) -> int:
    try:
        return max(1, int(raw or 1))
    except (TypeError, ValueError):
        return 1


def make_page(raw: int | str | None, size: int, total: int) -> Page:
    number = clean_page(raw)
    pages = max(1, -(-total // size))
    return Page(number=min(number, pages), size=size, total=total)
