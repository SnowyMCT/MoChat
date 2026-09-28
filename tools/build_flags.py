"""构建旗帜素材：抓 Twemoji 旗图 -> 拼成一张本地精灵图 -> 生成 flags.py。

    .venv/Scripts/python.exe tools/build_flags.py

**只有这个脚本联网**，而且只需要跑一次。跑完得到两个产物，之后程序完全离线：

    mchat/assets/flags.png      一张精灵图（16 列 × N 行，每格 64×64）—— 给面板用
    mchat/assets/flags/000.png  每面旗一张独立 PNG（64×64）—— 给消息文本里的 <img> 用
    mchat/ui/flags.py           旗帜表：字符 + 国家代码 + 中英关键词 + 图路径

为什么要两种形态
----------------
面板的格子是 QToolButton 图标，一次读盘 + 切图最快（精灵图）；而**消息文本、输入框**
是 QTextDocument 渲染的，`<img src>` 只能按 URL 取"一个文件"，喂不了精灵图里的某一格 ——
所以文本侧需要逐张 PNG（`flags/<序号>.png`，序号就是表里的槽位）。

素材来源与许可
--------------
* 旗图：Twemoji（jdecked/twemoji），**CC-BY 4.0**，需要署名 —— 见 mchat/assets/README.md。
* 国家名：Unicode CLDR 的 localenames/territories（zh / en），Unicode License。
  （注意：CLDR 的 emoji annotations 里**没有**国家旗，只有符号类表情；国家名得走地名表。）

为什么不逐张塞 PNG
------------------
一幅 16 列 × 17 行的 64px 精灵图 ≈ 一张文件、几百 KB，切图只要 `copy(rect)`；
逐张 PNG 会有 260+ 个小文件，打包、路径、加载都是负担（Telegram 自己也是精灵图）。

下载缓存放在仓库根的 ``.flag_cache/``（已 gitignore），重复跑不会重新下载。
"""
from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, ".flag_cache")
ASSETS = os.path.join(ROOT, "mchat", "assets")
TARGET_PY = os.path.join(ROOT, "mchat", "ui", "flags.py")
TARGET_PNG = os.path.join(ASSETS, "flags.png")

UA = {"User-Agent": "MChat-build/1.0 (+local asset builder)"}
TWEMOJI_TREE = "https://api.github.com/repos/jdecked/twemoji/git/trees/main?recursive=1"
TWEMOJI_RAW = "https://raw.githubusercontent.com/jdecked/twemoji/main/assets/72x72/{name}.png"
CLDR_RAW = ("https://raw.githubusercontent.com/unicode-org/cldr-json/main/"
            "cldr-json/cldr-localenames-full/main/{lang}/territories.json")

COLS = 16
CELL = 64                      # 显示尺寸 24~30px，用 64px 保留高分屏清晰度
TEXT_PX = 64                   # 逐张 PNG 的边长（文本里按 17px 显示，留足缩放余量）
PNG_DIR = "flags"              # mchat/assets/flags/
MAX_KEYWORDS = 10

# 特殊旗（非两字母序列）：黑旗 / 白旗 / 海盗 / 彩虹 / 跨性别 / 英格兰 / 苏格兰 / 威尔士
SPECIALS = [
    ("🏳️", "white flag 白旗"),
    ("🏴", "black flag 黑旗"),
    ("🏁", "chequered flag 格子旗 终点"),
    ("🚩", "triangular flag 三角旗 红旗"),
    ("🏳️‍🌈", "rainbow flag 彩虹旗 lgbt pride"),
    ("🏳️‍⚧️", "transgender flag 跨性别旗"),
    ("🏴‍☠️", "pirate flag 海盗旗 skull"),
    ("🏴󠁧󠁢󠁥󠁮󠁧󠁿", "england 英格兰"),
    ("🏴󠁧󠁢󠁳󠁣󠁴󠁿", "scotland 苏格兰"),
    ("🏴󠁧󠁢󠁷󠁬󠁳󠁿", "wales 威尔士"),
]

RI_A = 0x1F1E6                 # regional indicator A：国旗就是两个这样的字符拼起来


