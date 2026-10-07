"""给图片查看器拍亮/暗两张截图（人工复核用，不是用例）。

用法：``.venv/Scripts/python.exe tools/shoot_image_view.py``
产物：``artifacts/image-viewer-light.png`` / ``artifacts/image-viewer-dark.png``
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6.QtWidgets import QApplication                              # noqa: E402

from mchat.core import assets as core_assets                          # noqa: E402
from mchat.ui import theme as theme_module                            # noqa: E402
from mchat.ui.image_view import ImageViewerDialog                     # noqa: E402

ARTIFACTS = os.path.join(ROOT, "artifacts")


def main() -> int:
    app = QApplication.instance() or QApplication([])
    os.makedirs(ARTIFACTS, exist_ok=True)
    folder = tempfile.mkdtemp(prefix="mchat-view-shot-")
    try:
        source = core_assets.demo_path(core_assets.DEMO_PHOTO)
        first = os.path.join(folder, "配色预览.png")
        second = os.path.join(folder, "配色预览（第二张）.png")
        shutil.copyfile(source, first)
        shutil.copyfile(source, second)

        viewer = ImageViewerDialog()
        viewer.set_images([("m_1", first, "配色预览.png"),
                           ("m_2", second, "配色预览（第二张）.png"),
                           ("m_3", os.path.join(folder, "还没下完.png"), "还没下完.png")],
                          0)
        viewer.resize(1000, 700)
        viewer.show()
        for theme, name in ((theme_module.LIGHT, "light"), (theme_module.DARK, "dark")):
            viewer.apply_palette(theme)
            viewer.fit_to_window()
            deadline = time.time() + 0.5
            while time.time() < deadline:
                app.processEvents()
                time.sleep(0.01)
            path = os.path.join(ARTIFACTS, f"image-viewer-{name}.png")
            viewer.grab().save(path)
            print("已保存", path)
        viewer.close()
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
