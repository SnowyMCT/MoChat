"""打包自检：在**冻结后的 exe 里**验证旗帜素材都在、并且真的画成了图。

为什么要这么个东西
------------------
旗帜是**图片素材**，只能靠打包时带进去（见 main.spec 的 datas）。打包方式一变
（例如回头顺手用 `pyinstaller -F -w main.py`）素材就悄悄丢了 —— 表现是界面里
旗帜全变成 "CN" "JP"，而**程序本身照样启动得好好的**，所以"能打开"不算验证。
这个探针把三件事依次钉死，每步都打印证据：

    素材找到了吗（路径 + 数量）→ 图标切出来了吗（相邻两面旗的像素得不一样）
    → 画到界面上了吗（旗帜在富文本里要真的是 <img>，并留一张截图给人看）

用法：先在**与主程序同一套 datas** 下打成一次性 exe，跑它、看退出码：

    pyinstaller tools/frozen_check.py --onefile --console --noconfirm \
        --paths . --add-data "mchat/assets;mchat/assets" --name frozen_check \
        --specpath build --workpath build/probe --distpath dist
    dist/frozen_check.exe                     # 退出码 0 = 素材链路完好
    dist/frozen_check.exe --shot out.png      # 顺手存一张截图

不打包直接跑也行（`.venv/Scripts/python.exe tools/frozen_check.py`），
那验证的是源码树里那份素材 —— 出问题时两边各跑一次，就能判断是"素材坏了"
还是"没打进去"。

不弹窗、不抢焦点：窗口用 WA_DontShowOnScreen，跑完就关。
"""
from __future__ import annotations

import argparse
import os
import sys
from urllib.parse import unquote
from urllib.request import url2pathname

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if not getattr(sys, "frozen", False):          # 源码直接跑时才需要补 sys.path
    sys.path.insert(0, ROOT)

# 冻结的 exe 在管道/双击下把 stdout 当 GBK，打印旗帜字符会 UnicodeEncodeError 直接崩，
# 所以一开始就把输出切到 UTF-8（不可重定向时忽略）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):       # pragma: no cover - 换不了就凑合
        pass

from PyQt6.QtCore import QSize, Qt                                          # noqa: E402
from PyQt6.QtGui import QImage                                               # noqa: E402
from PyQt6.QtWidgets import QApplication, QLabel                             # noqa: E402

from mchat.ui import emoji as emoji_data                                     # noqa: E402
from mchat.ui import flag_text                                               # noqa: E402
from mchat.ui import flags as flag_data                                      # noqa: E402
from mchat.ui.emoji_panel import EmojiPicker                                 # noqa: E402

SAMPLES = ("🇨🇳", "🇯🇵", "🇺🇸", "🇬🇧", "🏴󠁧󠁢󠁥󠁮󠁧󠁿")          # 含一个 tag 序列（英格兰）
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)
    return ok


def colorful_pixels(image: QImage) -> int:
    """数\"明显有颜色\"的像素（旗帜是彩色的；退化成单色/豆腐块时接近 0）。"""
    count = 0
    for y in range(image.height()):
        for x in range(image.width()):
            pixel = image.pixelColor(x, y)
            if max(pixel.red(), pixel.green(), pixel.blue()) \
                    - min(pixel.red(), pixel.green(), pixel.blue()) > 40:
                count += 1
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="冻结版旗帜素材自检")
    parser.add_argument("--shot", default=os.path.join(os.getcwd(), "frozen_flags.png"),
                        help="把旗帜渲染结果存成 PNG（默认当前目录；冻结后 ROOT 是解包目录，"
                             "别拿它当基准）")
    args = parser.parse_args(argv)

    print("[环境]")
    print(f"  frozen={getattr(sys, 'frozen', False)}  "
          f"_MEIPASS={getattr(sys, '_MEIPASS', '(无)')}")
    print(f"  flags.py={flag_data.__file__}")

    print("[1/4] 素材路径")
    assets = os.path.dirname(flag_data.sprite_path())
    check("素材目录不是空的", os.path.isdir(assets), assets)
    check("精灵图 flags.png 在", flag_data.sprite_exists(), flag_data.sprite_path())
    pngs = [c for c in flag_data.FLAG_CHARS if flag_data.png_exists(c)]
    check("逐张 PNG 全齐", len(pngs) == len(flag_data.FLAG_CHARS),
          f"{len(pngs)}/{len(flag_data.FLAG_CHARS)}")
    check("flag_text 认为素材可用", flag_text.assets_available())

    print("[2/4] 富文本里的旗帜是 <img>（消息气泡/输入框走这条路）")
    for char in SAMPLES[:3]:
        html, rich = flag_text.to_display(f"国旗 {char} 到")
        tag = flag_text.to_html(char, size=18)
        url = tag.split('src="', 1)[1].split('"', 1)[0] if 'src="' in tag else ""
        path = url2pathname(unquote(url[len("file:///"):])) if url else ""
        check(f"{char} 走富文本且图片文件真实存在",
              rich and f"<img" in html and os.path.isfile(path),
              path or "(没有 src)")

    print("[3/4] 界面：面板里的旗帜图标 + 气泡里的渲染")
    app = QApplication.instance() or QApplication([])
    picker = EmojiPicker()
    picker.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)   # 不出现在屏幕上
    picker.show()
    picker.select_category(emoji_data.FLAGS)
    for _ in range(4):
        app.processEvents()
    if args.shot:                                # 面板整幅留一张给人看
        panel = os.path.splitext(args.shot)[0] + "_panel.png"
        os.makedirs(os.path.dirname(os.path.abspath(panel)), exist_ok=True)
        picker.grab().save(panel)
        print(f"  面板截图：{panel}")
    cells = {c: picker.cell_at(c) for c in SAMPLES}
    icons_ok = all(b is not None and not b.icon().isNull() for b in cells.values())
    check("面板旗帜格子用的是图标（不是退回画字符）", icons_ok)
    if icons_ok:
        left = cells["🇨🇳"].icon().pixmap(QSize(64, 64)).toImage()
        right = cells["🇯🇵"].icon().pixmap(QSize(64, 64)).toImage()
        check("相邻两面旗切出来不是同一张图（真的按槽位切图）",
              left != right and colorful_pixels(left) > 100,
              f"彩色像素 {colorful_pixels(left)}")
        check("旗帜格子里没有文字（说明没降级）",
              all(not b.text() for b in cells.values()),
              " ".join(repr(b.text()) for b in cells.values() if b.text()) or "(都为空)")

    label = QLabel()
    label.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    label.setTextFormat(Qt.TextFormat.RichText)
    label.setText(flag_text.to_html("🇨🇳 🇯🇵 🇺🇸 🇬🇧", size=32))
    label.resize(200, 48)
    label.show()
    for _ in range(4):                      # 推进事件循环，让 QTextDocument 把图画出来
        app.processEvents()
    shot = label.grab()
    image = shot.toImage()
    check("气泡/标签里旗帜真的画出来了（彩色像素 > 0）", colorful_pixels(image) > 50,
          f"{colorful_pixels(image)} 个彩色像素")
    if args.shot:
        os.makedirs(os.path.dirname(os.path.abspath(args.shot)), exist_ok=True)
        shot.save(args.shot)
        print(f"  截图：{args.shot}")

    print("[4/4] 收尾")
    label.close()
    picker.close()
    picker.deleteLater()
    app.processEvents()

    print(f"\n{'全部通过' if not FAILURES else '失败 ' + str(len(FAILURES)) + ' 项：'}")
    for name in FAILURES:
        print(f"  - {name}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
