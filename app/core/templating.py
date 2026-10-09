"""共享的 Jinja2 环境与模板过滤器。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from .config import BASE_DIR
from .timeutil import DEFAULT_TIMEZONE, site_zone, to_local

TEMPLATES_DIR = BASE_DIR / "app" / "templates"
STATIC_DIR = BASE_DIR / "app" / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# ------------------------------------------------------------------ 过滤器


def human_size(num: int | float | None) -> str:
    if num is None:
        return "-"
    size = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.2f} {unit}" if size < 100 else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def fmt_dt(
    value: datetime | None, tz_name: str = DEFAULT_TIMEZONE, fmt: str = "%Y-%m-%d %H:%M"
) -> str:
    local = to_local(value, tz_name)
    return local.strftime(fmt) if local else "-"


def fmt_iso(value: datetime | None) -> str:
    """带时区的 ISO 时间，供前端 JS 处理。"""
    if value is None:
        return ""
    return value.replace(tzinfo=timezone.utc).isoformat()


def remaining(value: datetime | None, tz_name: str = DEFAULT_TIMEZONE) -> str:
    """剩余有效期的人性化描述；``None`` 表示长期有效。"""
    if value is None:
        return "长期有效"
    local = to_local(value, tz_name)
    delta = local - datetime.now(site_zone(tz_name))
    seconds = int(delta.total_seconds())
    if seconds <= 0:
        return "已过期"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days} 天 {hours} 小时"
    if hours:
        return f"{hours} 小时 {minutes} 分"
    return f"{minutes} 分钟"


def is_expiring_soon(value: datetime | None, hours: int = 24) -> bool:
    if value is None:
        return False
    return (value - datetime.now(timezone.utc).replace(tzinfo=None)) < timedelta(hours=hours)


def pretty_json(value: str | None) -> str:
    if not value:
        return ""
    try:
        return json.dumps(json.loads(value), ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return value


#: 分类 -> 扩展名（不含点）。既用于模板图标，也用于列表筛选。
CATEGORY_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "image": ("jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "ico", "heic", "avif"),
    "video": ("mp4", "mkv", "avi", "mov", "webm", "flv", "wmv", "m4v", "ts"),
    "audio": ("mp3", "flac", "wav", "aac", "ogg", "m4a", "wma", "opus"),
    "archive": ("zip", "rar", "7z", "tar", "gz", "bz2", "xz", "iso", "tgz", "zst"),
    "app": ("exe", "msi", "apk", "dmg", "deb", "rpm", "appimage", "ipa"),
    "document": ("pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "md", "csv", "rtf", "epub"),
    "code": ("py", "js", "ts", "java", "go", "rs", "c", "cpp", "h", "json", "yaml", "yml", "sh", "sql", "html", "css"),
}

CATEGORY_LABELS: dict[str, str] = {
    "image": "图片",
    "video": "视频",
    "audio": "音频",
    "archive": "压缩包",
    "app": "安装包",
    "document": "文档",
    "code": "代码",
    "other": "其它",
}


def file_category(mime: str | None, filename: str = "") -> str:
    """用于列表分组与图标选择。"""
    mime = (mime or "").lower()
    ext = Path(filename).suffix.lower().lstrip(".")
    for category, extensions in CATEGORY_EXTENSIONS.items():
        if ext in extensions:
            return category
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("text/"):
        return "document"
    return "other"


# ------------------------------------------------------------------ 图标

#: Feather 风格的线性图标，模板中通过 {{ icon('upload') }} 直接使用
_ICONS: dict[str, str] = {
    "upload": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>',
    "download": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>',
    "file": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>',
    "trash": '<polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
    "copy": '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
    "check": '<polyline points="20 6 9 17 4 12"/>',
    "x": '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/>',
    "moon": '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>',
    "user": '<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
    "users": '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    "shield": '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
    "mail": '<rect x="2" y="4" width="20" height="16" rx="2"/><path d="m22 7-10 6L2 7"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"/>',
    "chart": '<line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/>',
    "list": '<line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/>',
    "logout": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/>',
    "search": '<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>',
    "plus": '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>',
    "clock": '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
    "alert": '<circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>',
    "inbox": '<polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
    "database": '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/>',
    "globe": '<circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>',
    "chevron-left": '<polyline points="15 18 9 12 15 6"/>',
    "chevron-right": '<polyline points="9 18 15 12 9 6"/>',
    "external": '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/>',
    "refresh": '<polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>',
    "lock": '<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    "key": '<path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3"/>',
    "send": '<line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/>',
    "save": '<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/>',
    "eye": '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>',
}


def icon(name: str, size: int = 18, cls: str = "") -> Markup:
    inner = _ICONS.get(name, _ICONS["file"])
    klass = f' class="{cls}"' if cls else ""
    return Markup(
        f'<svg{klass} width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
        f'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
        f'stroke-linejoin="round" aria-hidden="true">{inner}</svg>'
    )


def page_url(base_path: str, params: dict | None, page: int = 1) -> str:
    """拼接保留筛选条件的分页链接。"""
    from urllib.parse import urlencode

    query = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
    if page and page > 1:
        query["page"] = page
    else:
        query.pop("page", None)
    encoded = urlencode({k: v for k, v in query.items() if v not in (None, "")})
    return f"{base_path}?{encoded}" if encoded else base_path


templates.env.globals["icon"] = icon
templates.env.globals["page_url"] = page_url
templates.env.globals["CATEGORY_LABELS"] = CATEGORY_LABELS
# 按站点时区取年份：UTC+8 在元旦前 8 小时会跨年，用 UTC 会显示旧年份
templates.env.globals["now_year"] = lambda tz_name=None: datetime.now(site_zone(tz_name)).year

# services.activity 不反向依赖模板层，可以安全导入
from ..services.activity import ACTION_LABELS  # noqa: E402

templates.env.globals["ACTION_LABELS"] = ACTION_LABELS

templates.env.filters["human_size"] = human_size

templates.env.filters["fmt_dt"] = fmt_dt
templates.env.filters["fmt_iso"] = fmt_iso
templates.env.filters["remaining"] = remaining
templates.env.filters["is_expiring_soon"] = is_expiring_soon
templates.env.filters["pretty_json"] = pretty_json
templates.env.filters["file_category"] = file_category
templates.env.filters["basename"] = lambda p: Path(p).name if p else ""
templates.env.filters["ext"] = lambda name: (Path(name).suffix[1:].upper()[:5] if name else "")
