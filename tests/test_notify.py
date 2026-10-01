"""桌面通知 + 提示音模块（mchat.ui.notify）的无头回归。

为什么值得单独测：这条链路是"最容易在别人机器上坏，又最难复现"的那一类。
托盘缺失、音频后端缺失、开关关掉、退出时收尾 —— 每一种都要**静默成功**，
一旦漏出异常就是整个聊天窗口崩掉。所以这里盯死四件事：

1. 无托盘环境下 Notifier 仍能构造，``available()`` 老老实实给 bool；
2. 合成出来的提示音是**合法 WAV**（wave 能打开、帧数/采样率对得上、首尾淡入淡出）；
3. 开关真的生效 —— 关掉总开关既不弹气泡也不响；关掉声音则只弹不响
   （靠 monkeypatch 记录"托盘/声音有没有被调用"，不看现象看调用）；
4. ``stop()`` 幂等、``message()`` 在任何开关状态下都不抛异常。

用法：``QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe -m pytest tests/test_notify.py -q``

注意：**不要**在本文件里 ``os.environ`` 设 QT_QPA_PLATFORM —— 那是进程级变量，
pytest 会先把所有测试模块 import 一遍，一设就把 test_ui / test_security_tool 一起
拽到无头平台（它们靠真实字体渲染断言）。跟其它 UI 测试一样，由外部命令给这个变量。
"""
from __future__ import annotations

import io
import os
import struct
import sys
import threading
import wave

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6.QtWidgets import QApplication, QSystemTrayIcon            # noqa: E402

from mchat.ui import notify as notify_mod                            # noqa: E402
from mchat.ui.notify import (Notifier, SAMPLE_RATE, TONE_SPECS,      # noqa: E402
                             build_chime_wav, sound_file_path)


@pytest.fixture(scope="module")
def app():
    """整个模块共用一个 QApplication（多个会崩）。"""
    return QApplication.instance() or QApplication([])


@pytest.fixture
def no_tray(monkeypatch):
    """强制"系统没有托盘"——本机（offscreen 或带托盘）都要能走到这条分支。"""
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: False))


def _recorder():
    """返回 (calls, func)：calls 记录每次调用的参数，func 就是可替换的假实现。

    注意：往 **PyQt 类**上 monkeypatch 时必须再包一层 ``staticmethod``（见下面几个用例）。
    普通函数塞进类属性会被当成描述符，调用时把实例自己当成第一个参数，记录下来的
    参数就整体错位一格 —— 这是踩过的坑。
    """
    calls: list = []

    def func(*args, **kwargs):
        calls.append(args)

    return calls, func


# ---- 构造 / available ----------------------------------------------------

def test_construct_and_available_is_bool(app, no_tray):
    """无托盘环境下也要能构造成功，available() 必须是干净的 bool。"""
    notifier = Notifier()
    assert notifier.available() is False          # no_tray 夹具已强制为假
    assert isinstance(notifier.available(), bool)


def test_defaults_are_on(app, no_tray):
    notifier = Notifier()
    assert notifier.enabled() is True
    assert notifier.sound_enabled() is True


def test_construct_without_qapplication_does_not_crash(monkeypatch):
    """极端情况：连 QApplication 都还没有时构造 —— 不许崩，只是没有托盘。

    不能真删掉单例，所以只把 instance() 骗成 None，验证"没有 app 就不建托盘"这条路。
    """
    monkeypatch.setattr(QApplication, "instance", staticmethod(lambda: None))
    notifier = Notifier()
    assert notifier.available() is False
    notifier.message("t", "b")                    # 也不许抛


def test_construct_with_explicit_icon(app, no_tray):
    """外部给了图标（托盘图标）时要能用它，而不是走兜底画的圆点。"""
    from PyQt6.QtGui import QIcon, QPixmap
    icon = QIcon(QPixmap(16, 16))
    notifier = Notifier(icon=icon)
    assert notifier._tray is not None             # 托盘项照样建起来了
    assert notifier._tray.icon().isNull() is False


