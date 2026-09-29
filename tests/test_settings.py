"""设置相关（不需要窗口）：字号档位表 + 全局字号缩放 + 样式表缩放。

用法：``python tests/test_settings.py``；装了 pytest 也可以 ``pytest tests``。

这些断言守的是"文字大小"这条链路最容易漏的地方：

* 档位表本身合理（有标准档、递增、区间有效）；
* 样式表里**每一个** ``font-size`` 都被缩放 —— 漏一个就会出现"气泡变大了、别处没变"；
* 系数为 1 时样式表**逐字节不变**（否则主题/字号那些字符串断言会莫名变红）；
* 坏输入（字符串、NaN、越界）不会把界面搞崩。
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mchat.ui.theme import (FONT_SCALE_LABELS, FONT_SCALE_MAX,        # noqa: E402
                            FONT_SCALE_MIN, FONT_SCALES, LIGHT,
                            build_qss, clamp_font_scale, font_scale,
                            label_of, menu_qss, scale_font_sizes, scale_of,
                            set_font_scale)

_FONT_PX = re.compile(r"font-size:\s*([0-9]*\.?[0-9]+)px")


def _reset() -> None:
    """字号系数是模块级全局状态，测完要归位（否则影响别的测试套件）。"""
    set_font_scale(1.0)


def _sizes(qss: str) -> list[float]:
    return [float(value) for value in _FONT_PX.findall(qss)]


# ---------------------------------------------------------------------------
# 档位表
# ---------------------------------------------------------------------------
def test_font_scale_table_is_sane():
    assert len(FONT_SCALES) >= 3, FONT_SCALES
    labels = [label for label, _ in FONT_SCALES]
    assert labels == list(FONT_SCALE_LABELS) and len(set(labels)) == len(labels)
    scales = [scale for _, scale in FONT_SCALES]
    assert scales == sorted(scales), scales                      # 从小到大
    assert 1.0 in scales, scales                                 # 必须有"标准"档
    assert FONT_SCALE_MIN == scales[0] and FONT_SCALE_MAX == scales[-1]
    assert FONT_SCALE_MIN < 1.0 < FONT_SCALE_MAX                 # 能小也能大


def test_scale_lookup_round_trip():
    for label, scale in FONT_SCALES:
        assert scale_of(label) == scale
        assert label_of(scale) == label
    assert scale_of("压根没有这个档位") == 1.0                     # 认不出来就标准
    assert label_of(0.9) in FONT_SCALE_LABELS                    # 系数 -> 最近的档位


def test_clamp_rejects_junk():
    assert clamp_font_scale(None) == 1.0
    assert clamp_font_scale("不是数字") == 1.0
    assert clamp_font_scale(float("nan")) == 1.0
    assert clamp_font_scale(1e9) == FONT_SCALE_MAX               # 越界夹住
    assert clamp_font_scale(-5) == FONT_SCALE_MIN
    assert clamp_font_scale("1.14") == 1.14                      # 数字字符串也认


# ---------------------------------------------------------------------------
# 样式表缩放
# ---------------------------------------------------------------------------
def test_scale_one_is_byte_identical():
    """系数为 1 时不许改写样式表：不然 QSS 文本断言会无端变红。"""
    _reset()
    qss = build_qss(LIGHT)
    assert scale_font_sizes(qss, 1.0) == qss
    assert scale_font_sizes(qss, 1.0) is qss                     # 连拷贝都不做
    _reset()


def test_every_font_size_gets_scaled():
    """这是"文字大小"能不能全局生效的关键：一个 font-size 都不能漏。"""
    _reset()
    base = build_qss(LIGHT)
    factor = 1.28                                                # 最大档（「特大」）
    scaled = build_qss(LIGHT, scale=factor)
    before, after = _sizes(base), _sizes(scaled)
    assert before and len(before) == len(after), (len(before), len(after))
    for original, grown in zip(before, after):
        assert abs(grown - original * factor) < 0.01, (original, grown)
    # 「小」档也一样（乘一个小于 1 的数，别出现 0 或负数）
    smaller = _sizes(build_qss(LIGHT, scale=FONT_SCALE_MIN))
    assert all(value > 0 for value in smaller)
    assert all(abs(a - b * 0.88) < 0.01 for a, b in zip(smaller, before))
    _reset()


def test_global_scale_applies_by_default():
    """设置里改完只做一件事：set_font_scale()。之后所有 build_qss 都得跟上。"""
    _reset()
    assert "font-size: 13.5px" in build_qss(LIGHT)               # 气泡正文原字号
    set_font_scale(1.14)
    assert abs(font_scale() - 1.14) < 1e-9
    assert "font-size: 15.39px" in build_qss(LIGHT)              # 13.5 × 1.14
    assert "font-size: 13.5px" not in build_qss(LIGHT)
    # 显式传 scale 的参数优先于全局（测试/预览用得上）
    assert "font-size: 13.5px" in build_qss(LIGHT, scale=1.0)
    _reset()
    assert "font-size: 13.5px" in build_qss(LIGHT)


def test_menu_and_settings_styles_scale_too():
    _reset()
    menu = menu_qss(LIGHT, scale=1.25)
    assert _sizes(menu) and all(value != 12.5 for value in _sizes(menu))
    assert abs(_sizes(menu)[0] - 12.5 * 1.25) < 0.01
    qss = build_qss(LIGHT)
    for selector in ("QDialog#settingsRoot", "QLabel#settingsPreview",
                     "QPushButton#ghostBtn", "QLabel#settingName"):
        assert selector in qss, selector                          # 设置区域的样式在
    _reset()


def test_scaled_qss_still_parses():
    """缩放后的样式表要是语法错了，Qt 会静默丢弃规则（界面没样式、也不报错）。"""
    from PyQt6.QtWidgets import QApplication, QLabel

    app = QApplication.instance() or QApplication([])
    qss = build_qss(LIGHT, scale=1.14)
    probe = QLabel("探针")
    probe.setStyleSheet(qss)
    assert "#3390EC" in probe.styleSheet()                        # 真的吃进去了
    hit = probe.styleSheet().count("font-size")
    assert hit == len(_FONT_PX.findall(qss)), (hit, len(_FONT_PX.findall(qss)))
    probe.deleteLater()
    app.processEvents()


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
        finally:
            _reset()
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
