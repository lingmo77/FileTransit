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


def test_upload_queue_has_exactly_one_reload_exit():
    """队列页只留一个刷新出口，而且它必须等上传跑完。

    回归点：删除/修改文件后直接 ``location.reload()``，浏览器会把在途的
    XHR 全部 abort——正在传的那几个当场变成「已取消」，看起来像是用户自己
    的删除操作把上传弄没了。所以这些地方一律登记到 ``refresh()``，
    由它等队列空了再刷。
    """
    code = _code_lines((STATIC / "js" / "upload.js").read_text(encoding="utf-8"))

    assert code.count("window.location.reload()") == 1, "队列页出现了第二个刷新出口"
    assert "if (reload.timer || active > 0) return;" in code, "刷新前没有「还有上传在跑」的把关"
    # 删除 / 修改文件都得走 refresh，不许自己动手 reload
    assert "refresh(900)" in code, "删除文件后没有走 refresh"
    assert "refresh(700)" in code, "修改文件后没有走 refresh"


def test_upload_queue_offers_a_retry():
    """取消 / 失败 / 卡死之后都得能重来，而不是只能把这一行删掉。"""
    code = _code_lines((STATIC / "js" / "upload.js").read_text(encoding="utf-8"))

    assert "upload-retry" in code, "队列里没有重试按钮"
    assert "row.retry.addEventListener('click', attempt)" in code, "重试按钮没接上重发"
    assert "重新上传" in code, "重试按钮没有可读的名字"
    # 卡死时要把它露出来，否则「已停顿」就成了死路
    assert "retry.hidden = false" in code, "卡死/取消/失败时没有露出重试按钮"


async def test_retry_button_can_actually_hide(new_client):
    """重试按钮的「藏起来」得真的藏得住。

    ``.icon-btn`` 是 ``display: grid``，优先级压过浏览器默认的
    ``[hidden] { display: none }``——少了那条覆盖规则，不需要重试的时候
    按钮也会一直挂在那里。
    """
    guest = await new_client()
    css = (await guest.get("/static/css/style.css")).text

    assert re.search(
        r"\.upload-item \.icon-btn\[hidden\]\s*\{[^}]*display:\s*none", css
    ), "缺少让重试按钮真正隐藏的 CSS"
    # 两个按钮成组放在最后一列，藏起一个才不会在网格里留出一道空列
    assert ".upload-actions" in css, "取消/重试按钮没有成组"


async def test_every_referenced_static_asset_resolves(api, user):
    """页面上引用的每个 ``/static/...`` 都要真的取得到（改名/改路径后立刻发现）。"""
    page = await api.get("/dashboard")
    assert page.status_code == 200

    urls = sorted(set(re.findall(r'["\'](/static/[^"\']+)["\']', page.text)))
    assert urls, "上传页一个静态资源都没引用？"

    for url in urls:
        # 带 ?v= 的查询串不影响 StaticFiles 按路径匹配
        assert (await api.get(url)).status_code == 200, f"取不到 {url}"


async def test_share_count_and_empty_state_are_rendered_together(api, user):
    """列表和空状态两个块都要在首屏渲染出来（靠 hidden 切显隐）。

    删掉卡片后要靠前端把计数和空状态当场改对——如果空状态块只在 ``total == 0``
    时才渲染，前端就没东西可显示，只能干等那次「等上传跑完再刷」的整页刷新。
    而那次刷新在队列里有卡住的上传时永远不来，计数会一直停在旧值上。
    """
    empty_page = await api.get("/dashboard")
    assert 'id="file-grid" hidden' in empty_page.text, "没文件时列表还占着地方"
    assert 'id="file-grid-empty" hidden' not in empty_page.text, "没文件时空状态被藏了"

    await api.upload("only.bin", b"x" * 32)
    page = await api.get("/dashboard")
    assert 'id="file-grid" hidden' not in page.text, "有文件时列表被藏了"
    assert 'id="file-grid-empty" hidden' in page.text, "有文件时空状态还露着"

    # 计数块带 data-total：前端只减自己那一份，不用去猜总数
    match = re.search(r'id="share-count"[^>]*data-total="(\d+)"', page.text)
    assert match, "计数块没有 data-total，前端只能去解析文本"
    assert match.group(1) == "1"


def test_delete_syncs_the_share_count_in_the_dom():
    """删除卡片后要自己把计数和空状态改对，不能只依赖那次延后的整页刷新。"""
    code = (STATIC / "js" / "upload.js").read_text(encoding="utf-8")

    assert "function syncListAfterRemoval()" in code, "没有就地更新列表的收尾函数"
    # 必须在真的摘掉卡片那一步调用（跟着淡出动画一起）
    assert re.search(r"card\.remove\(\);\s*\n\s*syncListAfterRemoval\(\);", code), (
        "摘掉卡片后没有同步计数"
    )
    assert "count.dataset.total" in code and "count.textContent" in code
    assert "grid.hidden = true" in code and "empty.hidden = false" in code


def test_hidden_file_grid_is_really_hidden():
    """``.file-grid`` 是 display:grid，比 UA 的 ``[hidden]{display:none}`` 优先级高。

    这条已经坑过两次（重试按钮、现在的列表），所以对**下发出去的** CSS 断言。
    """
    css = (STATIC / "css" / "style.css").read_text(encoding="utf-8")
    assert re.search(r"\.file-grid\[hidden\]\s*\{[^}]*display:\s*none", css), (
        "缺少让 file-grid 真正隐藏的 CSS"
    )


def test_upload_attempt_always_returns_its_slot():
    """请求没起来就失败时也要把 ``active`` 还回去。

    ``active`` 一旦多出来一个，「等队列空了再刷新」的刷新就永远排不上队——
    整页刷新从此不再发生（计数、配额、列表全停在旧值上）。
    """
    code = _code_lines((STATIC / "js" / "upload.js").read_text(encoding="utf-8"))

    assert code.count("active += 1") == 1, "计数点应该只有一个"
    assert code.count("active -= 1") == 1, "归还点应该只有一个"
    assert "function gaveUp(" in code, "缺少「没发出去也要收尾」的兜底"
    # 拼 FormData / open 和 send 两处都可能抛，都得兜住
    assert code.count("gaveUp(error);") == 2, "抛错路径没有全部兜住"