# ---- 合成的提示音 --------------------------------------------------------

def test_chime_wav_is_valid_wav(app):
    """内存合成的字节必须是合法 WAV：单声道 / 16bit / 采样率对 / 帧数 = 两声之和。"""
    data = build_chime_wav()
    assert data[:4] == b"RIFF" and data[8:12] == b"WAVE"

    with wave.open(io.BytesIO(data), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == SAMPLE_RATE
        frame_count = handle.getnframes()
        frames = handle.readframes(frame_count)

    expected = sum(int(SAMPLE_RATE * duration) for _freq, duration in TONE_SPECS)
    assert frame_count == expected > 0
    assert len(frames) == frame_count * 2         # 16bit 单声道 = 2 字节/帧


def test_chime_fades_in_and_out(app):
    """首尾包络都要落在 0 附近 —— 否则扬声器上会"啪"一声（click）。"""
    with wave.open(io.BytesIO(build_chime_wav()), "rb") as handle:
        frames = handle.readframes(handle.getnframes())
    first = struct.unpack("<h", frames[:2])[0]
    last = struct.unpack("<h", frames[-2:])[0]
    assert abs(first) <= 2
    assert abs(last) <= 2


def test_chime_has_energy_in_both_tones(app):
    """两声都要真的有点东西（不是静音 WAV）。取每声的中段算 RMS。"""
    with wave.open(io.BytesIO(build_chime_wav()), "rb") as handle:
        frame_count = handle.getnframes()
        frames = handle.readframes(frame_count)
    samples = struct.unpack(f"<{frame_count}h", frames)
    half = frame_count // 2
    middle = frame_count // 8                     # 避开淡入淡出区
    for start, end in ((middle, half - middle), (half + middle, frame_count - middle)):
        segment = samples[start:end]
        rms = (sum(value * value for value in segment) / len(segment)) ** 0.5
        assert rms > 1000, f"第 {start}-{end} 帧几乎是静音，不代表提示音"


def test_sound_file_written_once_and_readable(app):
    """落盘的 WAV 能被 wave 打开（QSoundEffect 只吃文件），且进程内只写一份。"""
    path = sound_file_path()
    assert os.path.exists(path)
    assert sound_file_path() == path               # 缓存命中，不重复写临时文件
    with wave.open(path, "rb") as handle:
        assert handle.getnframes() > 0
        assert handle.getframerate() == SAMPLE_RATE


def test_sound_file_is_shared_between_instances(app):
    """第二个 Notifier 复用模块级缓存的那一份临时文件，不重复落盘。"""
    path = sound_file_path()
    assert notify_mod._sound_path == path
    Notifier()                                     # 再建一个实例：共用同一份，不报错
    assert sound_file_path() == path


# ---- 开关（用 monkeypatch 记录调用，不看现象） ----------------------------

def test_disabled_is_completely_silent(app, monkeypatch):
    """总开关关掉：既不调托盘也不放声音，而且不报错。"""
    notifier = Notifier()
    tray_calls, tray_rec = _recorder()
    sound_calls, sound_rec = _recorder()
    monkeypatch.setattr(notifier, "_send_to_tray", tray_rec)
    monkeypatch.setattr(notifier, "_play_sound", sound_rec)

    notifier.set_enabled(False)
    assert notifier.enabled() is False
    notifier.message("标题", "正文")

    assert tray_calls == []
    assert sound_calls == []


def test_sound_disabled_still_notifies(app, monkeypatch):
    """声音开关关掉：气泡照弹，只是不响。"""
    notifier = Notifier()
    tray_calls, tray_rec = _recorder()
    sound_calls, sound_rec = _recorder()
    monkeypatch.setattr(notifier, "_send_to_tray", tray_rec)
    monkeypatch.setattr(notifier, "_play_sound", sound_rec)

    notifier.set_sound_enabled(False)
    assert notifier.sound_enabled() is False
    notifier.message("标题", "正文")

    assert tray_calls == [("标题", "正文")]
    assert sound_calls == []


def test_enabled_notifies_and_rings(app, monkeypatch):
    """两个开关都开着：托盘和声音各被调用一次。"""
    notifier = Notifier()
    tray_calls, tray_rec = _recorder()
    sound_calls, sound_rec = _recorder()
    monkeypatch.setattr(notifier, "_send_to_tray", tray_rec)
    monkeypatch.setattr(notifier, "_play_sound", sound_rec)

    notifier.message("标题", "正文")

    assert tray_calls == [("标题", "正文")]
    assert sound_calls == [()]


def test_switches_toggle_back(app, monkeypatch):
    """开关能来回拨（另一路会在设置面板里反复切）。"""
    notifier = Notifier()
    tray_calls, tray_rec = _recorder()
    monkeypatch.setattr(notifier, "_send_to_tray", tray_rec)

    notifier.set_enabled(False)
    notifier.set_enabled(True)
    notifier.message("a", "b")
    assert len(tray_calls) == 1


def test_consecutive_messages_do_not_stack_or_crash(app, monkeypatch):
    """连点消息：连着调 5 次，每次都走到，不排队也不崩（声音自己会打断自己）。"""
    notifier = Notifier()
    tray_calls, tray_rec = _recorder()
    sound_calls, sound_rec = _recorder()
    monkeypatch.setattr(notifier, "_send_to_tray", tray_rec)
    monkeypatch.setattr(notifier, "_play_sound", sound_rec)

    for index in range(5):
        notifier.message(f"标题{index}", "正文")

    assert len(tray_calls) == 5
    assert len(sound_calls) == 5


# ---- 真正打到 QSystemTrayIcon / 真实播放链路 ------------------------------

def test_real_tray_call_when_available(app, monkeypatch):
    """托盘可用时，message() 必须真的调到 QSystemTrayIcon.showMessage（标题/正文原样传）。"""
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True))
    tray_calls, tray_rec = _recorder()
    monkeypatch.setattr(QSystemTrayIcon, "showMessage", staticmethod(tray_rec))  # 不真弹气泡
    monkeypatch.setattr(QSystemTrayIcon, "show", lambda self: None)              # 也不真建图标

    notifier = Notifier()
    try:
        assert notifier.available() is True
        notifier.set_sound_enabled(False)          # 静音，只验气泡
        notifier.message("有人@你", "在 #通用 里提到了你")
    finally:
        notifier.stop()

    assert tray_calls, "托盘可用却没有调用 showMessage"
    assert tray_calls[0][0] == "有人@你"
    assert tray_calls[0][1] == "在 #通用 里提到了你"


