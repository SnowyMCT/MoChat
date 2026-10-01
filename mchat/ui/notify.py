"""桌面通知 + 提示音（自包含，不依赖仓库里任何其它模块）。

为什么单独一个模块、而不是塞进 main_window：
    通知这条链路**天生容易失败** —— 无托盘的精简系统、没有音频后端、被重定向的
    会话、Windows 上没 `show()` 过的托盘图标……任何一环出问题都不该把聊天窗口带崩。
    所以这里把「弹气泡 + 响一声」圈成一个可单独测试的小盒子：入口只有一个
    :meth:`Notifier.message`，它**保证不抛异常**（内外两层 try/except），调用方只管调。

三道降级链，每一道都是被现实逼出来的：
    1. 桌面气泡：``QSystemTrayIcon.showMessage``；系统没有托盘（``isSystemTrayAvailable``
       为假）就整个跳过 —— **不在这里弹 QMessageBox 兜底**，否则用户每来一条消息就被
       一个模态窗打断；兜底展示是调用方的事（它才知道要不要在聊天区加一条提示）。
    2. 提示音：优先 ``QtMultimedia.QSoundEffect``；模块**延迟导入**，因为 PyQt6 的
       QtMultimedia 在部分精简打包（PyInstaller 少拷插件）里会 ImportError，写在文件
       顶部就会把整个 UI 一起拖死。
    3. QtMultimedia 不可用 / 播放出错：Windows 退到 ``winsound.MessageBeep()``（系统
       自带提示音，不需要 Qt 音频后端），其它平台退到 ``QApplication.beep()``。

提示音是**程序生成**的：标准库 ``wave`` + ``struct`` 在内存里合成两个短促音
（880Hz→1320Hz，各 ~120ms，带淡入淡出），首次需要时落盘成临时 WAV 再交给
QSoundEffect —— 这样仓库里不用带二进制音频资源，打包也不会漏文件。
"""
from __future__ import annotations

import io
import math
import os
import struct
import sys
import tempfile
import threading
import wave

__all__ = ["Notifier", "build_chime_wav", "sound_file_path", "tray_available",
           "SAMPLE_RATE", "TONE_SPECS"]

# ---- 提示音参数 -----------------------------------------------------------
SAMPLE_RATE = 44_100
#: (频率 Hz, 时长 秒)。两声上行：「叮-咚」，比单音更容易从背景声里被注意到。
TONE_SPECS: tuple[tuple[float, float], ...] = ((880.0, 0.12), (1320.0, 0.12))
_AMPLITUDE = 0.35          # 峰值音量：留足余量，避免叠加系统音量后削波
_FADE_SECONDS = 0.012      # 淡入/淡出时长

#: 气泡停留时长（毫秒）。Windows 实际由系统通知中心决定，这里只是建议值。
_MESSAGE_MS = 8_000
_DEFAULT_VOLUME = 0.35

# 合成好的 WAV 落盘一次就够：进程内共享，且**不主动删除** —— 可能还有另一个
# Notifier 正在播它，或者 QSoundEffect 还在异步加载。TEMP 目录交给系统回收。
_sound_lock = threading.Lock()
_sound_path: "str | None" = None

# Notifier 必须是个 QObject：跨线程投递（QMetaObject.invokeMethod）只认 QObject，
# 普通 Python 类会直接 TypeError。QtCore 一般都在，但这里仍然兜一层：
# 模块**导入**绝不能因为缺 Qt 而失败（否则一条 import 就能把整个 UI 拖死）。
try:
    from PyQt6.QtCore import QObject as _NotifierBase
    from PyQt6.QtCore import pyqtSlot as _pyqtSlot
except Exception:                                   # pragma: no cover - 缺 Qt 的极端环境
    _NotifierBase = object

    def _pyqtSlot(*_args, **_kwargs):               # 退化：直接返回原函数
        def _decorator(func):
            return func
        return _decorator


