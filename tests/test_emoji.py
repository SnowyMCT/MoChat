"""表情数据与搜索的单测（零 Qt 依赖，无显示器也能跑）。

用法：``python tests/test_emoji.py``；装了 pytest 也可以 ``pytest tests``。

覆盖：
    表的完整性   —— 每行都被解析、关键词不为空、分类内不重复、分类数与总量
    搜索         —— 中文/英文子串、表情字符精确命中、空关键词返回空、上限
    与面板一致   —— CATEGORIES 的键都能取到表情，"最近"除外
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mchat.ui import emoji as E                                          # noqa: E402


# ---------------------------------------------------------------------------
# 表的完整性
# ---------------------------------------------------------------------------
def test_categories_shape():
    """分类键唯一、每类都有标签与图标，"最近"排第一且不占表数据。"""
    keys = [key for key, _label, _icon in E.CATEGORIES]
    assert len(keys) == len(set(keys)), keys
    assert keys[0] == E.RECENT
    assert E.item_count(E.RECENT) == 0
    for key, label, icon in E.CATEGORIES:
        assert label and icon, key
        assert E.label_of(key) == label


def test_table_is_parsed():
    """每个分类都有货，且总量够"多"（Telegram 面板那种量级）。"""
    assert len(E.SEARCH_ORDER) == 8, E.SEARCH_ORDER
    # 旗帜的表来自生成的 flags 模块（不是手写的 RAW），所以是"RAW + flags"
    assert set(E.SEARCH_ORDER) == set(E.RAW) | {E.FLAGS}, \
        set(E.RAW) ^ set(E.SEARCH_ORDER)
    for key in E.SEARCH_ORDER:
        assert E.item_count(key) >= 40, (key, E.item_count(key))
    assert E.total_count() >= 700, E.total_count()
    # 去掉注释说"跳过"的占位行不该混进来
    for key in E.SEARCH_ORDER:
        for char, _words in E.items(key):
            assert "skip" not in char, char


def test_every_row_has_keywords():
    for key in E.SEARCH_ORDER:
        for char, words in E.items(key):
            assert words, (key, char)
            assert len(char) <= 8, (key, repr(char))
            assert not any(ch.isspace() for ch in char), (key, repr(char))


def test_no_duplicate_within_category():
    for key in E.SEARCH_ORDER:
        chars = [char for char, _w in E.items(key)]
        assert len(chars) == len(set(chars)), \
            (key, [c for c in chars if chars.count(c) > 1])


def test_keyword_encoding():
    """关键词必须是小写、无空格、至少有一个字母数字 —— 混进空白会让搜索变玄学。

    （英文国名里允许 ç / é / ’ 这类真实文字，旗帜的括号已经在构建时去掉了。）
    """
    for key in E.SEARCH_ORDER:
        for char, words in E.items(key):
            for word in words:
                assert word == word.lower(), (key, char, word)
                assert word.strip() == word, (key, char, word)
                assert any(ch.isalnum() for ch in word), (key, char, word)
                assert not re.search(r"[()（）\[\]]", word), (key, char, word)
                if key != E.FLAGS:
                    # 手写的表一行一个词；旗帜允许 "ascension island" 这种短语
                    assert " " not in word, (key, char, word)


def test_all_emoji_covers_every_category():
    for key in E.SEARCH_ORDER:
        for char, _w in E.items(key):
            assert char in E.ALL_EMOJI, (key, char)
    assert len(E.ALL_EMOJI) == len(set(E.ALL_EMOJI))


# ---------------------------------------------------------------------------
# 搜索
# ---------------------------------------------------------------------------
def test_search_chinese():
    hits = E.search("笑")
    assert "😀" in hits and "😂" in hits
    assert len(hits) >= 10


def test_search_english():
    assert "🐱" in E.search("cat")
    assert "🍕" in E.search("pizza")
    assert "🚀" in E.search("rocket")


def test_search_prefix_and_case():
    """大小写不敏感；前缀也能命中（输入到一半就该有结果）。"""
    assert E.search("CAT") == E.search("cat")
    assert "🎧" in E.search("head")


def test_search_exact_emoji_char():
    """直接把表情粘进搜索框 -> 精确命中它自己，且排在最前。"""
    hits = E.search("❤️")
    assert hits and hits[0] == "❤️"


def test_search_empty_returns_nothing():
    assert E.search("") == []
    assert E.search("   ") == []


def test_search_no_match():
    assert E.search("这个词肯定搜不到") == []


def test_search_limit():
    assert len(E.search("a", limit=5)) <= 5


def test_keywords_lookup():
    assert "猫" in E.keywords("🐱")
    assert E.keywords("🙂")


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