def test_no_tray_means_no_tray_call(app, no_tray, monkeypatch):
    """没有托盘时**不调** showMessage（既不弹气泡，也不弹替代弹窗打扰用户）。"""
    tray_calls, tray_rec = _recorder()
    monkeypatch.setattr(QSystemTrayIcon, "showMessage", staticmethod(tray_rec))

    notifier = Notifier()
    notifier.set_sound_enabled(False)
    notifier.message("标题", "正文")

    assert tray_calls == []


def test_real_message_end_to_end_does_not_raise(app):
    """真实链路整条走一遍（不 monkeypatch 内部）。

    这是唯一一条会真去播声音的用例：若本机有扬声器会听到一声很短的"叮-咚"。
    故意的 —— 光看 mock 证明不了 QSoundEffect / winsound 在真环境下不会抛。
    """
    notifier = Notifier()
    try:
        notifier.message("MChat", "真实链路：不抛异常即通过")
        notifier.message("", "")                   # 空标题/空正文也不许炸
    finally:
        notifier.stop()


# ---- 声音降级链 ----------------------------------------------------------

def test_sound_prefers_qt(app, monkeypatch):
    """Qt 这条路能走通时，不该再去打扰系统提示音。"""
    notifier = Notifier()
    qt_calls, qt_rec = _recorder()
    fallback_calls, fallback_rec = _recorder()
    monkeypatch.setattr(notifier, "_play_qt_sound", lambda: (qt_rec(), True)[1])
    monkeypatch.setattr(notifier, "_play_fallback_sound", fallback_rec)

    notifier._play_sound()

    assert len(qt_calls) == 1
    assert fallback_calls == []


