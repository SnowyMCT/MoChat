# MChat

一个 Telegram 风格的桌面聊天软件。当前是**本机演示**版：界面完整、消息能收能发，
"对方"由脚本自动回复，**还没有联网**（传输层接口已经留好，见下）。

原来那套 **Base64 / AES-256 安全文本转换器**收进了聊天窗口左上角的 **「工具箱」** 按钮
（快捷键 `Ctrl+T`），当一个小功能保留下来，交互和动效一点没丢。

## 运行

```bash
# 方式一：项目自带 venv（依赖已装好）
.venv/Scripts/python.exe main.py

# 方式二：用 uv 按 pyproject.toml 装依赖后运行
uv run main.py
```

## 界面长什么样

- **左栏**：会话列表（头像 + 名字 + 摘要 + 时间 + 未读徽标）、搜索框、主题切换。
  置顶会话排在最前（名字后面标 📌），摘要按宽度自动省略号截断。
  **在会话上点右键**可以置顶 / 免打扰 / 标记为未读 / 清空这个会话的历史（见下）。
- **右栏**：聊天头部（头像、名字、"在线 / N 位成员 / 正在输入…"）、消息区（气泡 + 日期分隔条）、
  输入栏（表情、自动增高输入框、发送按钮）。
- **发出**的消息靠右（亮色主题是 Telegram 那种浅绿气泡，暗色是深蓝），带头上的 ✓ / 已读 ✓✓；
  **收到**的消息靠左，群里还会显示发送者名字。
- 打开就是"今天 / 昨天"的真实时间线（演示数据的时间戳相对"现在"生成，不会过期）。
- 主题：亮色 / 暗色 / 跟随系统，选择会被记住。
- **分界线**：左右两栏之间那条竖线可以直接拖动，改会话列表的宽度（220–460px，窗口变窄时
  上限会自动让步，保证聊天区不被挤没）；鼠标悬停/拖动时竖线会高亮变粗，**双击还原默认 296px**，
  拖出来的宽度和主题一样会被记住，下次打开还在。
- **表情面板**：输入栏左边的笑脸点开就是 Telegram 那样的表情面板（分类 + 搜索 + 最近使用），
  详见下面的「表情面板」一节。

交互细节：`Enter` 发送、`Shift+Enter` 换行、`Ctrl+T` 开工具箱、`Ctrl+F` 聚焦搜索、
拖动分界线改宽度（双击还原）、`Ctrl+E` 开关表情面板（`Esc` 也能关）。
`Ctrl+Shift+F` 开全局搜索；气泡上右键可以加表情回应、删自己的消息。
- **本地历史**：消息写进 SQLite（`%APPDATA%\MChat\...\mchat.db`），关掉再打开还在；
  全文检索走 FTS5 索引，2 万条实测检索 13ms。
- **按会话搜索**：聊天头部的放大镜（或 `Ctrl+Shift+F`）只搜**当前会话**的消息，
  点一行跳过去并高亮那条气泡；顶部「仅当前会话 / 全部会话」可以随时放宽范围，
  换会话时范围自动跟着走。想一上来就跨会话找，走菜单里的「在所有会话中搜索…」。
- **草稿**：切走会话时没发出去的话自动存起来，列表里标成红色「草稿：…」，切回来还在。
- **表情回应**：气泡右键有常用表情（或「更多表情…」从面板里挑），点一下加、再点撤；
  自己点过的胶囊会高亮。
- **删除消息**：自己的消息可以右键删除（红色二次确认），气泡、内存、库三处一起清。

## 表情面板（802 个表情，全部本地，不联网）

点输入栏左边的笑脸（或 `Ctrl+E`）展开，面板夹在消息区和输入栏之间 —— 展开时**把消息区顶上去**，
不是浮层盖住消息：

- **533 个文字表情 + 269 面旗帜 = 802 个**，分 8 类：表情 / 动物自然 / 食物饮品 / 活动运动 /
  旅行地点 / 物品 / 符号 / 旗帜，底部那排图标就是分类（最左边是「最近」）。
- 格子按面板宽度**自动排满**（拖动左右分界线改变宽度时会跟着重排）。
- **搜索中英都行**：`笑`、`cat`、`rocket` 都能搜，关键词表是中英混排的；把表情本身粘进搜索框
  也能精确定位它。旗帜还能按**国名**搜：`中国`、`香港`、`japan`、`cote`（自动忽略重音）都行。
  清空搜索回到原来的分类。
