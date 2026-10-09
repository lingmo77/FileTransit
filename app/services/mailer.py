"""邮件发送（SMTP）。

配置来自数据库中的站点设置，支持 无加密 / SSL / STARTTLS 三种方式。
``smtplib`` 是阻塞的，因此发送动作放到线程池执行，不阻塞事件循环。
"""

from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr

from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from ..core.templating import templates
from . import settings_service


class MailError(Exception):
    """发送失败，message 直接展示给管理员。"""


class MailNotConfigured(MailError):
    pass


@dataclass(slots=True)
class SmtpConfig:
    host: str
    port: int
    encryption: str  # none | ssl | starttls
    username: str
    password: str
    from_address: str
    from_name: str
    timeout: int


async def load_config(db: AsyncSession) -> SmtpConfig:
    host = (await settings_service.get_str(db, "email.smtp_host")).strip()
    from_address = (await settings_service.get_str(db, "email.from_address")).strip()
    if not host:
        raise MailNotConfigured("尚未配置 SMTP 服务器地址，请先在管理后台完成邮件配置")
    if not from_address:
        raise MailNotConfigured("尚未配置发件人邮箱地址，请先在管理后台完成邮件配置")

    username = (await settings_service.get_str(db, "email.username")).strip()
    password = settings_service.decrypt_secret(
        await settings_service.get_str(db, "email.password_enc")
    )

    return SmtpConfig(
        host=host,
        port=await settings_service.get_int(db, "email.smtp_port", 465),
        encryption=(await settings_service.get_str(db, "email.encryption", "ssl")).lower(),
        username=username,
        password=password,
        from_address=from_address,
        from_name=(await settings_service.get_str(db, "email.from_name")).strip()
        or (await settings_service.get_str(db, "site.name", "文件中转站")),
        timeout=await settings_service.get_int(db, "email.timeout", 20),
    )


def _render(name: str, **context: object) -> str:
    return templates.get_template(name).render(**context)


def _build_message(
    cfg: SmtpConfig, *, to: str, subject: str, html: str, text: str
) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((cfg.from_name, cfg.from_address))
    message["To"] = to
    message.set_content(text)
    message.add_alternative(html, subtype="html")
    return message


def _send_sync(cfg: SmtpConfig, message: EmailMessage) -> None:
    try:
        if cfg.encryption == "ssl":
            with smtplib.SMTP_SSL(
                cfg.host, cfg.port, timeout=cfg.timeout, context=ssl.create_default_context()
            ) as server:
                server.ehlo()
                if cfg.username:
                    server.login(cfg.username, cfg.password)
                server.send_message(message)
        else:
            with smtplib.SMTP(cfg.host, cfg.port, timeout=cfg.timeout) as server:
                server.ehlo()
                if cfg.encryption == "starttls":
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                if cfg.username:
                    server.login(cfg.username, cfg.password)
                server.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        raise MailError(f"SMTP 认证失败：{exc.smtp_error!r}（请检查用户名与密码/授权码）") from exc
    except smtplib.SMTPException as exc:
        raise MailError(f"SMTP 错误：{exc}") from exc
    except (OSError, ssl.SSLError) as exc:
        raise MailError(f"无法连接 SMTP 服务器：{exc}") from exc


async def send_message(
    db: AsyncSession, *, to: str, subject: str, html: str, text: str
) -> None:
    cfg = await load_config(db)
    message = _build_message(cfg, to=to, subject=subject, html=html, text=text)
    await run_in_threadpool(_send_sync, cfg, message)


# ------------------------------------------------------------------ 业务邮件


async def send_verification_email(
    db: AsyncSession, *, to: str, username: str, link: str, ttl_minutes: int
) -> None:
    site_name = await settings_service.get_str(db, "site.name", "文件中转站")
    html = _render(
        "email/verify.html",
        site_name=site_name,
        username=username,
        link=link,
        ttl_minutes=ttl_minutes,
    )
    text = _render(
        "email/verify.txt",
        site_name=site_name,
        username=username,
        link=link,
        ttl_minutes=ttl_minutes,
    )
    await send_message(db, to=to, subject=f"【{site_name}】邮箱验证", html=html, text=text)


async def send_password_reset_email(
    db: AsyncSession, *, to: str, username: str, link: str, ttl_minutes: int
) -> None:
    site_name = await settings_service.get_str(db, "site.name", "文件中转站")
    html = _render(
        "email/reset.html",
        site_name=site_name,
        username=username,
        link=link,
        ttl_minutes=ttl_minutes,
    )
    text = _render(
        "email/reset.txt",
        site_name=site_name,
        username=username,
        link=link,
        ttl_minutes=ttl_minutes,
    )
    await send_message(db, to=to, subject=f"【{site_name}】重置密码", html=html, text=text)


async def send_test_email(db: AsyncSession, *, to: str) -> None:
    site_name = await settings_service.get_str(db, "site.name", "文件中转站")
    html = _render("email/test.html", site_name=site_name)
    text = f"这是一封来自「{site_name}」的测试邮件。\n\n如果你收到它，说明 SMTP 配置正确。"
    await send_message(db, to=to, subject=f"【{site_name}】SMTP 配置测试", html=html, text=text)
