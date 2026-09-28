"""旗帜数据、精灵图与搜索的单测（零 Qt 依赖，无显示器也能跑）。

用法：``python tests/test_flags.py``；装了 pytest 也可以 ``pytest tests``。

覆盖：
    数据表     —— 数量、代码唯一且与字符自洽、关键词规范、索引自洽
    精灵图     —— 文件在、尺寸与行列数一致、每格都在图内（PNG 头自己解析，不依赖 Qt）
    搜索       —— 中文名/英文名/国家代码/表情本身
    与表情层一致 —— flags 分类进了 emoji 的分类表与搜索，旗子与普通表情互不干扰
"""
from __future__ import annotations

import os
import re
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mchat.ui import emoji as E                                          # noqa: E402
from mchat.ui import flags as F                                          # noqa: E402

RI_A, RI_Z = 0x1F1E6, 0x1F1FF
ENGLAND = "🏴󠁧󠁢󠁥󠁮󠁧󠁿"


def png_size(path: str) -> tuple[int, int]:
    """读 PNG 头拿宽高（IHDR 固定在第 16 字节起，8 字节宽度 + 8 字节高度）。"""
    with open(path, "rb") as fh:
        head = fh.read(24)
    assert head[:8] == b"\x89PNG\r\n\x1a\n", "不是 PNG"
    width, height = struct.unpack(">II", head[16:24])
    return width, height


# ---------------------------------------------------------------------------
# 数据表
# ---------------------------------------------------------------------------
def test_flag_count():
    """259 个国家/地区旗（两字母序列）+ 10 面特殊旗（彩虹、海盗、英格兰…）。"""
    assert len(F.FLAGS) == 269, len(F.FLAGS)
    pairs = [1 for _c, code, _w in F.FLAGS if code]
    assert len(pairs) == 259, len(pairs)
    assert len(F.FLAG_CHARS) == len(set(F.FLAG_CHARS)) == len(F.FLAGS)


def test_pair_codes_match_their_chars():
    """两字母旗的代码必须和字符自洽：🇨🇳 的两个 regional indicator 就是 C、N。"""
    for char, code, _words in F.FLAGS:
        if not code:
            assert any(ord(c) > 0xFFFF for c in char) or char in "🏁🚩", char
            continue
        assert len(code) == 2 and code.isalpha() and code.islower(), (char, code)
        letters = "".join(chr(ord(c) - RI_A + ord("a")) for c in char)
        assert letters == code, (char, code, letters)
        assert all(RI_A <= ord(c) <= RI_Z for c in char), char


def test_keywords_are_clean():
    """每面旗都要有关键词；关键词串用 "|" 分隔（这样短语不会被空格打散）。"""
    for char, _code, words in F.FLAGS:
        assert words.strip(), char
        pieces = words.split(F.SEPARATOR)
        assert pieces and all(p.strip() for p in pieces), (char, words)
        assert "  " not in words, (char, words)          # 空白已折叠成单个空格
        for piece in pieces:
            assert piece == piece.strip() and piece == piece.lower(), (char, piece)
            assert any(ch.isalnum() for ch in piece), (char, piece)
            assert not re.search(r"[()（）\[\]]", piece), (char, piece)


def test_index_is_consistent():
    for position, (char, _code, _words) in enumerate(F.FLAGS):
        assert F.INDEX[char] == position
        assert F.is_flag(char)
        assert not F.is_flag("😀")
        assert F.keywords(char) == tuple(w for w in F.FLAGS[position][2]
                                         .split(F.SEPARATOR) if w)
        assert F.code_of(char) == F.FLAGS[position][1]


def test_core_countries_present():
    codes = {code for _c, code, _w in F.FLAGS}
    for code in ("cn", "hk", "mo", "tw", "jp", "us", "gb", "de", "fr", "ru",
                 "un", "eu", "xk"):
        assert code in codes, code


