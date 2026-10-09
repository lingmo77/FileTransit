"""通用分页。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

DEFAULT_PER_PAGE = 24
MAX_PER_PAGE = 200


@dataclass(slots=True)
class Page:
    items: list[Any] = field(default_factory=list)
    page: int = 1
    per_page: int = DEFAULT_PER_PAGE
    total: int = 0

    @property
    def total_pages(self) -> int:
        return max(1, -(-self.total // max(1, self.per_page)))

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.total_pages

    @property
    def prev_page(self) -> int:
        return max(1, self.page - 1)

    @property
    def next_page(self) -> int:
        return min(self.total_pages, self.page + 1)

    @property
    def first_index(self) -> int:
        return 0 if not self.total else (self.page - 1) * self.per_page + 1

    @property
    def last_index(self) -> int:
        return min(self.total, self.page * self.per_page)

    def page_numbers(self, window: int = 2) -> list[int | None]:
        """页码列表，``None`` 代表省略号。"""
        pages: set[int] = set(range(1, min(self.total_pages, window) + 1))
        pages |= set(
            range(max(1, self.page - window), min(self.total_pages, self.page + window) + 1)
        )
        pages |= set(
            range(max(1, self.total_pages - window + 1), self.total_pages + 1)
        )

        result: list[int | None] = []
        previous = 0
        for number in sorted(pages):
            if previous and number - previous > 1:
                result.append(None)
            result.append(number)
            previous = number
        return result


async def paginate(
    db: AsyncSession,
    stmt: Select,
    *,
    page: int = 1,
    per_page: int = DEFAULT_PER_PAGE,
    options: tuple = (),
    order_by: tuple = (),
) -> Page:
    count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
    total = int((await db.execute(count_stmt)).scalar_one() or 0)

    per_page = max(1, min(per_page, MAX_PER_PAGE))
    total_pages = max(1, -(-total // per_page))
    page = max(1, min(page, total_pages))

    query = stmt
    if options:
        query = query.options(*options)
    if order_by:
        query = query.order_by(*order_by)

    rows = (
        await db.execute(query.offset((page - 1) * per_page).limit(per_page))
    ).scalars().all()

    return Page(items=list(rows), page=page, per_page=per_page, total=total)
