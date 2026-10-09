"""操作日志写入与 User-Agent 解析。

日志使用**独立会话**提交：即使业务流程随后回滚（例如登录失败），
审计记录也必须留下。
"""

from __future__ import annotations

import json
import re
from typing import Any

from fastapi import Request

from ..core.database import SessionLocal
from ..core.security import client_ip, user_agent
from ..models import ActivityLog, User


class Action:
    REGISTER = "register"
    EMAIL_VERIFIED = "email_verified"
    RESEND_VERIFICATION = "resend_verification"
    LOGIN = "login"
    LOGIN_FAILED = "login_failed"
    LOGOUT = "logout"
    PASSWORD_CHANGE = "password_change"
    PROFILE_UPDATE = "profile_update"
    UPLOAD = "upload"
    DOWNLOAD = "download"
    DELETE_FILE = "delete_file"
    UPDATE_FILE = "update_file"
    EXPIRE_CLEANUP = "expire_cleanup"
    ADMIN_USER_UPDATE = "admin_user_update"
    ADMIN_DELETE_FILE = "admin_delete_file"
    ADMIN_SETTINGS_UPDATE = "admin_settings_update"
    ADMIN_SMTP_TEST = "admin_smtp_test"
    ADMIN_LOGS_PURGE = "admin_logs_purge"


ACTION_LABELS: dict[str, str] = {
    Action.REGISTER: "注册账号",
    Action.EMAIL_VERIFIED: "邮箱验证",
    Action.RESEND_VERIFICATION: "重发验证邮件",
    Action.LOGIN: "登录",
    Action.LOGIN_FAILED: "登录失败",
    Action.LOGOUT: "登出",
    Action.PASSWORD_CHANGE: "修改密码",
    Action.PROFILE_UPDATE: "修改资料",
    Action.UPLOAD: "上传文件",
    Action.DOWNLOAD: "下载文件",
    Action.DELETE_FILE: "删除文件",
    Action.UPDATE_FILE: "修改文件",
    Action.EXPIRE_CLEANUP: "过期清理",
    Action.ADMIN_USER_UPDATE: "管理-用户变更",
    Action.ADMIN_DELETE_FILE: "管理-删除文件",
    Action.ADMIN_SETTINGS_UPDATE: "管理-修改设置",
    Action.ADMIN_SMTP_TEST: "管理-测试邮件",
    Action.ADMIN_LOGS_PURGE: "管理-清空日志",
}

# ------------------------------------------------------------------ UA 解析

_BROWSERS: list[tuple[str, re.Pattern[str]]] = [
    ("Edge", re.compile(r"Edg(?:e|A|iOS)?/([\d.]+)")),
    ("Opera", re.compile(r"(?:OPR|Opera)/([\d.]+)")),
    ("Chrome", re.compile(r"Chrome/([\d.]+)")),
    ("Firefox", re.compile(r"Firefox/([\d.]+)")),
    ("Safari", re.compile(r"Version/([\d.]+).*Safari")),
    ("IE", re.compile(r"MSIE ([\d.]+)")),
]

_OSES: list[tuple[str, re.Pattern[str]]] = [
    ("Windows", re.compile(r"Windows NT [\d.]+")),
    ("Android", re.compile(r"Android(?: ([\d.]+))?")),
    ("iOS", re.compile(r"(?:iPhone|iPad|iPod)")),
    ("macOS", re.compile(r"Mac OS X ([\d_.]+)")),
    ("Linux", re.compile(r"Linux")),
]

_DOWNLOADERS: list[tuple[str, re.Pattern[str]]] = [
    ("IDM", re.compile(r"Internet Download Manager|IDM/", re.I)),
    ("迅雷", re.compile(r"Thunder|XLLed|XL/([\d.]+)", re.I)),
    ("aria2", re.compile(r"aria2", re.I)),
    ("wget", re.compile(r"Wget", re.I)),
    ("curl", re.compile(r"curl", re.I)),
    ("Motrix", re.compile(r"Motrix", re.I)),
    ("Free Download Manager", re.compile(r"FDM", re.I)),
]


def parse_user_agent(ua: str | None) -> tuple[str, str, str]:
    """返回 ``(browser, os_name, device)``。"""
    if not ua:
        return ("未知", "未知", "未知")

    device = "桌面端"
    if re.search(r"Mobile|iPhone|Android.*Mobile", ua, re.I):
        device = "移动端"
    elif re.search(r"iPad|Tablet|Android(?!.*Mobile)", ua, re.I):
        device = "平板"

    browser = "其它"
    for name, pattern in _DOWNLOADERS:
        if pattern.search(ua):
            browser = f"{name}（下载器）"
            break
    else:
        for name, pattern in _BROWSERS:
            match = pattern.search(ua)
            if match:
                browser = f"{name} {match.group(1).split('.')[0]}"
                break

    os_name = "其它"
    for name, pattern in _OSES:
        match = pattern.search(ua)
        if match:
            version = match.group(1) if match.groups() else None
            os_name = f"{name} {version.replace('_', '.')}" if version else name
            break

    return (browser[:64], os_name[:64], device)


# ------------------------------------------------------------------ 写入


async def log_activity(
    *,
    action: str,
    user: User | None = None,
    request: Request | None = None,
    status: str = "success",
    username: str | None = None,
    ip: str | None = None,
    ua: str | None = None,
    target_type: str | None = None,
    target_id: str | int | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    if request is not None:
        ip = ip or client_ip(request)
        ua = ua or user_agent(request)

    browser, os_name, device = parse_user_agent(ua)

    record = ActivityLog(
        user_id=user.id if user is not None else None,
        username=(user.username if user is not None else username) or "anonymous",
        action=action,
        status=status,
        ip_address=ip,
        user_agent=ua,
        browser=browser,
        os_name=os_name,
        device=device,
        method=request.method if request is not None else None,
        path=(request.url.path[:512] if request is not None else None),
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        detail=json.dumps(detail, ensure_ascii=False) if detail else None,
    )

    # 独立会话，保证审计记录不受主事务回滚影响
    async with SessionLocal() as session:
        session.add(record)
        await session.commit()
