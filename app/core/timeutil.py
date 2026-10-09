"""站点时区换算。

约定（见 models/base.py 的 ``utcnow``）：数据库里所有时间都是 **naive UTC**，
站点时区由设置项 ``site.timezone`` 决定。

- 出库渲染：``to_local`` / ``fmt_dt``（core/templating.py）把 UTC 转成站点时区
- 入库比较：用户填的日期是**站点时区的壁钟时间**，必须先经 ``local_to_utc``
  转回 UTC，才能和库里的时间比较。少这一步就会出现"填了日期筛不到/删不掉"
  的偏差，且偏差量正好是一个时区差。

这里只依赖标准库，好让 services 层也能直接引用而不牵连 Jinja2。
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = "Asia/Shanghai"


def site_zone(tz_name: str | None) -> ZoneInfo:
    """把站点设置里的时区名解析成 ``ZoneInfo``；非法值退回 UTC。"""
    try:
        return ZoneInfo(tz_name or DEFAULT_TIMEZONE)
    except Exception:  # noqa: BLE001 - 时区名非法或容器内缺 tzdata 时兜底
        return ZoneInfo("UTC")


def local_to_utc(value: datetime, tz_name: str | None) -> datetime:
    """把站点时区的壁钟时间换算成 naive UTC。

    已经带时区信息的值直接换算；不带时区的按站点时区解释。
    """
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.replace(tzinfo=site_zone(tz_name)).astimezone(timezone.utc).replace(tzinfo=None)


def to_local(
    value: datetime | None, tz_name: str | None = DEFAULT_TIMEZONE
) -> datetime | None:
    """把库里存的 naive UTC 换算成站点时区的 aware 时间。"""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).astimezone(site_zone(tz_name))