# ---------------------------------------------------------------------------
# 精灵图
# ---------------------------------------------------------------------------
def test_sprite_exists_and_matches_grid():
    """精灵图尺寸 = 列数 × 行数 × 格边长，且行数够放所有旗子。"""
    assert F.sprite_exists(), f"缺素材：{F.sprite_path()}（跑 tools/build_flags.py 生成）"
    width, height = png_size(F.sprite_path())
    rows = (len(F.FLAGS) + F.COLS - 1) // F.COLS
    assert (width, height) == (F.COLS * F.CELL, rows * F.CELL), (width, height)
    assert os.path.getsize(F.sprite_path()) > 20 * 1024, "精灵图太小，像是坏的"


def test_every_cell_inside_sprite():
    width, height = png_size(F.sprite_path())
    for index in range(len(F.FLAGS)):
        x, y, w, h = F.cell_rect(index)
        assert x >= 0 and y >= 0 and x + w <= width and y + h <= height, index
        assert (w, h) == (F.CELL, F.CELL), index


# ---------------------------------------------------------------------------
# 逐张 PNG（消息文本里的 <img> 只能按 URL 取"一个文件"）
# ---------------------------------------------------------------------------
def test_png_dir_is_complete():
    directory = F.png_dir()
    assert os.path.isdir(directory), f"缺素材目录：{directory}（跑 tools/build_flags.py）"
    names = sorted(n for n in os.listdir(directory) if n.endswith(".png"))
    assert len(names) == len(F.FLAGS), (len(names), len(F.FLAGS))
    assert names[0] == "000.png", names[0]
    assert names[-1] == f"{len(F.FLAGS) - 1:03d}.png", names[-1]
    width, height = png_size(os.path.join(directory, names[0]))
    assert (width, height) == (F.CELL, F.CELL), (width, height)
    total = sum(os.path.getsize(os.path.join(directory, n)) for n in names)
    assert total > 100 * 1024, total


def test_png_path_url_roundtrip():
    """path/url/反查三者要对得上：输入框取文本时全靠 char_for_url 还原字符。"""
    for char in ("🇨🇳", "🇯🇵", "🇺🇸", ENGLAND, "🏳️‍🌈", "🇺🇳"):
        path = F.png_path(char)
        url = F.png_url(char)
        assert path.endswith(f"{F.INDEX[char]:03d}.png"), path
        assert url.startswith("file:///"), url
        assert "%3A" not in url and " " not in url, url      # 盘符冒号不该被编码
        assert F.char_for_url(url) == char, (url, F.char_for_url(url))
        assert F.png_exists(char)


def test_png_lookup_is_safe_for_non_flags():
    assert F.png_path("😀") == "" and F.png_url("😀") == ""
    assert not F.png_exists("😀")
    assert F.char_for_url("file:///x/notanumber.png") == ""
    assert F.char_for_url(f"file:///x/{len(F.FLAGS)}.png") == ""   # 越界要安全
    assert F.char_for_url("") == ""


# ---------------------------------------------------------------------------
# 搜索
# ---------------------------------------------------------------------------
def test_search_by_chinese_name():
    assert F.search("中国")[:1] == ["🇨🇳"]
    assert "🇭🇰" in F.search("香港")
    assert F.search("日本") == ["🇯🇵"]


def test_search_by_english_name_and_code():
    assert F.search("japan") == ["🇯🇵"]
    assert F.search("cn")[:1] == ["🇨🇳"]
    assert "🇺🇸" in F.search("united states")
    assert F.search("scotland") == ["🏴󠁧󠁢󠁳󠁣󠁴󠁿"]


def test_search_by_flag_char_itself():
    assert F.search("🇨🇳") == ["🇨🇳"]


def test_search_empty_and_miss():
    assert F.search("") == []
    assert F.search("这个词肯定搜不到") == []


def test_search_limit():
    assert len(F.search("island", limit=3)) == 3


# ---------------------------------------------------------------------------
# 与表情层一致
# ---------------------------------------------------------------------------
def test_flags_are_a_category_in_emoji_layer():
    keys = [key for key, _label, _icon in E.CATEGORIES]
    assert E.FLAGS in keys and keys[-1] == E.FLAGS
    assert E.label_of(E.FLAGS) == "旗帜"
    assert E.item_count(E.FLAGS) == len(F.FLAGS)
    assert E.is_flag("🇨🇳") and not E.is_flag("😀")