def fetch(url: str, *, binary: bool = False, cache_name: str | None = None):
    """下载（带本地缓存）。构建期用，程序运行时不碰网络。"""
    if cache_name:
        os.makedirs(CACHE, exist_ok=True)
        path = os.path.join(CACHE, cache_name)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            with open(path, "rb") as fh:
                data = fh.read()
            return data if binary else json.loads(data.decode("utf-8"))
    request = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read()
    if cache_name:
        with open(os.path.join(CACHE, cache_name), "wb") as fh:
            fh.write(data)
    return data if binary else json.loads(data.decode("utf-8"))


def char_of(code: str) -> str:
    """``"cn"`` -> 🇨🇳（两个 regional indicator 字符）。"""
    return "".join(chr(RI_A + ord(letter) - ord("a")) for letter in code)


def twemoji_name(char: str) -> str:
    """猜 Twemoji 的文件名：码点小写十六进制用 ``-`` 连接（如 1f1e8-1f1f3）。"""
    return "-".join(f"{ord(ch):x}" for ch in char)


def flag_items() -> list[tuple[str, str, str]]:
    """``[(字符, 国家代码, 兜底关键词), …]``：259 个两字母旗 + 特殊旗。"""
    tree = fetch(TWEMOJI_TREE)
    names = {entry["path"].rsplit("/", 1)[-1][:-4]
             for entry in tree["tree"]
             if entry["path"].startswith("assets/72x72/") and entry["path"].endswith(".png")}
    pair = re.compile(r"^(1f1e[6-9a-f]|1f1f[0-9a-f])-(1f1e[6-9a-f]|1f1f[0-9a-f])$")
    codes = sorted(m.group(0).split("-")[0] + "-" + m.group(0).split("-")[1]
                   for name in names if (m := pair.match(name)))
    items: list[tuple[str, str, str]] = []
    for slug in codes:
        # 1f1e8-1f1f3 -> 🇨🇳（先把十六进制码点还原成字符，再把两个字符映射回字母）
        char = "".join(chr(int(part, 16)) for part in slug.split("-"))
        code = "".join(chr(ord(ch) - RI_A + ord("a")) for ch in char)
        items.append((char, code, code))
    for char, fallback in SPECIALS:
        items.append((char, "", fallback))
    # 校验：每个字符在 Twemoji 里都要能找到图（带/不带 FE0F 两种写法都试）
    missing = []
    for char, _code, _fb in items:
        if twemoji_name(char) not in names and twemoji_name(
                char.replace("\ufe0f", "")) not in names:
            missing.append(char)
    if missing:
        raise SystemExit(f"Twemoji 里找不到这些旗子的图：{missing}")
    return items


def territory_names(lang: str) -> dict[str, str]:
    """CLDR 地名表：``{"CN": "中国", "UN": "联合国", …}``（304 条，含 AC/TA/XK 这类例外码）。"""
    data = fetch(CLDR_RAW.format(lang=lang), cache_name=f"cldr_territories_{lang}.json")
    table = data["main"][lang]["localeDisplayNames"]["territories"]
    return {code.upper(): name for code, name in table.items()}