- **最近使用**：用过的表情排最前，最多记 24 个，写进 `QSettings`，下次打开还在。
- 点表情**不会把焦点从输入框抢走**（格子是 `FocusPolicy.NoFocus`），可以连着插好几个；
  插入位置就是输入框里的光标处。
- 文字表情用的是 Unicode 字符 + 系统彩色字体（Windows 是 Segoe UI Emoji）；**旗帜走图片**：
  Windows 的 Segoe UI Emoji 不画国旗，只会渲染成两个字母（"CN"），所以旗帜是
  `mchat/assets/flags.png` 这张本地精灵图里切出来的（259 个两字母旗 + 10 面特殊旗：
  彩虹 / 跨性别 / 海盗 / 英格兰 / 苏格兰 / 威尔士 / 格子旗…）。字符在这两种情况下都只是
  "查槽位用的 key"，和 Telegram 的做法一致。
- **程序运行期间一次网络请求都没有**：精灵图和关键词表都已经生成好放在仓库里。
  只有重新生成素材的 `tools/build_flags.py` 会联网（跑一次就够，见下）。

### 旗帜在消息里也是图片

面板只是入口 —— **发出去的消息、输入框里、会话摘要里的旗帜同样是图片**：

- 存储 / 发送 / 搜索**永远用原来的字符**（core 层和传输层完全不知道图片的存在）；
  显示前把字符换成 `<img src="file:///…/flags/048.png">`，从输入框取文本时再按 URL
  反查回字符。所以除了面板用的精灵图，还生成了 `mchat/assets/flags/000.png…` 逐张 PNG
  （Qt 富文本的 `<img>` 只按 URL 取"一个文件"，喂不了精灵图里的一格）。
- 输入框为此从 `QPlainTextEdit` 换成了 `QTextEdit`（只有 `QTextDocument` 能内嵌图片），
  并用 `setAcceptRichText(False)` 挡住外部粘进来的富文本格式；粘贴文本会走同一条
  "旗帜变图"的路。
- **取文本必须自己走文档**：`toPlainText()` 会把图片变成 `U+FFFC`（对象替换符），
  直接发出去消息内容就废了 —— `MessageEdit.message_text()` 按 `U+FFFC` 的位置把图
  换回字符。
- 只有**含旗帜**的消息才走富文本，其余仍是纯文本 —— 消息里写 `<b>粗</b>` 照样原样显示，
  不会被当标签执行（有回归测试盯着）。

### 旗帜素材怎么来的

```bash
# 只有这一步需要联网；素材生成后程序完全离线
.venv/Scripts/python.exe tools/build_flags.py
```

它会抓 Twemoji 的 72×72 旗图拼成 16 列精灵图，再从 Unicode CLDR 的
`cldr-localenames` 取中英国名做关键词（顺手折叠重音、去掉 `St.`/括号这类噪音）。
下载缓存在 `.flag_cache/`（已 gitignore），重复跑不会重新下载。

- 旗图：**Twemoji（jdecked/twemoji），CC-BY 4.0**；国名：Unicode CLDR，Unicode License。
  署名与替换方式见 `mchat/assets/README.md`（想换素材集只要替掉图再跑一次脚本）。

## 工具箱（原来那个转换器）

点左上角「工具箱」按钮打开，是一个**非模态**窗口（关掉不影响聊天，重复点复用同一个窗口）：

- Base64 编解码、AES-256-CBC 加解密（SHA-256 派生密钥 + PKCS7 填充）；
- 悬浮标签输入框（170ms 变形动效）、密钥区折叠、一键复制、边框闪红报错、底部提示气泡；
- `Ctrl+Enter` 执行、`Ctrl+Shift+C` 复制、`Esc` 清空输出；
- 加密默认 **MChat v1 格式**（`MCH1` + 随机 IV，同一明文每次密文都不同），
  勾选"兼容 Test16 旧格式"则退回固定 IV、可与老程序互通；解密方向两种格式都自动识别。

聊天里还有一条便捷入口：右上角 ⋮ 菜单 →「把输入框的文字送进工具箱」。

## 代码结构

分两层：**core 不依赖 Qt**（能脱离界面单测），**ui 只管画和点**。

