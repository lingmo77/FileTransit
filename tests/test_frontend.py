"""前端约定的回归测试。

取消上传、站内确认框、上传速度这些交互要真实浏览器才能完整跑通，
ASGI 测试覆盖不到；但「东西还在、而且真的发到了浏览器」这一层必须钉住，
否则以后重构 JS 时很容易悄悄退回去。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"
TEMPLATES = ROOT / "app" / "templates"


def _code_lines(text: str) -> str:
    """去掉行注释，免得把注释里提到的 ``window.confirm`` 当成真调用。"""
    return "\n".join(
        line for line in text.splitlines() if not line.strip().startswith(("//", "*", "/*"))
    )


def test_no_native_confirm_left():
    """删除类操作一律走站内弹窗，不许退回 ``window.confirm``。

    原生 confirm 是浏览器**进程级**对话框：样式不可控、移动端会变成系统提示，
    而且是同步阻塞的——弹着的时候整个页面都不刷新，上传进度条也停住。
    """
    offenders = []
    for path in sorted([*STATIC.rglob("*.js"), *TEMPLATES.rglob("*.html")]):
        if "window.confirm(" in _code_lines(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, "仍有原生 confirm 调用: " + ", ".join(offenders)


async def test_confirm_dialog_is_actually_served(new_client):
    """站内确认框要真的能取到——只写在源码里不算数（静态目录挂错就白搭）。"""
    guest = await new_client()
    response = await guest.get("/static/js/app.js")
    assert response.status_code == 200
    assert "confirm: function" in response.text
    assert "modal-backdrop" in response.text


async def test_upload_script_ships_cancel_and_speed(new_client):
    """上传脚本得能取消、得报速度，验的是浏览器实际拿到的那一份。"""
    guest = await new_client()
    response = await guest.get("/static/js/upload.js")
    assert response.status_code == 200

    script = response.text
    assert "xhr.abort()" in script, "取消上传没有真正调用 xhr.abort()"
    assert "formatDuration" in script, "缺少「剩余时间」的格式化"
    assert "formatSize(speed)" in script, "缺少上传速度显示"
    # 只改文案不接 abort 的话，点了取消按钮什么都不会发生
    assert "upload-dismiss" in script, "取消按钮没有绑定到上传行上"


async def test_every_referenced_static_asset_resolves(api, user):
    """页面上引用的每个 ``/static/...`` 都要真的取得到（改名/改路径后立刻发现）。"""
    page = await api.get("/dashboard")
    assert page.status_code == 200

    urls = sorted(set(re.findall(r'["\'](/static/[^"\']+)["\']', page.text)))
    assert urls, "上传页一个静态资源都没引用？"

    for url in urls:
        # 带 ?v= 的查询串不影响 StaticFiles 按路径匹配
        assert (await api.get(url)).status_code == 200, f"取不到 {url}"
