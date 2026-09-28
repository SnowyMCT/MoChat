"""旗帜文本渲染的单测（零 Qt 依赖）：字符 <-> 图片标记。

用法：``python tests/test_flag_text.py``；装了 pytest 也可以 ``pytest tests``。

覆盖：
    识别       —— 哪些文本含旗帜（含英格兰那种 tag 序列、连续多面旗）
    转义顺序   —— 先转义再插图：用户输入的 <b> 必须变成文字，不能被执行
    拆片       —— split_flags 的分段正好能拼回原文
    显示决策   —— to_display 只在"含旗帜且素材在"时才走富文本
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mchat.ui import flag_text                                            # noqa: E402
from mchat.ui import flags as F                                           # noqa: E402

ENGLAND = "🏴󠁧󠁢󠁥󠁮󠁧󠁿"


# ---------------------------------------------------------------------------
# 识别
# ---------------------------------------------------------------------------
def test_contains_flag():
    assert flag_text.contains_flag("🇨🇳")
    assert flag_text.contains_flag("我们在🇨🇳")
    assert flag_text.contains_flag("A🇯🇵B🇺🇸C")
    assert flag_text.contains_flag(ENGLAND)
    assert flag_text.contains_flag("彩虹🏳️‍🌈旗")
    assert not flag_text.contains_flag("没有任何旗帜")
    assert not flag_text.contains_flag("😀🎉")


def test_contains_flag_ignores_single_regional_indicator():
    """单独一个 regional indicator 不是旗帜，别误判（否则会把字母吃掉换成图）。"""
    assert not flag_text.contains_flag("\U0001F1E8")
    assert not flag_text.contains_flag("C\U0001F1E8")


# ---------------------------------------------------------------------------
# 转义顺序
# ---------------------------------------------------------------------------
def test_to_html_escapes_user_markup():
    html_text = flag_text.to_html("🇨🇳<b>粗</b>")
    assert "<b>" not in html_text
    assert "&lt;b&gt;" in html_text
    assert "file://" in html_text
    assert "😀" not in html_text                      # 普通表情原样保留（fence: 只有旗帜换图）


def test_to_html_keeps_plain_emoji_and_newlines():
    html_text = flag_text.to_html("第一行😀\n第二行")
    assert "😀" in html_text
    assert "<br>" in html_text
    assert "\n" not in html_text                      # 换行交给 <br>


def test_to_html_replaces_every_flag():
    html_text = flag_text.to_html("🇨🇳🇯🇵🇺🇸")
    assert html_text.count("<img") == 3
    assert F.png_url("🇨🇳") in html_text and F.png_url("🇯🇵") in html_text


def test_to_html_uses_longest_match():
    """英格兰是 tag 序列，必须整体匹配，不能被拆成 🏴 + 尾随 tag 字符。"""
    html_text = flag_text.to_html(ENGLAND)
    assert html_text.count("<img") == 1
    assert F.png_url(ENGLAND) in html_text


def test_to_html_has_no_raw_flag_chars_left():
    for char in ("🇨🇳", ENGLAND, "🏳️‍🌈", "🇺🇳"):
        assert char not in flag_text.to_html(f"x{char}y")


def test_to_html_sizes():
    small = flag_text.to_html("🇨🇳", size=flag_text.PREVIEW_IMAGE_SIZE)
    big = flag_text.to_html("🇨🇳")
    assert f'height="{flag_text.PREVIEW_IMAGE_SIZE}"' in small
    assert f'height="{flag_text.TEXT_IMAGE_SIZE}"' in big


# ---------------------------------------------------------------------------
# 拆片
# ---------------------------------------------------------------------------
def test_split_flags_roundtrip():
    text = "前缀🇨🇳中间" + ENGLAND + "后缀"
    assert "".join(piece for piece, _is in flag_text.split_flags(text)) == text


def test_split_flags_marks_only_flags():
    pieces = flag_text.split_flags("A🇨🇳B")
    assert pieces == [("A", False), ("🇨🇳", True), ("B", False)]


def test_split_flags_plain_text():
    assert flag_text.split_flags("没有旗") == [("没有旗", False)]
    assert flag_text.split_flags("") == []


# ---------------------------------------------------------------------------
# 显示决策
# ---------------------------------------------------------------------------
def test_to_display_plain_when_no_flag():
    payload, rich = flag_text.to_display("普通消息 <b>不转义</b>")
    assert payload == "普通消息 <b>不转义</b>"          # 纯文本路径一个字都不动
    assert rich is False


def test_to_display_rich_when_flag_and_assets():
    payload, rich = flag_text.to_display("看🇨🇳")
    assert rich is True and "file://" in payload


def test_assets_available_is_true_here():
    """素材缺失时 to_display 会整体退回纯文本 —— 这里跑在仓库里，应该是齐的。"""
    assert flag_text.assets_available(), f"缺素材：{F.png_dir()}"
    payload, rich = flag_text.to_display("看🇨🇳")
    assert rich is True


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