```
main.py                     启动入口
mchat/app.py                入口转出（兼容 from mchat.app import main）
mchat/core/                 纯逻辑层，零 Qt 依赖
    crypto.py               Base64 + AES-256-CBC（原 mchat.crypto）
    db.py                   本地历史：SQLite + FTS5（消息/回应/草稿/会话旗标）
    models.py               User / Message / Conversation + 时间文案
    store.py                会话仓库：排序、未读、搜索、变更订阅
    transport.py            传输层接口 ChatTransport + 单机演示实现 LocalTransport
    seed.py                 演示会话与自动回复台词
mchat/ui/                   界面层
    theme.py                颜色令牌（亮/暗）+ 全局 QSS
    icons.py                用 QPainter 现画的矢量图标（无图片资源）
    avatar.py               圆形渐变头像
    widgets.py              通用组件：悬浮标签输入框 / 分段控件 / 提示气泡 / 折叠区 / 左右分界线
    emoji.py                表情表与搜索（纯数据 + 函数，零 Qt 依赖，可脱离界面单测）
    flags.py                旗帜表与精灵图坐标（生成物，零 Qt 依赖）
    emoji_panel.py          表情面板：分类条 + 网格 + 搜索 + 最近使用
    search_dialog.py        全局消息搜索窗口（FTS5 + 跳转）
    anim.py                 统一动效：淡入 / 展开 / 交叉淡入 / 错峰 / 打字点
    flag_text.py            文本里的旗帜：字符 <-> <img>，取文本时反查（零 Qt 依赖）
    sidebar.py              左栏：会话条目（含右键菜单回调）、搜索、工具箱入口、主题切换
    chat_view.py            右栏：气泡、日期分隔条、消息列表、输入栏
    security_tool.py        安全工具箱（面板 + 对话框包装）
    main_window.py          主窗口：接线、主题、事件轮询
assets/flags.png            旗帜精灵图（本地素材，随包分发；生成物）
assets/flags/*.png          逐张旗帜 PNG（消息文本 <img> 用；生成物）
tools/build_flags.py        生成 flags.png + flags.py 的构建脚本（唯一联网的一步）
tests/                      八套测试，共 400 项断言
```

减少重复的做法：会话条目、气泡、输入栏、工具箱都只有一份实现；文案与规则集中在
`models.py` / `store.py`；主题只由 `main_window` 驱动一次，子组件统一 `apply_palette()`。

## 本地历史 / 搜索 / 互动（`mchat/core/db.py`）

聊天记录落在**一个 SQLite 文件**里（`sqlite3` 是标准库，引擎与 FTS5 索引都是原生 C），
位置跟着系统走 —— Windows 是 `%APPDATA%\MChat\...\mchat.db`，菜单里「打开数据目录」
可以直接跳过去。程序运行期间**不开网络**，也不需要任何服务。

```
messages   消息正文（id 主键，conversation_id + ts 有索引）
messages_fts  FTS5 trigram 索引（external content，正文只存一份）
reactions  表情回应（message_id + emoji，记 count 与"是不是我点的"）
drafts     草稿（conversation_id 主键，空串即删除）
```

几个刻意的取舍：

- **写穿**：仓库每次发消息事件就顺手 upsert 一条（幂等，重复写只是刷新状态），
  不做批量攒批 —— 一条 INSERT 是几十微秒，比"攒批 + 定时刷盘"少一份状态。
  演示数据在启动时整批写回一次，所以库是**完整日志**，连几周前的开场白都能搜到。
- **只取最近**：界面一次只画最近 200 条（`LIMIT`），翻到更早的历史由搜索跳转时
  按需再向库里多要一批 —— 进程里不必常驻全量消息。
- **trigram 分词**：中文按子串命中（"这周要去日本"搜"日本"能中）；`unicode61`
  做不到（实测 0 条）。短于 3 个字符的词 trigram 索引命不了，退化成 `LIKE`（仍是 C 层扫描）。
- **WAL + `synchronous=NORMAL`**：已提交的数据崩了不丢，又不用每条都 fsync；
  退出/启动时 `compact()` 把 WAL 收回主库（2 万条 9.5MB → 4.6MB，耗时 44ms）。