def fold_accents(text: str) -> str:
    """去掉重音/弯引号：让 "cote d'ivoire"、"sao tome"、"aland" 也能搜到。"""
    decomposed = unicodedata.normalize("NFKD", text.replace("’", "'"))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def keywords_for(code: str, zh: dict[str, str], en: dict[str, str],
                 fallback: str) -> list[str]:
    """关键词 = 国家代码 + 中文名 + 英文名（含长名的分词）+ 特殊旗的手写词。

    中文名字本身没有空格，检索时按子串匹配（搜"香港"能命中"中国香港特别行政区"）；
    英文长名额外拆成单词，这样搜 "island" / "cunha" 也能命中。
    """
    words: list[str] = []

    def add(piece: str, *, from_name: bool) -> None:
        """收一个词：去首尾点、转小写，并且额外收一份"去重音"的写法。"""
        piece = piece.strip().strip(".").lower()
        for variant in (piece, fold_accents(piece)):
            core = "".join(ch for ch in variant if ch.isalnum())
            if not core:
                continue
            # 丢掉 "st." / "&" 这类碎片（中文两字词是有意义的，只过滤纯 ASCII 的短碎片）
            if from_name and variant.isascii() and len(core) < 3:
                continue
            if variant not in words:
                words.append(variant)

    def push(word: str, *, from_name: bool = True) -> None:
        """把一个名称拆成关键词：整体短语 + 按空格/连字符切的词。

        CLDR 的名字是"人类文案"，带 & . （） () 这类东西（"St. Barthélemy"、
        "科科斯（基林）群岛"），所以先去掉括号、把 "St." 写成 "saint"、
        折叠多余空白，再切词。
        """
        cleaned = re.sub(r"[()（）\[\]{}]", " ", word)
        cleaned = re.sub(r"\bst\.\s*", "saint ", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        add(cleaned, from_name=from_name)
        for piece in cleaned.split():
            add(piece, from_name=from_name)
        for piece in re.split(r"[-–—]", cleaned):
            add(piece, from_name=from_name)

    if code:
        push(code, from_name=False)                  # 国家代码永远保留（cn/us/jp…）
        push(zh.get(code.upper(), ""))
        push(en.get(code.upper(), ""))
    for word in fallback.split():
        push(word, from_name=False)                  # 特殊旗的手写词
    return words[:MAX_KEYWORDS]


def download_pngs(items) -> dict[str, bytes]:
    """把每面旗的 72×72 PNG 抓下来（缓存到 .flag_cache，失败重试一次）。"""
    def one(char: str):
        name = twemoji_name(char)
        try:
            return char, fetch(TWEMOJI_RAW.format(name=name), binary=True,
                               cache_name=f"{name}.png")
        except Exception:                      # noqa: BLE001 - 退化到去 FE0F 的写法
            name = twemoji_name(char.replace("\ufe0f", ""))
            return char, fetch(TWEMOJI_RAW.format(name=name), binary=True,
                               cache_name=f"{name}.png")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(one, [char for char, _c, _f in items]))
    return dict(results)