def build_chime_wav(sample_rate: int = SAMPLE_RATE,
                    tones: "tuple[tuple[float, float], ...]" = TONE_SPECS,
                    amplitude: float = _AMPLITUDE) -> bytes:
    """在内存里合成提示音，返回完整的 WAV 字节串（16-bit 单声道 PCM）。

    为什么加淡入淡出：直接切一个正弦波的开头/结尾，波形上是个垂直台阶，
    扬声器会先"啪"一声（click）。用线性包络把首尾各 ~12ms 拉起来/放下去就没了。
    """
    frames = bytearray()
    for freq, duration in tones:
        count = max(1, int(sample_rate * duration))
        # 淡入淡出都不能超过半个音的时长，否则两声之间会断出声洞
        fade = max(1, min(int(sample_rate * _FADE_SECONDS), count // 2))
        for i in range(count):
            if i < fade:
                envelope = i / fade                     # 淡入
            elif i >= count - fade:
                # 淡出：用 count-1-i，保证**最后一个采样点正好是 0**（用 count-i 的话
                # 尾值停在 1/fade 的台阶上，还是会有一丝"啪"）。测试里卡了这一点。
                envelope = (count - 1 - i) / fade
            else:
                envelope = 1.0
            value = amplitude * envelope * math.sin(2.0 * math.pi * freq * i / sample_rate)
            frames += struct.pack("<h", int(max(-1.0, min(1.0, value)) * 32767))

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(bytes(frames))
    return buffer.getvalue()


def sound_file_path() -> str:
    """把合成好的提示音写进临时文件并返回路径（全进程只写一次）。

    QSoundEffect 只吃文件/URL，不认内存里的 bytes —— 所以必须落盘一次。
    用模块级缓存 + 锁：多个 Notifier 实例、甚至多线程同时首次触发时，
    不会各写一份、也不会互相覆盖。
    """
    global _sound_path
    with _sound_lock:
        if _sound_path and os.path.exists(_sound_path):
            return _sound_path
        data = build_chime_wav()
        fd, path = tempfile.mkstemp(prefix="mchat-notify-", suffix=".wav")
        with os.fdopen(fd, "wb") as handle:      # fdopen 接管 fd 的关闭
            handle.write(data)
        _sound_path = path
        return path


def tray_available() -> bool:
    """系统是否支持托盘气泡通知。单独抽成函数，方便测试强制走某一分支。"""
    try:
        from PyQt6.QtWidgets import QSystemTrayIcon
        return bool(QSystemTrayIcon.isSystemTrayAvailable())
    except Exception:
        # 没有 QtWidgets（或没有平台插件）时按"不支持"处理，绝不往外抛
        return False


def _fallback_icon():
    """兜底托盘图标：一个纯色小方块。

    Windows 上图标为空（null）的托盘项**不会显示气泡**，所以 ``icon=None`` 时
    必须塞一个非空图标进去，不能让它空着。用 QPainter 现画，不依赖图片资源。
    """
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QColor, QIcon, QPainter, QPixmap
    pixmap = QPixmap(16, 16)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setBrush(QColor("#3B82F6"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(1, 1, 14, 14)
    finally:
        painter.end()          # 不 end() 的话 Qt 会警告 painter 未结束
    return QIcon(pixmap)


def _on_gui_thread() -> bool:
    """当前是否在 Qt 主线程。

    QSystemTrayIcon / QSoundEffect **只能在 GUI 线程碰**，从工作线程直接调
    轻则不生效、重则段错误（段错误是 try/except 兜不住的，所以必须提前判断）。
    """
    from PyQt6.QtCore import QCoreApplication, QThread
    app = QCoreApplication.instance()
    if app is None:
        return True            # 干脆没有 Qt 应用，交给上层 try/except 兜
    return QThread.currentThread() is app.thread()


class Notifier(_NotifierBase):
    """桌面通知 + 提示音。构造永不失败，``message()`` 永不抛异常。

    继承 QObject 只为一件事：Qt 的跨线程投递（QueuedConnection）要求目标是 QObject。
    """

    def __init__(self, icon=None):
        super().__init__()
        # icon: QIcon | None —— 托盘图标；None 时现画一个兜底图标（见 _fallback_icon）
        self._enabled = True
        self._sound_enabled = True
        self._icon = icon
        self._tray = None          # QSystemTrayIcon | None
        self._effect = None        # QSoundEffect | None（延迟创建）
        self._sound_broken = False # 试过一次就不反复重试 import，免得每条消息都卡
        self._build_tray()

    # ---- 构造 / 状态 ------------------------------------------------------
    def _build_tray(self) -> None:
        """建托盘项。没有 QApplication 就不建 —— 那会直接崩，宁可不通知。"""
        try:
            from PyQt6.QtWidgets import QApplication, QSystemTrayIcon
        except Exception:
            return
        if QApplication.instance() is None:
            return
        try:
            tray = QSystemTrayIcon(self._icon if self._icon is not None else _fallback_icon())
            tray.setToolTip("MChat")
            self._tray = tray
        except Exception:
            self._tray = None      # 建不出来就当作"没有托盘"，后续全部静默跳过

    def available(self) -> bool:
        """系统是否支持托盘气泡通知。不支持时构造仍成功，只是 message() 不弹气泡。"""
        if self._tray is None:
            return False
        return tray_available()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    def set_sound_enabled(self, enabled: bool) -> None:
        self._sound_enabled = bool(enabled)

    def enabled(self) -> bool:
        return self._enabled

    def sound_enabled(self) -> bool:
        return self._sound_enabled

    # ---- 对外入口 --------------------------------------------------------
    def message(self, title: str, body: str) -> None:
        """弹一条系统通知（+ 可选提示音）。**任何失败都吞掉，绝不抛给调用方。**"""
        try:
            if not self._enabled:
                return
        except Exception:
            return
        try:
            title, body = str(title), str(body)
        except Exception:
            title, body = "MChat", ""

        try:
            if not _on_gui_thread():
                # 非 GUI 线程：排队丢回主线程执行。投不出去就静默丢弃 —— 宁可少一条通知，
                # 也绝不在工作线程里碰 QSystemTrayIcon（那可能整个进程段错误，兜不住）。
                self._post_to_gui(title, body)
                return
        except Exception:
            pass
        self._deliver(title, body)

    def stop(self) -> None:
        """收尾：藏掉并销毁托盘图标、停掉声音。幂等，重复调用无副作用。"""
        tray, self._tray = self._tray, None
        if tray is not None:
            try:
                tray.hide()
            except Exception:
                pass
            try:
                tray.deleteLater()   # 有事件循环时真正释放；没有也只是排个队，无害
            except Exception:
                pass
        effect, self._effect = self._effect, None
        if effect is not None:
            try:
                effect.stop()
            except Exception:
                pass

    # ---- 内部实现 --------------------------------------------------------
    def _post_to_gui(self, title: str, body: str) -> bool:
        """把通知投递到 GUI 线程（QueuedConnection），返回是否投递成功。"""
        try:
            from PyQt6.QtCore import Q_ARG, QMetaObject, Qt
            QMetaObject.invokeMethod(self, "_deliver_slot",
                                     Qt.ConnectionType.QueuedConnection,
                                     Q_ARG(str, title), Q_ARG(str, body))
            return True
        except Exception:
            return False

    def _deliver(self, title: str, body: str) -> None:
        # 排队投递可能被延后到用户关掉通知开关之后，所以这里再确认一次开关状态
        try:
            if not self._enabled:
                return
        except Exception:
            return
        try:
            self._send_to_tray(title, body)
        except Exception:
            pass                   # 气泡失败不影响响铃（两者互不牵连）
        try:
            if self._sound_enabled:
                self._play_sound()
        except Exception:
            pass

    def _send_to_tray(self, title: str, body: str) -> None:
        """真·托盘气泡。单独一个方法，测试里替换掉它就能记录"托盘是否被调用"。"""
        tray = self._tray
        if tray is None or not self.available():
            return                 # 没托盘：静默跳过，调用方另有兜底展示
        try:
            if not tray.isVisible():
                tray.show()        # Windows 上没 show() 过的托盘项不会出气泡
        except Exception:
            pass
        try:
            from PyQt6.QtWidgets import QSystemTrayIcon
            icon = QSystemTrayIcon.MessageIcon.Information
        except Exception:
            icon = None
        # body 里可能带换行/超长文本，交给系统通知中心自己截断，这里不做加工
        try:
            tray.showMessage(title, body, icon, _MESSAGE_MS)
        except TypeError:
            # 老版本 PyQt 的签名不带 icon/msecs 参数，退化调用
            tray.showMessage(title, body)

    # ---- 声音 ------------------------------------------------------------
    def _ensure_effect(self):
        """延迟创建 QSoundEffect（返回 None 表示这条链走不通）。"""
        if self._effect is not None:
            return self._effect
        if self._sound_broken:
            return None
        try:
            from PyQt6.QtCore import QUrl
            from PyQt6.QtMultimedia import QSoundEffect
            from PyQt6.QtWidgets import QApplication
            if QApplication.instance() is None:
                return None
            path = sound_file_path()
            effect = QSoundEffect()
            effect.setSource(QUrl.fromLocalFile(path))
            effect.setVolume(_DEFAULT_VOLUME)
            effect.setLoopCount(1)
            self._effect = effect
            return effect
        except Exception:
            self._sound_broken = True   # 记下来：音频后端缺失是常态，别每条消息都重试
            return None

    def _play_sound(self) -> None:
        """响一声：先试 QtMultimedia，失败再退到系统提示音。"""
        if self._play_qt_sound():
            return
        self._play_fallback_sound()

    def _play_qt_sound(self) -> bool:
        try:
            effect = self._ensure_effect()
            if effect is None:
                return False
            from PyQt6.QtMultimedia import QSoundEffect
            if effect.status() == QSoundEffect.Status.Error:
                return False       # 加载失败（比如后端没有可用的音频设备）
            effect.stop()          # 连续来消息时打断上一条，不叠音
            effect.play()
            return True
        except Exception:
            return False

    def _play_fallback_sound(self) -> None:
        """降级链末端的"能响就行"。整体吞异常：没有声卡也不该报错。"""
        try:
            if sys.platform.startswith("win"):
                import winsound
                winsound.MessageBeep()     # Windows 自带提示音，不经过 Qt 音频后端
                return
        except Exception:
            pass                   # MessageBeep 在无音频会话下会抛 RuntimeError
        try:
            from PyQt6.QtWidgets import QApplication
            QApplication.beep()
        except Exception:
            pass                   # 连 beep() 都失败：那就真的没声音，安静收场

    @_pyqtSlot(str, str)
    def _deliver_slot(self, title: str, body: str) -> None:
        """QueuedConnection 的落地端（跨线程投递时才会走到）。"""
        self._deliver(title, body)