- **会话级旗标**存在 `chats` 表（`conversation_id + pinned/muted/cleared`）：置顶与免打扰
  由侧栏右键菜单切换，重启后照旧；`cleared` 是"这个会话被清空过"的标记，用来在启动时
  把重新生成的演示数据里那几条种子消息丢掉 —— 否则上次的清空下次启动就白清了。
- **清空历史是按会话来的**（侧栏右键菜单里那一项）：只删这一个会话的消息、回应和草稿，
  **其它会话不受影响**；仍然走 `confirm_destructive()` 二次确认（红色按钮 + 默认停在"取消"），
  确认文案里会写明是哪个会话、要删多少条。清完顺手 `compact()` 把文件收小。
- **免打扰**不是只画个月亮图标：未读徽标变灰、不再弹跳，而且**不计进顶部总数**
  （`store.total_unread()` 里就把免打扰的跳过，逻辑只有一份）。
- **危险操作**（删消息、清空某个会话）走 `confirm_destructive()`：红色按钮 + 默认停在"取消"。

实测（`tests/test_db.py` 里的基准，2 万条中文消息，本机）：

```
批量落库 365ms ｜ FTS5 检索 13ms ｜ 取最近 200 条 0.5ms ｜ 占用 240 字节/条（compact 后）
```

搜索窗口（`mchat/ui/search_dialog.py`）**范围默认是"你现在开着的这个会话"**（从哪个会话
进去就搜哪个会话），顶部有「仅当前会话 / 全部会话」分段控件；换会话时范围跟着走，
只有"全部会话"模式不受影响。输入防抖 180ms、最多显示 60 行，点一行就切会话 + 滚动 +
闪一下边框定位到那条消息。会话过滤是拼进 SQL 的 `WHERE conversation_id = ?`
（FTS5 与 `LIKE` 两条路径都支持），不是查完再筛。

## 界面动效（`mchat/ui/anim.py`）

时长与缓动只有一份（`FAST 140 / NORMAL 200 / SLOW 280` + `OutCubic`），六处动效共用同一套函数：

- **新消息入场**：**只淡入 + 滚动滑入**，气泡一上来就占最终大小。
  这是被实测逼出来的：最早的写法是"高度 0 长到自然高度 + 淡入"，逐帧量出来三个毛病 ——
  开头 30ms 停着不动、长个儿的时候气泡在行里被反复居中（y 在 `616↔628` 之间来回抖）、
  收尾恢复高度时整条列表跳 48px。根因是**动了几何**：每帧都要重排整块列表。
  现在的做法几何全程不动（表格里的高度恒定 49px、y 从 683 单调滑到 579），
  "从下面滑上来"交给滚动条动画（只改一个整数，绘制层直接平移）。
  只给新到的消息做；初次铺满整屏不做，否则一屏十几条一起动，既卡又晕。
- **"正在输入"**：头部文字后面跟三个跳动的小点（自绘 + 45ms 定时器，隐藏即停，
  不常驻 CPU；相位差做出"追逐"感）。
- **主题交叉淡入**：切主题前先把旧外观抓成一张快照盖上，再让它淡出 —— 不是硬切。
- **未读徽标回弹**：数字变多时用 `OutBack` 弹一下（自绘缩放，不触发重新布局）；
  只对已经在屏幕上的条目弹，首次铺列表不弹，免得太吵。
- **搜索结果错峰淡入**：结果行每行延迟 16ms 依次浮现（延迟累加上限 12 行，60 行也不等半天）。
- **表情回应**：只有新加的那个胶囊淡入，已有的纹丝不动。

滚动位置上的两处处理（不处理的话消息会跑出视野）：

- **新消息的滑入**：`anim.slide_scroll()` 先把位置垫到"新气泡正好在折线下方"，
  再用 240ms 滑到底。有个坑：追加那一瞬间 `scrollbar.maximum()` **还是旧值**
  （布局要一拍才认出新增的高度），所以终点必须挂 `rangeChanged` 动态修正，
  否则会停在"差一条消息"的地方（实测停在 321/376）。
  没有滚动余量（内容还没满一屏）时**什么都不做**：那时新消息本来就在视野里，硬滚反而一跳。
- **表情面板展开/收起**会改消息区高度，原本贴底的位置就"错位"了：等面板铺开
  （或收完）之后再补一次 `follow_bottom` —— 只在打开面板前就是贴底状态时才补，
  正在翻历史的人不该被拽回来。