def test_emoji_search_reaches_flags():
    """面板搜索走的是 emoji.search，旗帜必须也在里面（中文/英文都能命中）。"""
    assert "🇨🇳" in E.search("中国")
    assert "🇯🇵" in E.search("japan")
    assert "🇨🇳" in E.ALL_EMOJI and "😀" in E.ALL_EMOJI
    assert E.total_count() >= 400 + len(F.FLAGS)
    assert E.keywords("🇨🇳")          # 面板用关键词拼 flag 的 tooltip


def test_flag_does_not_disturb_text_categories():
    """加旗帜不该污染文字表情的分类数量（回归：曾把 FLAGS 混进 RAW 的键集合）。"""
    assert set(E.SEARCH_ORDER) == set(E.RAW) | {E.FLAGS}
    assert E.item_count(E.FACES) == 93, E.item_count(E.FACES)
    assert "🇨🇳" not in E.items(E.FACES)


# ---------------------------------------------------------------------------
# 打包（PyInstaller）
# ---------------------------------------------------------------------------
def test_assets_dir_follows_the_frozen_bundle():
    """冻结后素材在 sys._MEIPASS 下取：源码树那份在 exe 里不存在。

    回归：`pyinstaller -F -w main.py` 打出来的 exe 里旗帜全变成 "CN" —— 素材没打进去，
    而路径又是按源码树算的，两边凑一起就整体退化成纯文本。
    """
    import tempfile
    real_get = getattr(sys, "_MEIPASS", None)
    with tempfile.TemporaryDirectory() as tmp:
        frozen = os.path.join(tmp, "mchat", "assets")       # 解包目录里的层级
        os.makedirs(os.path.join(frozen, F.PNG_DIRNAME))
        with open(os.path.join(frozen, F.SPRITE_NAME), "wb") as fh:
            fh.write(b"PNG")                            # 内容不重要，只看路径对不对
        sys._MEIPASS = tmp
        try:
            assert F.sprite_path() == os.path.join(frozen, F.SPRITE_NAME), F.sprite_path()
            assert F.png_dir() == os.path.join(frozen, F.PNG_DIRNAME), F.png_dir()
            assert F.sprite_exists()
        finally:
            if real_get is None:
                del sys._MEIPASS
            else:
                sys._MEIPASS = real_get
    # 解包目录里没有 mchat/assets 时（比如源码直接跑）落回源码树
    sys._MEIPASS = tempfile.gettempdir()
    try:
        assert F.sprite_path().endswith(os.path.join("mchat", "assets", F.SPRITE_NAME)), \
            F.sprite_path()
        assert F.sprite_exists()
    finally:
        if real_get is None:
            del sys._MEIPASS
        else:
            sys._MEIPASS = real_get


def test_asset_paths_are_absolute():
    """素材路径必须是绝对路径：相对路径拼进 file:/// 会变成 "file:///mchat/…"（根目录），
    富文本里的旗帜就成了破图标 —— 而面板（走 QPixmap，按相对路径也能读到）却看不出来。"""
    for path in (F.sprite_path(), F.png_dir(), F.png_path("🇨🇳")):
        assert os.path.isabs(path), path
    url = F.png_url("🇨🇳")
    assert url.startswith("file:///") and ":/" in url[8:], url      # file:///E:/Codes/…


def test_spec_bundles_the_assets():
    """main.spec 必须带 datas —— PyInstaller 用 `-F -w main.py` 会把 spec 重写成 datas=[]。"""
    spec = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "main.spec")
    assert os.path.isfile(spec), f"缺打包配置：{spec}"
    with open(spec, encoding="utf-8") as fh:
        text = fh.read()
    assert "mchat/assets" in text, "main.spec 没带旗帜素材，打出来的 exe 旗帜会变国家代码"
    assert "console=False" in text, "spec 应保持无控制台（等价 -w）"


# ---------------------------------------------------------------------------
# 自写 runner：不装 pytest 也能一条命令跑完
# ---------------------------------------------------------------------------
def _run_all() -> int:
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    failed = 0
    for name, func in tests:
        try:
            func()
            print(f"  ok    {name}")
        except Exception as exc:                       # noqa: BLE001
            failed += 1
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
