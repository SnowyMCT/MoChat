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
  置顶会话排在最前（演示数据里"王尼玛"是置顶），摘要按宽度自动省略号截断。
- **右栏**：聊天头部（头像、名字、"在线 / N 位成员 / 正在输入…"）、消息区（气泡 + 日期分隔条）、
  输入栏（表情、自动增高输入框、发送按钮）。
- **发出**的消息靠右（亮色主题是 Telegram 那种浅绿气泡，暗色是深蓝），带头上的 ✓ / 已读 ✓✓；
  **收到**的消息靠左，群里还会显示发送者名字。
- 打开就是"今天 / 昨天"的真实时间线（演示数据的时间戳相对"现在"生成，不会过期）。
- 主题：亮色 / 暗色 / 跟随系统，选择会被记住。

交互细节：`Enter` 发送、`Shift+Enter` 换行、`Ctrl+T` 开工具箱、`Ctrl+F` 聚焦搜索。

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
    models.py               User / Message / Conversation + 时间文案
    store.py                会话仓库：排序、未读、搜索、变更订阅
    transport.py            传输层接口 ChatTransport + 单机演示实现 LocalTransport
    seed.py                 演示会话与自动回复台词
mchat/ui/                   界面层
    theme.py                颜色令牌（亮/暗）+ 全局 QSS
    icons.py                用 QPainter 现画的矢量图标（无图片资源）
    avatar.py               圆形渐变头像
    widgets.py              通用动效组件：悬浮标签输入框 / 分段控件 / 提示气泡 / 折叠区
    sidebar.py              左栏：会话条目、搜索、工具箱入口、主题切换
    chat_view.py            右栏：气泡、日期分隔条、消息列表、输入栏
    security_tool.py        安全工具箱（面板 + 对话框包装）
    main_window.py          主窗口：接线、主题、事件轮询
tests/                      四套测试，共 131 项断言
```

减少重复的做法：会话条目、气泡、输入栏、工具箱都只有一份实现；文案与规则集中在
`models.py` / `store.py`；主题只由 `main_window` 驱动一次，子组件统一 `apply_palette()`。

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

## 测试

```bash
# 规范命令：一条跑完全部四套（139 项断言）
.venv/Scripts/python.exe -m pytest tests -q

# 也可以逐套直接跑（不依赖 pytest，会打印每项结果并生成截图）
.venv/Scripts/python.exe tests/test_crypto.py          # 算法层 13 项
.venv/Scripts/python.exe tests/test_chat_core.py       # 核心逻辑 21 项（零 Qt）
.venv/Scripts/python.exe tests/test_security_tool.py   # 工具箱界面 45 项
.venv/Scripts/python.exe tests/test_ui.py              # 聊天界面端到端 60 项
```

pytest 已作为 dev 依赖装在项目 venv 里（`pyproject.toml` 的 `[dependency-groups] dev`）。
两个界面套件是脚本式的（`main()` 返回失败项数），文件末尾各挂了一个 `test_*_suite()`
包装，所以 `pytest tests` 与直接运行跑的是同一批断言。

界面测试用 `WA_DontShowOnScreen` 让窗口"显示但不出现"：布局与绘制走真实平台（中文渲染正常），
屏幕上不会弹窗、不抢焦点；截图落在 `artifacts/`（亮/暗主题的聊天窗口 + 工具箱）。
无显示器的环境可加 `QT_QPA_PLATFORM=offscreen`（此时中文会变成方块，只适合跑断言）。

`tests/ui_probe.py` 是两个界面测试共用的工具：`pump()` 推进事件循环、
`glyph_pixels()` 按 DPR 裁出控件区域数明暗像素（验"字色是否可读"这类只能靠像素说话的问题）。

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
- **未读的先后顺序**：`ChatStore.receive()` 先累加未读再入库 —— 入库会通知界面，
  界面发现"这条会话正开着"会立刻清零；顺序反了未读会残留。
- **QSS 只写背景不写颜色 = 暗色主题下的黑字**：`QPlainTextEdit#composerInput` 原来只设了
  `background: transparent`，Qt 就把字色退回系统默认调色板（黑），落在深色输入栏上几乎看不见。
  凡是在 QSS 里给文字控件设了背景，就顺手把 `color`（以及 `selection-*`）写上；
  组件侧再用 `QPalette` 设一遍 `Text` / `PlaceholderText` 兜底。测试用像素断言守着
  （`glyph_pixels()`：暗色下最亮像素必须够亮、最暗不低于底色）。
- **截图断言要乘 devicePixelRatio**：高分屏（本机 DPR 1.75）下 `grab()` 出来的是设备像素，
  用逻辑坐标去 `copy(QRect)` 会裁到毫不相干的区域，测出来的数字全是错的。
- **逐帧不做 QSS 解析**：动画只改字号与 `QPalette` 颜色；颜色令牌变了才重建样式表。
- **自绘控件不嵌套布局**：标签、滑块、字段的几何全部手算，动画期间不会触发布局回流。
