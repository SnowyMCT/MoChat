"""真机启动验证 + 截图：登录窗口 → 注册 → 空主窗口 → 新建会话窗口。

跑法（自己会起一个真服务端）：
    .venv/Scripts/python.exe tools/probe_online.py
截图落在 artifacts/ 下，供人工/视觉复核新界面（登录、空列表、新建会话）。
"""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from PyQt6.QtCore import QSettings, Qt, QTimer          # noqa: E402
from PyQt6.QtWidgets import QApplication                # noqa: E402

from mchat.core.api import ApiClient                    # noqa: E402
from mchat.core.db import ChatLog                       # noqa: E402
from mchat.core.session import Session, log_path_for    # noqa: E402
from mchat.ui.login_window import LoginWindow           # noqa: E402
from mchat.ui.main_window import MainWindow             # noqa: E402
from server_fixture import running_server               # noqa: E402

ART = os.path.join(ROOT, "artifacts")
PASSWORD = "secret123"


def pump(app, ms: int = 2500) -> None:
    """真推进事件循环（要 sleep，否则 200 次 processEvents 只花几毫秒，
    后台线程根本没机会连上服务端）。"""
    import time as _t
    end = _t.time() + ms / 1000.0
    while _t.time() < end:
        app.processEvents()
        _t.sleep(0.01)


def shoot(widget, name: str) -> str:
    path = os.path.join(ART, name)
    widget.grab().save(path)
    print("  shot ", path)
    return path


def main() -> int:
    os.makedirs(ART, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    settings = QSettings("MChatProbe", "MChatProbe")
    settings.clear()

    with running_server() as server:
        # ---- 1. 登录窗口（亮 / 暗）----
        # 账号缓存也指向临时目录：探针绝不读也不写用户真实的 %APPDATA%
        from mchat.core.session import SessionStore
        folder = tempfile.mkdtemp(prefix="mchat-probe-")
        login = LoginWindow(session_store=SessionStore(os.path.join(folder, "account.json")),
                            api_factory=ApiClient)
        login.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        login.server_field.setText(server.base_url)
        login.resize(460, 640)
        login.show()
        for theme in ("亮色", "暗色"):
            login.apply_theme(theme)
            pump(app, 400)
            shoot(login, f"login_{'light' if theme == '亮色' else 'dark'}.png")
        # 状态行也要看一眼（错误提示）
        login.set_mode("注册")
        login.username_field.setText("probe_alice")
        login.password_field.setText(PASSWORD)
        login.confirm_field.setText(PASSWORD)
        login.display_field.setText("爱丽丝")
        login.apply_theme("亮色")
        assert login.submit() is True
        got = []
        login.loggedIn.connect(lambda session: got.append(session))
        deadline = 0
        while not got and deadline < 300:
            app.processEvents()
            deadline += 1
            import time as _t
            _t.sleep(0.02)
        if not got:
            print("FAIL 登录没成功：", login.status_text())
            return 1
        session = got[0]
        print("  ok    登录成功：", session.label, "|", login.status_text())
        login.hide()

        # ---- 2. 主窗口：空会话起步 ----
        window = MainWindow(session=session,
                            api=ApiClient(session.server, session.token),
                            log=ChatLog(log_path_for(session, folder)),
                            settings=settings)
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        window.show()
        pump(app, 3000)
        print("  ok    空会话列表：", len(window.store.conversations()), "个会话 |",
              window.connection_text())
        assert len(window.store.conversations()) == 0, "新账号不该有会话"
        assert "已连接" in window.connection_text(), window.connection_text()
        shoot(window, "online_empty_light.png")
        window.set_theme("暗色")
        pump(app, 600)
        shoot(window, "online_empty_dark.png")
        window.set_theme("亮色")
        pump(app, 600)

        # ---- 3. 新建会话窗口（搜人 + 建群）----
        other = ApiClient(server.base_url)
        other.register("probe_bob", PASSWORD, "鲍勃")
        dialog = window.open_new_chat()
        dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        dialog.show()
        dialog.set_mode("找人聊天")
        dialog.set_keyword("probe_bob")
        dialog.run_search()
        pump(app, 2000)
        print("  ok    搜到人：", list(dialog.rows()), "|", dialog.status_text())
        shoot(dialog, "online_new_chat.png")
        dialog.set_mode("新建群聊")
        dialog.title_field.setText("评审组")
        dialog.toggle_member("probe_bob")
        pump(app, 300)
        shoot(dialog, "online_new_group.png")
        print("  ok    已选成员：", dialog.selected_members())
        # 真的建一个群，验证界面路径能拿到会话
        assert dialog.create_group() is True
        pump(app, 2500)
        names = [c.title for c in window.store.conversations()]
        print("  ok    建群后会话：", names)
        assert any("评审组" in name for name in names), names
        shoot(window, "online_with_group.png")

        # ---- 4. 会话内置顶（"双方可见"）：横幅要真的出来 ----
        group_id = [c.id for c in window.store.conversations()
                    if "评审组" in c.title][0]
        window.select_conversation(group_id)
        pump(app, 1500)
        window.chat_view.set_input_text("这条要置顶：周四下午两点，会议室 3 号")
        assert window.send_message("这条要置顶：周四下午两点，会议室 3 号") is True
        pump(app, 1500)
        pinned_id = [m for m in window.store.get(group_id).messages if m.outgoing][-1].id
        assert window.toggle_pin_message(pinned_id, for_both=True) is True
        pump(app, 1200)
        pin = window.store.pin_ref(group_id, pinned_id)
        assert pin is not None and pin.for_both, pin
        assert window.chat_view.pinned_message_id() == pinned_id
        print("  ok    双方置顶横幅：", window.chat_view.pin_banner.text(),
              "|", window.chat_view.pin_banner.scope_text())
        shoot(window, "online_pinned.png")
        dialog.hide()
        window.shutdown()
        window.hide()
        app.processEvents()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
