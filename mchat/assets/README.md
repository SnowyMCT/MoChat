本目录是**随程序分发的本地素材**，程序运行时不会联网。

flags.png
---------
一面旗 64×64 像素、16 列排布的精灵图（`mchat/ui/flags.py` 里的 `COLS` / `CELL` / `FLAGS`
顺序就是槽位顺序）。表情面板的「旗帜」分类按槽位切图显示 —— 因为 Windows 的
Segoe UI Emoji 不画国旗，只会渲染成两个字母（"CN"），英格兰/苏格兰/威尔士那种
tag 序列更是几乎没字体支持。字符只当"查槽位用的 key"。

这个文件是**生成物**，不要手改。重新生成（唯一需要联网的一步）：

```bash
.venv/Scripts/python.exe tools/build_flags.py
```

脚本会抓 Twemoji 的 72×72 旗图拼成精灵图、从 CLDR 取中英国名生成关键词表，下载缓存
留在仓库根的 `.flag_cache/`（已 gitignore），重复跑不会重新下载。

素材来源与许可
--------------
- **旗图**：Twemoji（<https://github.com/jdecked/twemoji>），**CC-BY 4.0**。
  本项目的署名要求：保留本文件与 `mchat/ui/flags.py` 里的来源说明。
- **国家名**：Unicode CLDR `cldr-localenames-full`（territories，zh / en），
  Unicode License (<https://www.unicode.org/license.txt>)。

注意：Twemoji 是"图形化呈现"，不代表任何政治主张；如果你要用于正式场合，
自行替换成合意的旗图集之后跑一次构建脚本即可（表结构不变）。

demo/
-----
演示附件的素材，全部由脚本**自己生成**（不下载、无第三方许可问题）：

- `配色预览.png`（960×600）：演示"图片消息"的气泡缩略图，渐变天空 + 落日 + 远山；
- `评审材料-v3.pdf`（几百字节）：演示"文件消息"的文件卡片，手写的最小合法一页 PDF。

重新生成：

```bash
.venv/Scripts/python.exe tools/build_demo_assets.py
```

这两个文件必须**提交进仓库**：程序运行时不生成、不联网，演示数据（`mchat/core/seed.py`）
直接引用它们的绝对路径；测试、截图复核也拿它们当真实附件。
打包时它们跟着 `mchat/assets` 一起进 exe（见 `main.spec` 的 datas，
`tools/build_exe.py` 会在打包前后各核对一次）。