另外，**点消息区的空白处会收起表情面板**：在消息区视口上装一个 `eventFilter`
（只对"消息区里的点击"生效，点面板自己不受影响），而且**不吞掉这次点击** —— 该选
文字、该点气泡照常。

三条硬约束（写在这里免得后人再踩）：

1. **动画结束一律摘掉 `QGraphicsOpacityEffect`** —— 留着等于给控件常驻一层离屏合成，
   长列表会明显变卡，也破坏像素级断言。另外**别把透明效果套在 QScrollArea 上**，
   视口会走离屏合成、滚动出现拖影。
2. **可一键关闭**：`anim.set_enabled(False)`（菜单里的「界面动效」勾选项，选择记进 QSettings）
   关掉后所有动效**直接落终态**，界面语义不变 —— 测试要像素确定性时也靠它。
3. **删控件前先 `anim.cancel(widget)`**：删除是 `deleteLater`，动画的收尾回调可能才刚跑起来，
   回调里再碰控件，PyQt6 会把 `RuntimeError` 升级成**进程级崩溃**（实测踩过一次，
   清空历史/刷新搜索结果时必现）。

## 怎么接真网络

界面只依赖传输层的三个动作，换成 socket 实现即可，**ui 一行都不用改**：

```python
class ChatTransport(abc.ABC):          # mchat/core/transport.py
    def start(self) -> None: ...
    def send(self, message: Outgoing) -> None: ...
    def poll(self, now: float | None = None) -> list[TransportEvent]: ...
```

- 现在的 `LocalTransport`：`send()` 排一个"正在输入… → 台词回复 → 已读回执"的事件队列，
  `poll()` 把到期的倒出来（用时间戳判断，测试里可以注入假时钟，不用真等）；
- 将来的 `SocketTransport`：`send()` 写 socket，`poll()` 读 socket 并解析成同样的事件。
- 主窗口每 400ms 调一次 `tick()`，把事件喂给 `ChatStore`，界面靠订阅刷新 —— 这条链路不用动。
- `ChatStore` 内部有锁，网络线程直接 append 也是安全的。

## 打包成 exe

```bash
# 推荐：打包（onefile + 无控制台）+ 自检素材真进了包
.venv/Scripts/python.exe tools/build_exe.py

# 等价的手动命令（走 main.spec，里面已经带了素材）
pyinstaller main.spec
```

**别再用 `pyinstaller -F -w main.py`**。那条命令不带任何 data 文件，而旗帜是**图片素材**
（`mchat/assets/flags.png` + 逐张 PNG）：冻结后 `flags.sprite_exists()` / `png_exists()`
全假，界面整体降级成纯文本，而 Windows 的 Segoe UI Emoji 不画国旗、只画成两个字母 ——
于是旗帜全部变成 "CN" "JP"（踩过一次的坑）。素材必须按 `main.spec` 的
`datas=[('mchat/assets', 'mchat/assets')]` 打进去；而且那条命令会把 `main.spec`
重写成 `datas=[]` 的默认版，所以别拿它当备选。

素材路径是**冻结感知**的：`flags._assets_dir()` 先看 `sys._MEIPASS`（PyInstaller 的解包
目录，onefile 是临时目录、onedir 就是 exe 旁边，层级与源码一致），那里没有才落回源码树。
onefile / onedir 都不用改代码。

`tools/build_exe.py` 做四件事：① 检查素材齐不齐（缺就立刻报，别等翻包）；
② 找 PyInstaller —— 当前解释器优先，退回 PATH 上的 `pyinstaller`，并把实际用的解释器
打出来（**用哪个解释器打包就冻结哪个 PyQt6**）；③ 跑 `pyinstaller main.spec`；
④ 读 PyInstaller 自己写的 `build/main/PKG-00.toc` 清单，核对 `mchat/assets` 下的文件
真的都进了包，对不上就非 0 退出。

`tests/test_flags.py` 里有三条断言盯着这件事（冻结路径解析、素材路径必须是绝对路径、
`main.spec` 必须带素材），再把 `pyinstaller -F -w main.py` 之类改回来会当场变红。

**打包后想确认"旗帜真的画成图了"**，跑一次自检探针（不弹窗、不抢焦点，退出码 0 就是好的）：