def build_sprite(items, pngs: dict[str, bytes]) -> None:
    """把 PNG 拼成一张 16 列的精灵图（用 Qt 画，避免额外依赖）。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QGuiApplication, QImage, QPainter

    app = QGuiApplication.instance() or QGuiApplication(["build_flags"])
    rows = (len(items) + COLS - 1) // COLS
    sheet = QImage(COLS * CELL, rows * CELL, QImage.Format.Format_ARGB32_Premultiplied)
    sheet.fill(Qt.GlobalColor.transparent)
    painter = QPainter(sheet)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    for index, (char, _code, _fb) in enumerate(items):
        tile = QImage.fromData(pngs[char])
        if tile.isNull():
            raise SystemExit(f"解码失败：{char}")
        tile = tile.scaled(CELL, CELL, Qt.AspectRatioMode.IgnoreAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)
        _outline(tile)
        painter.drawImage((index % COLS) * CELL, (index // COLS) * CELL, tile)
    painter.end()
    os.makedirs(ASSETS, exist_ok=True)
    if not sheet.save(TARGET_PNG, "PNG", 9):
        raise SystemExit(f"写不出去：{TARGET_PNG}")
    del app
    print(f"精灵图：{TARGET_PNG}  {sheet.width()}×{sheet.height()}  "
          f"{os.path.getsize(TARGET_PNG) / 1024:.0f} KB  {len(items)} 面旗")


def _outline(image) -> None:
    """给旗图描一圈极淡的边。

    Twemoji 的旗子没有描边，白底旗（日本、白旗…）贴在浅色气泡上就"糊"进背景，
    只剩一个红点。描一圈 12% 黑，浅底深底都能看清，又不至于变成"画框"。
    """
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QColor, QPainter, QPen

    painter = QPainter(image)
    pen = QPen(QColor(0, 0, 0, 32))
    pen.setWidth(1)
    pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRect(0, 0, image.width() - 1, image.height() - 1)
    painter.end()


def build_pngs(items, pngs: dict[str, bytes]) -> None:
    """再写一份逐张 PNG（序号即槽位）：富文本里的 <img src> 只能按文件取图。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QGuiApplication, QImage

    app = QGuiApplication.instance() or QGuiApplication(["build_flags"])
    directory = os.path.join(ASSETS, PNG_DIR)
    os.makedirs(directory, exist_ok=True)
    for name in os.listdir(directory):              # 表变了就重新编号，先清旧的
        if name.endswith(".png"):
            os.remove(os.path.join(directory, name))
    total = 0
    for index, (char, _code, _fb) in enumerate(items):
        tile = QImage.fromData(pngs[char]).scaled(
            TEXT_PX, TEXT_PX, Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        _outline(tile)
        path = os.path.join(directory, f"{index:03d}.png")
        if not tile.save(path, "PNG", 9):
            raise SystemExit(f"写不出去：{path}")
        total += os.path.getsize(path)
    del app
    print(f"逐张 PNG：{directory}  {len(items)} 张  {total / 1024:.0f} KB")


def write_module(items, lines: list[str]) -> None:
    with open(TARGET_PY, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(MODULE_TEMPLATE.format(rows=(len(items) + COLS - 1) // COLS,
                                        cols=COLS, cell=CELL, count=len(items),
                                        png_dir=PNG_DIR,
                                        table="\n".join(lines)))
    print(f"数据表：{TARGET_PY}  {len(items)} 行")


MODULE_TEMPLATE = '''"""旗帜数据表 + 精灵图坐标（**零 Qt 依赖**，可脱离界面单测）。

这个文件由 ``tools/build_flags.py`` 生成，别手改 —— 改了就再跑一次脚本。
（表内容变化时脚本会重下素材；下载缓存放在仓库根的 .flag_cache/。）

    flags.py    本文件：字符、国家代码、中英关键词、精灵图坐标
    assets/flags.png   一张 {cols} 列 × {rows} 行的精灵图，每格 {cell}×{cell}（面板用）
    assets/flags/*.png 逐张独立 PNG，000.png 起按槽位编号（消息文本 <img> 用）

两种形态的原因：面板格子是 QToolButton 图标，精灵图一次读盘最省；而消息文本/输入框是
QTextDocument 渲染的，`<img src>` 只能按 URL 取单个文件，喂不了精灵图里的一格。

数据来源与许可
--------------
* 旗图：Twemoji（jdecked/twemoji），CC-BY 4.0 —— 署名见 mchat/assets/README.md
* 国家名：Unicode CLDR localenames/territories（zh / en），Unicode License

为什么用精灵图而不是 Unicode 国旗字符
------------------------------------
Windows 的 Segoe UI Emoji 不画国旗，只会渲染成两个字母（"CN"）；英格兰/苏格兰/威尔士
那种 tag 序列更是几乎没字体支持。所以走图片方案：字符只当"查槽位用的 key"
（Telegram 也是这么做的，见 lib_ui 的 emoji.txt + emoji_*.webp）。
"""
from __future__ import annotations

import os
import sys
from urllib.parse import quote

COLS = {cols}
CELL = {cell}
SPRITE_NAME = "flags.png"
PNG_DIRNAME = "{png_dir}"

# (字符, 国家代码, 关键词串) —— 顺序就是精灵图里的槽位顺序
# 关键词串用 "|" 分隔（不是空格）：这样 "united states" 这种多词短语能原样保留
FLAGS: tuple[tuple[str, str, str], ...] = (
{table}
)

FLAG_CHARS: tuple[str, ...] = tuple(char for char, _code, _words in FLAGS)
INDEX: dict[str, int] = {{char: i for i, (char, _c, _w) in enumerate(FLAGS)}}


def sprite_path() -> str:
    """精灵图的绝对路径（放在 mchat/assets/ 下，随包分发）。"""
    return os.path.join(_assets_dir(), SPRITE_NAME)


def sprite_exists() -> bool:
    return os.path.isfile(sprite_path())


def _assets_dir() -> str:
    """素材目录：**打包（PyInstaller）后先看解包目录**，源码运行时才是旁边的 mchat/assets/。

    冻结后 ``__file__`` 指向 exe 内部的虚拟路径，按源码树算出来的目录是空的 ——
    于是 ``sprite_exists()/png_exists()`` 全假、旗帜退化成 "CN" 两个字母
    （典型的"开发时好好的，打包就没了"）。PyInstaller 把 datas 解到 ``sys._MEIPASS``，
    onefile 是临时目录、onedir 就是 exe 旁边，里面的层级与源码一致。
    """
    frozen_root = getattr(sys, "_MEIPASS", "")
    bundled = os.path.join(frozen_root, "mchat", "assets") if frozen_root else ""
    if bundled and os.path.isdir(bundled):
        return bundled
    # 兜底分支必须给**绝对路径**：相对路径会被拼进 file:/// URL 里，
    # 变成 "file:///mchat/assets/…"（根目录），富文本里的旗帜就全成了破图标
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "assets")


def png_dir() -> str:
    """逐张 PNG 的目录（mchat/assets/flags/）。"""
    return os.path.join(_assets_dir(), PNG_DIRNAME)


def png_path(char: str) -> str:
    """第 ``INDEX[char]`` 面旗的独立 PNG 路径（文件名就是槽位序号）。"""
    index = INDEX.get(char)
    return "" if index is None else os.path.join(png_dir(), f"{{index:03d}}.png")


def png_url(char: str) -> str:
    """给富文本 `<img src="…">` 用的 file:// URL（路径里的空格、引号都要转义）。"""
    path = png_path(char)
    if not path:
        return ""
    # 冒号要留在外面：百分号编码后的 "E%3A" 虽然也能被 QUrl 解回来，但没必要冒险
    return "file:///" + quote(path.replace(os.sep, "/").lstrip("/"), safe="/:")


def char_for_url(url: str) -> str:
    """反查：从 `<img src>` 的 URL 找回旗帜字符（输入框取文本时靠它还原）。"""
    stem = url.rsplit("/", 1)[-1].split(".")[0]
    if stem.isdigit() and int(stem) < len(FLAGS):
        return FLAGS[int(stem)][0]
    return ""


def png_exists(char: str) -> bool:
    path = png_path(char)
    return bool(path) and os.path.isfile(path)


def cell_rect(index: int) -> tuple[int, int, int, int]:
    """第 ``index`` 个旗子在精灵图里的 ``(x, y, w, h)``。"""
    return (index % COLS) * CELL, (index // COLS) * CELL, CELL, CELL


def is_flag(char: str) -> bool:
    return char in INDEX


SEPARATOR = "|"          # 关键词串的分隔符：多词短语（如 united states）原样保留


def keywords(char: str) -> tuple[str, ...]:
    index = INDEX.get(char)
    if index is None:
        return ()
    return tuple(w for w in FLAGS[index][2].split(SEPARATOR) if w)


def code_of(char: str) -> str:
    index = INDEX.get(char)
    return FLAGS[index][1] if index is not None else ""


def search(keyword: str, *, limit: int = 120) -> list[str]:
    query = keyword.strip().lower()
    if not query:
        return []
    hits = []
    for char, code, words in FLAGS:
        if (query == char or query == code
                or any(query in w for w in words.split(SEPARATOR))):
            hits.append(char)
    return hits[:limit]
'''


def main() -> int:
    items = flag_items()
    print(f"旗帜清单：{len(items)} 个（两字母旗 {sum(1 for _c, code, _f in items if code)}）")
    zh, en = territory_names("zh"), territory_names("en")
    pngs = download_pngs(items)
    print(f"图片已就绪：{len(pngs)} 张（缓存目录 {CACHE}）")

    lines = []
    for char, code, fallback in items:
        words = keywords_for(code, zh, en, fallback)
        lines.append(f'    ("{char}", "{code}", "{"|".join(words)}"),')
    write_module(items, lines)
    build_sprite(items, pngs)
    build_pngs(items, pngs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