def test_sound_falls_back_when_qt_unavailable(app, monkeypatch):
    """Qt 播不出来（import 失败 / 没有音频后端）时必须降级，而不是"没声音还报错"。"""
    notifier = Notifier()
    monkeypatch.setattr(notifier, "_play_qt_sound", lambda: False)

    if sys.platform.startswith("win"):
        import winsound
        beep_calls, beep_rec = _recorder()
        monkeypatch.setattr(winsound, "MessageBeep", beep_rec)
        notifier._play_sound()
        assert len(beep_calls) == 1, "Windows 上应退到 winsound.MessageBeep()"
    else:
        beep_calls, beep_rec = _recorder()
        monkeypatch.setattr(QApplication, "beep", staticmethod(beep_rec))
        notifier._play_sound()
        assert len(beep_calls) == 1, "非 Windows 上应退到 QApplication.beep()"


def test_qt_sound_error_falls_back(app, monkeypatch):
    """QSoundEffect 加载失败（Status.Error，比如后端没有音频设备）→ 必须落兜底提示音。

    本机若根本没有 QtMultimedia，那就更该走兜底 —— 两种情况下断言的都是"兜底被调用"，
    所以这条用例不需要 skip 也能在所有机器上跑出确定结论。
    """
    notifier = Notifier()
    fallback_calls, fallback_rec = _recorder()
    monkeypatch.setattr(notifier, "_play_fallback_sound", fallback_rec)
    try:
        from PyQt6.QtMultimedia import QSoundEffect
    except Exception:
        # 没有 QtMultimedia：Qt 那条链本就不可用，兜底必须接管
        notifier._play_sound()
        assert len(fallback_calls) == 1
        return

    # 强制"音频后端坏了"：把状态直接改成 Error
    monkeypatch.setattr(QSoundEffect, "status", lambda self: QSoundEffect.Status.Error)
    assert notifier._play_qt_sound() is False
    notifier._play_sound()
    assert len(fallback_calls) == 1


# ---- stop() --------------------------------------------------------------

def test_stop_is_idempotent(app, no_tray):
    """收尾必须能重复调：退出流程里有多个可能路径都会喊一声 stop()。"""
    notifier = Notifier()
    notifier.stop()
    notifier.stop()
    notifier.stop()
    assert notifier.available() is False
    notifier.message("t", "b")                    # 收尾之后再调也不许抛


def test_stop_stops_sound_and_tray(app, monkeypatch):
    """stop() 之后，内部引用要放开（避免退出时还持有托盘/播放器对象）。"""
    notifier = Notifier()
    notifier.stop()
    assert notifier._tray is None
    assert notifier._effect is None


# ---- 线程安全 ------------------------------------------------------------

def test_message_from_worker_thread_does_not_raise(app, monkeypatch):
    """从工作线程调 message()：不许抛，且**不能在非 GUI 线程里碰托盘**。

    Qt 的托盘/播放器只能在主线程用，摸错线程可能直接段错误（try/except 兜不住），
    所以模块会把通知排队丢回主线程 —— 这里验证投递之后主线程能正常收到。
    """
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True))
    tray_calls, tray_rec = _recorder()
    monkeypatch.setattr(QSystemTrayIcon, "showMessage", staticmethod(tray_rec))
    monkeypatch.setattr(QSystemTrayIcon, "show", lambda self: None)

    notifier = Notifier()
    notifier.set_sound_enabled(False)
    errors: list = []

    def worker():
        try:
            notifier.message("来自线程", "正文")
        except BaseException as exc:              # noqa: BLE001 - 测试要抓所有异常
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors == []

    # 排队的事件要主线程跑事件循环才落地
    for _ in range(200):
        if tray_calls:
            break
        app.processEvents()
    assert tray_calls and tray_calls[0][0] == "来自线程"
    notifier.stop()