```bash
pyinstaller tools/frozen_check.py --onefile --console --noconfirm --paths . \
    --add-data "../../mchat/assets;mchat/assets" --name frozen_check \
    --specpath build/probe --workpath build/probe --distpath dist
dist/frozen_check.exe --shot artifacts/frozen_flags.png
```

它依次验证：素材路径与数量（269 张 + 精灵图）→ 富文本里的旗帜真的是 `<img>` 且
图片文件存在 → 面板格子的图标不是退回画字符、相邻两面旗切出来不是同一张图 →
截图（`artifacts/frozen_flags.png` 是富文本、`..._panel.png` 是面板），都是肉眼可核的。
不打包直接 `python tools/frozen_check.py` 跑也行 —— 那验证的是源码树里那份素材，
两边各跑一次就能判断是"素材坏了"还是"没打进去"。

## 测试

```bash
# 规范命令：一条跑完全部八套（400 项断言）
.venv/Scripts/python.exe -m pytest tests -q

# 也可以逐套直接跑（不依赖 pytest，会打印每项结果并生成截图）
.venv/Scripts/python.exe tests/test_crypto.py          # 算法层 13 项
.venv/Scripts/python.exe tests/test_chat_core.py       # 核心逻辑 28 项（零 Qt）
.venv/Scripts/python.exe tests/test_db.py              # 本地历史/检索/互动/旗标 22 项（零 Qt，含效率基准）
.venv/Scripts/python.exe tests/test_emoji.py           # 表情数据与搜索 14 项（零 Qt）
.venv/Scripts/python.exe tests/test_flags.py           # 旗帜数据/素材/搜索/打包 21 项（零 Qt）
.venv/Scripts/python.exe tests/test_flag_text.py       # 旗帜文本渲染 14 项（零 Qt）
.venv/Scripts/python.exe tests/test_security_tool.py   # 工具箱界面 45 项
.venv/Scripts/python.exe tests/test_ui.py              # 聊天界面端到端 246 项（含动效/滚动/右键菜单）
```

pytest 已作为 dev 依赖装在项目 venv 里（`pyproject.toml` 的 `[dependency-groups] dev`）。
两个界面套件是脚本式的（`main()` 返回失败项数），文件末尾各挂了一个 `test_*_suite()`
包装，所以 `pytest tests` 与直接运行跑的是同一批断言。

界面测试用 `WA_DontShowOnScreen` 让窗口"显示但不出现"：布局与绘制走真实平台（中文渲染正常），
屏幕上不会弹窗、不抢焦点；截图落在 `artifacts/`（亮/暗主题的聊天窗口 + 工具箱）。
无显示器的环境可加 `QT_QPA_PLATFORM=offscreen`（此时中文会变成方块，只适合跑断言）。

`tests/ui_probe.py` 是两个界面测试共用的工具：`pump()` 推进事件循环、
`glyph_pixels()` / `widget_pixel()` 按 DPR 从窗口截图里取像素（验"字色是否可读"、
"分界线到底画没画出来"这类只能靠像素说话的问题）、`colorful_pixels()` 数彩色像素
（验表情是不是真的用彩色字体画出来了，而不是豆腐块）、`drag_split_handle()` /
`double_click_handle()` 发带真实全局坐标的鼠标事件，用来端到端模拟拖分界线。
`wait_for(app, predicate)` 则是**动画断言的正确姿势**：一边跑事件循环一边等条件成立，
而不是"睡固定时长 + 卡阈值" —— 后者在机器慢一拍时会在动画尾巴上偶发失败（同一处抖过
两次、约 1/30 概率），换成它之后连跑十几轮都稳。

## 实现上的几个坑（都已处理）

- **构造期事件**：`setCursor` / `setMinimumHeight` 会在 `__init__` 中途派发事件，此时处理函数若访问
  尚未创建的属性，PyQt 会在 C++ 虚函数里抛异常并直接 `abort()`（没有 Python 报错栈）。
  自绘组件因此都有 `_ready` 闸门，且事件过滤器安装前先给 `self._field` 赋值。
- **动态控件必须显式 `show()`**：父控件已显示后再创建的子控件默认隐藏 —— 切换算法时新建的分段按钮、
  刷新会话列表时新建的条目，不 `show()` 会整片消失。
