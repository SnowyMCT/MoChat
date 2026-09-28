"""旗帜在文本里的显示：把旗帜字符换成图片，取文本时再换回来。

为什么需要这一层
----------------
旗帜在消息里是 **Unicode 字符**（🇨🇳 = 两个 regional indicator），Windows 的字体
只肯画成 "CN" 两个字母 —— 所以"面板里是图、发出来是字母"。面板能用图是因为格子是
按钮图标，而**气泡、输入框、会话摘要都是 QTextDocument 渲染的文本**，那里想画图
只有一条路：塞 `<img src="file:///…/flags/048.png">`（Qt 富文本只按 URL 取文件）。

约定
----
* 存储/发送/搜索**永远用原字符**：这里只负责"显示前"和"取回后"的转换，
  ``core`` 层和传输层完全不知道图片的存在。
* 只有含旗帜的文本才走富文本；其余仍是纯文本 —— 少一层解析，也不会因为哪里漏了
  转义把用户输入的 `<b>` 当标签吃掉。
* ``to_html`` **先转义再替换**旗帜：旗帜字符不在 ``&<>"'`` 里，转义不会破坏它，
  反过来先替换就会把用户输入的标签放出去。
"""
from __future__ import annotations

import html
from html import escape

from . import flags as flag_data

__all__ = ["TEXT_IMAGE_SIZE", "PREVIEW_IMAGE_SIZE", "contains_flag", "split_flags",
           "to_html", "to_display", "assets_available"]

TEXT_IMAGE_SIZE = 18            # 气泡/输入框里旗帜的显示边长（正文 13.5px）
PREVIEW_IMAGE_SIZE = 14         # 会话摘要那行矮，图也小一点

_ASSETS_OK: bool | None = None

# 首字符 -> 候选旗帜（长的排前面：英格兰那种 tag 序列比两字母旗长，必须先试）
_BY_FIRST: dict[str, tuple[str, ...]] = {}
for _flag in flag_data.FLAG_CHARS:
    _BY_FIRST.setdefault(_flag[0], []).append(_flag)
for _bucket in _BY_FIRST.values():
    _bucket.sort(key=len, reverse=True)
_BY_FIRST = {key: tuple(value) for key, value in _BY_FIRST.items()}


def assets_available() -> bool:
    """逐张 PNG 素材在不在（不在就整体退回纯文本，让用户至少看到字符）。"""
    global _ASSETS_OK
    if _ASSETS_OK is None:
        _ASSETS_OK = bool(flag_data.FLAG_CHARS) and flag_data.png_exists(
            flag_data.FLAG_CHARS[0])
    return _ASSETS_OK


def _match_at(text: str, index: int) -> str:
    """``text[index:]`` 开头是不是某个旗帜（返回最长的那个）。"""
    for candidate in _BY_FIRST.get(text[index], ()):
        if text.startswith(candidate, index):
            return candidate
    return ""


def contains_flag(text: str) -> bool:
    """文本里有没有旗帜（找到第一个就返回，长消息不用整段扫完）。"""
    index = 0
    while index < len(text):
        matched = _match_at(text, index)
        if matched:
            return True
        index += 1
    return False


def split_flags(text: str) -> list[tuple[str, bool]]:
    """切成 ``[(片段, 是不是旗帜), …]``：插入输入框时按片段决定"敲字"还是"插图"。"""
    pieces: list[tuple[str, bool]] = []
    plain: list[str] = []
    index = 0
    while index < len(text):
        matched = _match_at(text, index)
        if matched:
            if plain:
                pieces.append(("".join(plain), False))
                plain.clear()
            pieces.append((matched, True))
            index += len(matched)
        else:
            plain.append(text[index])
            index += 1
    if plain:
        pieces.append(("".join(plain), False))
    return pieces


def _img_tag(char: str, size: int) -> str:
    url = flag_data.png_url(char)
    if not url:
        return ""
    code = flag_data.code_of(char).upper() or "旗帜"
    # alt 会被部分阅读器/悬停用到；宽高写死，否则 Qt 按原图 64px 撑开行高
    return (f'<img src="{url}" width="{size}" height="{size}"'
            f' alt="{escape(code)}" title="{escape(code)}">')


def to_html(text: str, *, size: int = TEXT_IMAGE_SIZE) -> str:
    """消息文本 -> 富文本：旗帜变图片，其余原样（已转义）。"""
    escaped = html.escape(text)
    out: list[str] = []
    index = 0
    while index < len(escaped):
        matched = _match_at(escaped, index)
        tag = _img_tag(matched, size) if matched else ""
        if tag:
            out.append(tag)
            index += len(matched)
        else:
            out.append(escaped[index])
            index += 1
    return "".join(out).replace("\n", "<br>")


def to_display(text: str, *, size: int = TEXT_IMAGE_SIZE) -> tuple[str, bool]:
    """显示用：返回 ``(要显示的字符串, 是不是富文本)``。

    没旗帜、或素材缺失时原样返回纯文本（``rich=False``）—— 调用方据此设
    ``Qt.TextFormat``，纯文本那条路连转义都不用做。
    """
    if not assets_available() or not contains_flag(text):
        return text, False
    return to_html(text, size=size), True
