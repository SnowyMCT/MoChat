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
import re
from html import escape

from ..core.models import ellipsize
from . import flags as flag_data

__all__ = ["TEXT_IMAGE_SIZE", "PREVIEW_IMAGE_SIZE", "contains_flag", "split_flags",
           "to_html", "to_display", "truncate", "assets_available",
           "URL_PATTERN", "contains_link", "split_rich"]

TEXT_IMAGE_SIZE = 18            # 气泡/输入框里旗帜的显示边长（正文 13.5px）
PREVIEW_IMAGE_SIZE = 14         # 会话摘要那行矮，图也小一点

#: 消息里认成链接的形状：只认 ``http(s)://`` 和 ``www.`` 开头。
#: **不去猜裸域名** —— 中文里"见 a.com"这种误判比漏判更烦人（Telegram 也只做这两类）。
URL_PATTERN = re.compile(r"(?:https?://|www\.)[^\s\u3000<>\"'（）()【】\[\]，。；]+",
                         re.IGNORECASE)

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


def truncate(text: str, limit: int, *, ellipsis: str = "…") -> str:
    """按字符数截断给用户看，但**绝不把一面旗帜切成两半**。

    为什么需要它：置顶横幅、会话摘要这类地方都是"取前面几十个字"，
    直接切字符串很容易正好切在两字符旗帜的中间 —— 剩下一半会渲染成一个
    字母方块（看起来像乱码）。切完把落单的那一半去掉即可。

    实现只有一份：``core.models.ellipsize``（数据层也要用它，见会话摘要与回复引用），
    这里只是 UI 侧沿用的名字。
    """
    return ellipsize(text, limit, ellipsis=ellipsis)


def to_display(text: str, *, size: int = TEXT_IMAGE_SIZE,
               links: bool = True) -> tuple[str, bool]:
    """显示用：返回 ``(要显示的字符串, 是不是富文本)``。

    旗帜变图、链接变可点的 ``<a>``，**两种替换在同一趟里做**（``split_rich``），
    否则"先替换旗帜再找链接"会在已经替换出来的 HTML 上再扫一遍 —— 转义打架，
    用户输入的标签也有被放出去的风险。

    没有旗帜也没有链接、或者旗帜素材缺失时原样返回纯文本（``rich=False``）——
    调用方据此设 ``Qt.TextFormat``，纯文本那条路连转义都不用做。
    """
    pieces = split_rich(text)
    flags_ok = assets_available()
    out: list[str] = []
    rich = False
    for kind, value in pieces:
        if kind == "flag":
            tag = _img_tag(value, size) if flags_ok else ""
            if tag:
                out.append(tag)
                rich = True
            else:
                out.append(escape(value))        # 素材缺了就退回字符，别丢内容
        elif kind == "url" and links:
            href = value if value[:4].lower() == "http" else "http://" + value
            out.append(f'<a href="{escape(href, quote=True)}">{escape(value)}</a>')
            rich = True
        else:
            out.append(escape(value))
    if not rich:
        return text, False
    return "".join(out).replace("\n", "<br>"), True


def contains_link(text: str) -> bool:
    """文本里有没有链接（气泡据此决定要不要连上"点开"那条路）。"""
    return bool(URL_PATTERN.search(text))


def split_rich(text: str) -> list[tuple[str, str]]:
    """把文本切成 ``[("text" | "flag" | "url", 片段), …]``（一趟扫完，顺序不乱）。

    同一趟里判两种替换，是为了让"旗帜"和"链接"在任何顺序下都互不干扰
    （链接里不会出现旗帜字符，旗帜也不会是链接的一部分）。
    """
    pieces: list[tuple[str, str]] = []
    plain: list[str] = []
    index = 0
    while index < len(text):
        matched = _match_at(text, index)
        if matched:
            if plain:
                pieces.append(("text", "".join(plain)))
                plain.clear()
            pieces.append(("flag", matched))
            index += len(matched)
            continue
        found = URL_PATTERN.match(text, index)
        if found:
            if plain:
                pieces.append(("text", "".join(plain)))
                plain.clear()
            pieces.append(("url", found.group(0)))
            index = found.end()
            continue
        plain.append(text[index])
        index += 1
    if plain:
        pieces.append(("text", "".join(plain)))
    return pieces