- **别指望 `deleteLater()` 立刻回收**：`DeferredDelete` 只在事件循环回到主层级时处理，
  旧控件会和新控件叠着画。改法是先 `hide()` + `setParent(None)` 再 `deleteLater()`。
- **动态属性的 QSS 要手动 repolish**：会话条目的选中态用 `[selected="true"]` 选择器，
  改完属性必须 `style().unpolish/polish` 才会重新匹配，否则高亮不变。
- **对话框不继承主窗口样式表**：`QDialog` 是顶层窗口，得自己 `setStyleSheet(build_qss(p))`，
  所以工具箱由主窗口在切主题时显式 `apply_palette()`。
- **分界线由自己画，所以侧栏的 `border-right` 挪走了**：竖线如果由侧栏和分界线各画一半，
  主题切换、悬停高亮的时机对不上就会露出双线或错位。现在 `#sidebar` 不画右边框，
  `SplitHandle.paintEvent` 一次画完"底色 + 中缝"（默认边框色、悬停主题色、拖动加粗）。
  另外侧栏宽度用 `setFixedWidth` 而不是 `setMinimumWidth`：拖动时只有聊天区在伸缩，
  宽度所见即所得，不会因为窗口略窄被布局"匀"回去。
- **未读的先后顺序**：`ChatStore.receive()` 先累加未读再入库 —— 入库会通知界面，
  界面发现"这条会话正开着"会立刻清零；顺序反了未读会残留。
- **表情面板是"布局里的控件"，不是弹窗**：`Qt.Popup` 那类浮层在
  `WA_DontShowOnScreen` 的窗口上根本没法做无头验证（另一个顶层窗口，收尾还容易挂住），
  而且会盖住消息。现在面板就夹在消息区和输入栏之间，展开动画只改 `maximumHeight`，
  消息区自然被顶上去 —— 既能被测试，又和 Telegram 的行为一致。
- **表情格子必须 `NoFocus`**：QToolButton 默认点击会拿焦点，用户点两个表情就得再点回输入框。
  格子设成 `FocusPolicy.NoFocus` 后，焦点留在输入框/搜索框，可以连点。
- **`autoExclusive` 的按钮不能程序化取消勾选**：搜索状态下要让所有分类按钮都"未选中"，
  直接 `setChecked(False)` 会被 Qt 挡回去；得先 `setAutoExclusive(False)`、改完再打开。
- **旗帜只能走图片**：Windows 的 Segoe UI Emoji 不画国旗（渲染成 "CN" 两个字母），
  英格兰/苏格兰/威尔士那种 tag 序列更是几乎没字体支持。所以旗帜做成一张本地精灵图，
  按 `字符 -> 槽位` 查坐标切图（`flags.cell_rect`）—— 和 Telegram 一样，字符只是 key。
  另外**素材是生成物**：`tools/build_flags.py` 跑一次产出 `flags.png` + `flags.py`，
  程序运行时不联网；构建脚本里的正则/反斜杠务必用编辑器写（shell heredoc 会把 `\b`
  变成真的退格符，正则静默失配）。
- **富文本里判断"是不是图片"不能只看 `charFormat().isImageFormat()`**：图片后面插入的
  文字（甚至 `QPlainTextEdit → QTextEdit` 的 `setPlainText`）会**继承图片的字符格式**，
  于是纯文字片段也报 `isImageFormat() == True`，取文本时就把它当成图片、返回空串。
  判据要跟 Qt 自己对齐：真图片在 `toPlainText()` 里是 `U+FFFC`（对象替换符）。
  另外插图后要 `cursor.setCharFormat(QTextCharFormat())` 抹掉图片格式，否则后续输入的
  字符都带着它。这两条合起来才是"输入框里能插图又取回原文"。
- **`QMenu.exec()` 会阻塞，别在测试里触发它**：右键菜单"能不能弹出来"没法在无头测试里
  验证（exec 会一直等用户点），正确做法是把菜单构造拆成 `_build_menu()` /
  `_build_conversation_menu()` 这样的方法，测试直接拿到 `QMenu`、按文字点名 `QAction`
  再 `trigger()` —— 覆盖的是同一条代码路径。想看样式就 `popup()`（不阻塞）+ `grab()`。
  反过来，**别在测试里真的发一个会走到 `exec()` 的事件**：会挂死到超时（实测踩过一次，
  10 分钟才被 timeout 掐掉）。非要测"右键那一行会请求弹菜单"这条链路，就先
  `signal.disconnect(handler)` 摘掉主窗口的处理器，测完再接回去。
- **列表里别动控件几何**：想让新消息"冒出来"而去动画它的高度，代价是每帧一次整块
  列表重排 + 气泡在同一行里被反复居中（实测 y 在 `616↔628` 来回抖），收尾恢复
  高度时还会整体跳一下 —— 观感就是"卡一下"。正确做法：**控件一上来就占最终大小**，
  只用透明度做淡入，"从下面滑上来"改成滚动条动画（只改整数、不做布局）。
  要动几何时也得记住：追加那一刻 `scrollbar.maximum()` 还是旧值，滑动终点必须挂
  `rangeChanged` 动态修正。回归用例要先堆出滚动条、再逐帧采样"高度恒定 + y 单调"
  （用 `mapTo(viewport, …)`，别用 `widget.y()`——那是行内相对坐标，恒为 0，断言等于没断言）。
- **PyQt6 动画的两个致命坑（都把进程干崩过）**：① `QTimer.singleShot(0, 上下文对象, lambda)`
  这个三参数重载 + lambda 会崩，换成两参数版就没事；② 在 `QAbstractAnimation.finished` 槽里
  当场 `setGraphicsEffect(None)` 也会崩（推迟到下一轮事件循环再摘）。③ 控件被 `deleteLater`
  之后动画回调再碰它，`RuntimeError` 会被升级成致命错误 —— 所以删之前先 `anim.cancel()`。
  这类崩溃的排查姿势：`python -X faulthandler` 直接给出崩溃时的 Python 调用栈。
- **SQLite 的取舍（都踩过）**：中文子串检索必须用 FTS5 的 `trigram` 分词（`unicode61`
  对"这周要去日本"搜"日本"是 0 条）；`trigram` 命不了 3 字符以下的关键词，得留一条
  `LIKE` 兜底；外部内容表（`content='messages'`）要靠触发器维护，**UPDATE/DELETE 都要写
  `'delete'` 那条特殊插入**，否则索引会和正文漂移；`VACUUM` 自己会往 WAL 里写一大堆页，
  所以 **先 VACUUM 再 `wal_checkpoint(TRUNCATE)`** 才收得干净（反过来白干）。
- **危险操作的确认弹窗要能注入**：`QMessageBox.exec()` 会开一个嵌套事件循环，
  无头测试里会直接挂住。所以主窗口收 `confirm` 回调（默认弹真窗），测试注入固定答案；
  同理**本地库也要能注入**（`log=`），否则跑一次测试就把用户 `%APPDATA%` 里的真实历史
  改了一遍 —— 这两条都是"可测试性倒逼出来的设计"。
- **富文本图片只能按 URL 取文件**：`<img src="…">` 支持 `file:///` 路径（`QLabel`、
  `QTextEdit` 都行，不用注册 QTextDocument 资源），但喂不了精灵图里的一格 —— 所以
  面板用精灵图、文本侧另生成逐张 PNG。Windows 路径里的盘符冒号别让 `quote()` 编码成
  `%3A`；路径带空格/引号时必须编码（`quote(path, safe="/:")`）。
- **QSS 只写背景不写颜色 = 暗色主题下的黑字**：`QTextEdit#composerInput` 原来只设了
  `background: transparent`，Qt 就把字色退回系统默认调色板（黑），落在深色输入栏上几乎看不见。
  凡是在 QSS 里给文字控件设了背景，就顺手把 `color`（以及 `selection-*`）写上；
  组件侧再用 `QPalette` 设一遍 `Text` / `PlaceholderText` 兜底。测试用像素断言守着
  （`glyph_pixels()`：暗色下最亮像素必须够亮、最暗不低于底色）。
- **截图断言要乘 devicePixelRatio**：高分屏（本机 DPR 1.75）下 `grab()` 出来的是设备像素，
  用逻辑坐标去 `copy(QRect)` 会裁到毫不相干的区域，测出来的数字全是错的。
- **逐帧不做 QSS 解析**：动画只改字号与 `QPalette` 颜色；颜色令牌变了才重建样式表。
- **自绘控件不嵌套布局**：标签、滑块、字段的几何全部手算，动画期间不会触发布局回流。
