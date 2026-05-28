"""
Shadow Recorder
  录屏区 — 透明悬浮框 + 系统音频轨（ScreenCaptureKit，无需虚拟声卡）
  摄像区 — 摄像头画面 + 麦克风音轨

高度联动：录屏区内容高 + 摄像区内容高 = 800px（恒定）
输出：shadow_时间戳.mp4（450×800，含两条独立音轨）
首次运行需在「系统设置 → 隐私 → 屏幕录制」中授权。
"""
import sys, cv2, numpy as np, subprocess, threading, wave, os, time
from datetime import datetime

SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
SWIFT_BIN     = os.path.join(SCRIPT_DIR, "audio_capture")        # 系统音频录制
SUBTITLE_BIN  = os.path.join(SCRIPT_DIR, "subtitle_recognizer")  # 实时字幕识别

import sounddevice as sd

from PyQt6.QtWidgets import (QApplication, QWidget, QLabel, QPushButton,
                              QInputDialog)
from PyQt6.QtCore    import QTimer, Qt, QPoint, QObject, pyqtSignal, QRect
from PyQt6.QtGui     import (QImage, QPixmap, QPainter, QColor, QPen, QFont,
                              QFontMetrics)

import mss

WIN_W   = 450
TOTAL_H = 800
INIT_CH = 400
MIN_CH  = 100
MAX_CH  = 700
BORDER  = 3
HANDLE  = 12
TOPBAR  = 24
SR      = 44100


# ── Letterbox ────────────────────────────────────────────────────────────────
def letterbox(frame_bgr: np.ndarray, tw: int, th: int) -> np.ndarray:
    fh, fw = frame_bgr.shape[:2]
    scale  = min(tw / fw, th / fh)
    nw, nh = int(fw * scale), int(fh * scale)
    r = cv2.resize(frame_bgr, (nw, nh), interpolation=cv2.INTER_AREA)
    c = np.zeros((th, tw, 3), np.uint8)
    c[(th-nh)//2:(th-nh)//2+nh, (tw-nw)//2:(tw-nw)//2+nw] = r
    return c


# ── 音频录制器 ────────────────────────────────────────────────────────────────
class AudioRecorder:
    """从指定设备录音，停止后写为 WAV 文件。"""

    def __init__(self, device_idx=None):
        self.device = device_idx
        self.ch     = 1
        if device_idx is not None:
            try:
                self.ch = max(1, min(2, sd.query_devices(device_idx)['max_input_channels']))
            except Exception:
                pass
        self._lock   = threading.Lock()
        self._chunks = []
        self._stream = None
        self.active  = False
        self.error   = None

    def start(self):
        self._chunks = []
        self.error   = None
        if self.device is None:
            return
        try:
            self._stream = sd.InputStream(
                device=self.device, samplerate=SR,
                channels=self.ch, dtype='float32',
                blocksize=1024, callback=self._cb)
            self._stream.start()
            self.active = True
        except Exception as e:
            self.error  = str(e)
            self.active = False

    def _cb(self, indata, frames, time, status):
        with self._lock:
            self._chunks.append(indata.copy())

    def stop_and_save(self, path: str) -> bool:
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        self.active = False
        with self._lock:
            chunks = list(self._chunks)
        if not chunks:
            return False
        data  = np.concatenate(chunks, axis=0)
        data16 = np.clip(data * 32767, -32768, 32767).astype(np.int16)
        try:
            with wave.open(path, 'w') as wf:
                wf.setnchannels(self.ch)
                wf.setsampwidth(2)
                wf.setframerate(SR)
                wf.writeframes(data16.tobytes())
            return True
        except Exception:
            return False


# ── 系统音频录制器（调用 Swift ScreenCaptureKit 工具） ──────────────────────────
class SystemAudioRecorder:
    """通过 audio_capture 二进制文件录制系统音频（不需要虚拟声卡）。"""

    def __init__(self):
        self._proc   = None
        self.active  = False
        self.error   = None

    def start(self, out_path: str):
        self.error  = None
        self.active = False
        if not os.path.exists(SWIFT_BIN):
            self.error = "audio_capture 工具不存在"
            return
        try:
            self._proc = subprocess.Popen(
                [SWIFT_BIN, out_path],
                stderr=subprocess.PIPE, text=True, bufsize=1)
            # 等待 READY（最多 6 秒）
            deadline = time.time() + 6
            ready    = False
            while time.time() < deadline:
                line = self._proc.stderr.readline()
                if not line:
                    break
                line = line.strip()
                if "READY" in line:
                    ready = True
                    break
                if "ERROR" in line or "TIP" in line:
                    self.error = line
                    self._proc.terminate()
                    self._proc = None
                    return
            if ready:
                self.active = True
                # 持续消耗 stderr 防止缓冲区阻塞
                threading.Thread(target=self._drain, daemon=True).start()
            else:
                self.error = "READY 超时"
                if self._proc:
                    self._proc.terminate()
                    self._proc = None
        except Exception as e:
            self.error = str(e)

    def _drain(self):
        if self._proc and self._proc.stderr:
            for _ in self._proc.stderr:
                pass

    def stop(self):
        self.active = False
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=6)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None


# ── 音频设备检测 ──────────────────────────────────────────────────────────────
def list_input_devices():
    """返回所有可用输入设备列表 [(idx, name), ...]。"""
    result = []
    for i, d in enumerate(sd.query_devices()):
        if d['max_input_channels'] > 0:
            result.append((i, d['name']))
    return result

def auto_detect_devices():
    """
    自动分配：
      sys_idx  — 系统音频（优先 BlackHole/Soundflower/Loopback）
      mic_idx  — 麦克风（系统默认输入）
    """
    devices = sd.query_devices()
    sys_idx = None
    for i, d in enumerate(devices):
        if d['max_input_channels'] > 0:
            n = d['name'].lower()
            if any(k in n for k in ('blackhole', 'soundflower', 'loopback', 'virtual')):
                sys_idx = i
                break

    mic_idx = None
    try:
        di = sd.default.device[0]
        if di is not None and int(di) >= 0:
            mic_idx = int(di)
    except Exception:
        pass

    return sys_idx, mic_idx


# ── 后台合并信号 ──────────────────────────────────────────────────────────────
class MergeSignals(QObject):
    done = pyqtSignal(bool, str)   # (success, output_path)


# ── ffmpeg 合并（后台线程） ────────────────────────────────────────────────────
def merge_and_save(tmp_video, sys_wav, mic_wav, output, signals: MergeSignals):
    def _run():
        inputs = [tmp_video]
        maps, meta = ['-map', '0:v'], []
        ai = 0
        for wav, title in [(sys_wav, 'System Audio'), (mic_wav, 'Microphone')]:
            if wav and os.path.exists(wav):
                inputs.append(wav)
                maps += ['-map', f'{len(inputs)-1}:a']
                meta += [f'-metadata:s:a:{ai}', f'title={title}']
                ai   += 1

        cmd = ['ffmpeg', '-y']
        for i in inputs:
            cmd += ['-i', i]
        cmd += maps + meta + ['-c:v', 'copy', '-c:a', 'aac', output]

        result = subprocess.run(cmd, capture_output=True, text=True)
        ok = result.returncode == 0

        for f in [tmp_video, sys_wav, mic_wav]:
            if f and os.path.exists(f):
                try: os.remove(f)
                except: pass

        signals.done.emit(ok, output)

    threading.Thread(target=_run, daemon=True).start()


# ── 字幕识别进程管理 ──────────────────────────────────────────────────────────
SUBTITLE_LANGS = [("EN", "en-US"), ("ZH", "zh-CN"), ("JP", "ja-JP")]

class SubtitleProcess:
    """管理 subtitle_recognizer 进程，通过回调推送识别文本。"""

    def __init__(self, on_text):
        self._proc      = None
        self._on_text   = on_text
        self.active     = False
        self.error      = None
        self._lang      = "en-US"

    def start(self, lang="en-US"):
        if self.active:
            self.stop()
        self._lang  = lang
        self.error  = None
        if not os.path.exists(SUBTITLE_BIN):
            self.error = "subtitle_recognizer 工具不存在"
            return False
        try:
            self._proc = subprocess.Popen(
                [SUBTITLE_BIN, lang],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True, bufsize=1)
            ready = threading.Event()
            threading.Thread(target=self._watch_stderr,
                             args=(ready,), daemon=True).start()
            threading.Thread(target=self._watch_stdout, daemon=True).start()
            if not ready.wait(timeout=8):
                self.error = "启动超时"
                self.stop()
                return False
            self.active = True
            return True
        except Exception as e:
            self.error = str(e)
            return False

    def _watch_stderr(self, ready_event: threading.Event):
        if not self._proc: return
        for line in self._proc.stderr:
            line = line.strip()
            if "READY" in line:
                ready_event.set()
            elif "ERROR" in line:
                self.error = line
                ready_event.set()   # unblock even on error

    def _watch_stdout(self):
        if not self._proc: return
        for line in self._proc.stdout:
            text = line.strip()
            if text:
                self._on_text(text)

    def stop(self):
        self.active = False
        if self._proc:
            self._proc.terminate()
            try:   self._proc.wait(timeout=4)
            except subprocess.TimeoutExpired: self._proc.kill()
            self._proc = None


# ── 字幕卡片控件 ───────────────────────────────────────────────────────────────
class SubtitleCard(QWidget):
    """
    半透明黑色圆角卡片，显示识别字幕。
    自动定位在录屏区内容底部，4 秒无更新后渐隐。
    """
    PADDING_H = 14
    PADDING_V = 10
    RADIUS    = 10
    FONT_SIZE = 13

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._text    = ""
        self._opacity = 0.0

        self._hold_t  = QTimer(self)   # 显示保持计时器
        self._hold_t.setSingleShot(True)
        self._hold_t.timeout.connect(self._begin_fade)

        self._fade_t  = QTimer(self)   # 渐隐动画
        self._fade_t.timeout.connect(self._tick_fade)

        self.hide()

    # ── 外部调用 ──────────────────────────────────────────────────────────
    def show_text(self, text: str):
        self._text    = text
        self._opacity = 1.0
        self._fade_t.stop()
        self._hold_t.stop()
        self._hold_t.start(4000)
        self._reposition()
        self.show()
        self.raise_()
        self.update()

    def clear(self):
        self._hold_t.stop()
        self._fade_t.stop()
        self._opacity = 0.0
        self._text    = ""
        self.hide()

    # ── 动画 ──────────────────────────────────────────────────────────────
    def _begin_fade(self):
        self._fade_t.start(40)

    def _tick_fade(self):
        self._opacity = max(0.0, self._opacity - 0.04)
        if self._opacity <= 0.0:
            self._fade_t.stop()
            self.hide()
        else:
            self.update()

    # ── 定位（紧贴内容区底部）────────────────────────────────────────────
    def _reposition(self):
        if not self._text or not self.parent():
            return
        par   = self.parent()
        fm    = QFontMetrics(QFont("Helvetica", self.FONT_SIZE))
        max_w = par.width() - 40
        br    = fm.boundingRect(
            QRect(0, 0, max_w, 2000),
            Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignCenter,
            self._text)
        w = min(br.width()  + self.PADDING_H * 2, par.width() - 20)
        h = br.height() + self.PADDING_V  * 2
        x = (par.width() - w) // 2
        # 内容区 = TOPBAR 到 (height - HANDLE)
        content_bottom = par.height() - HANDLE
        y = content_bottom - h - 12
        self.setGeometry(x, y, w, h)

    # ── 绘制 ──────────────────────────────────────────────────────────────
    def paintEvent(self, _):
        if not self._text or self._opacity <= 0.0:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setOpacity(self._opacity)

        # 卡片背景
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 210))
        p.drawRoundedRect(self.rect(), self.RADIUS, self.RADIUS)

        # 文字
        p.setOpacity(self._opacity)
        p.setPen(QColor(255, 255, 255))
        p.setFont(QFont("Helvetica", self.FONT_SIZE))
        inner = self.rect().adjusted(self.PADDING_H, self.PADDING_V,
                                     -self.PADDING_H, -self.PADDING_V)
        p.drawText(inner,
                   Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignCenter,
                   self._text)
        p.end()


# ── 录屏区窗口 ────────────────────────────────────────────────────────────────
class ScreenWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.sys_audio_ok = os.path.exists(SWIFT_BIN)  # Swift 工具是否存在

        self.setWindowFlags(Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(WIN_W)
        self._ch = INIT_CH
        self.resize(WIN_W, self._ch + TOPBAR + HANDLE)
        self.setMouseTracking(True)

        self._recording = False
        self._blink     = False
        self._blink_t   = QTimer(self)
        self._blink_t.timeout.connect(self._do_blink)

        self._drag_global_start = QPoint()
        self._drag_win_start    = QPoint()
        self._resizing  = False
        self._res_y0    = 0
        self._res_ch0   = 0

        self.on_ch_changed = None

        # ── 字幕卡片 ──────────────────────────────────────────────────────
        self._subtitle_card = SubtitleCard(self)
        self._subtitle_proc = None
        self._sub_lang_idx  = 0   # 语言索引（对应 SUBTITLE_LANGS）

        self._sub_btn = QPushButton("字幕", self)
        self._sub_btn.setFixedSize(42, 16)
        self._sub_btn.move(WIN_W - 54, 4)
        self._sub_btn.setStyleSheet(
            "QPushButton{color:#555;background:transparent;border:1px solid #444;"
            "border-radius:3px;font-size:9px;}"
            "QPushButton:hover{color:#aaa;border-color:#777;}"
            "QPushButton:checked{color:#44cc88;border-color:#44cc88;}")
        self._sub_btn.setCheckable(True)
        self._sub_btn.clicked.connect(self._toggle_subtitle)

        self._lang_btn = QPushButton("EN", self)
        self._lang_btn.setFixedSize(24, 16)
        self._lang_btn.move(WIN_W - 8 - 24, 4)
        self._lang_btn.setStyleSheet(
            "QPushButton{color:#555;background:transparent;border:1px solid #333;"
            "border-radius:3px;font-size:9px;}"
            "QPushButton:hover{color:#aaa;}")
        self._lang_btn.clicked.connect(self._cycle_lang)

    def set_sys_audio_ok(self, ok: bool):
        self.sys_audio_ok = ok
        self.update()

    # ── 公开 API ──────────────────────────────────────────────────────────
    @property
    def content_h(self):
        return self._ch

    def set_content_h(self, ch: int, emit=True):
        ch = max(MIN_CH, min(MAX_CH, ch))
        if ch == self._ch: return
        self._ch = ch
        self.resize(WIN_W, ch + TOPBAR + HANDLE)
        self.update()
        if emit and self.on_ch_changed:
            self.on_ch_changed(ch)

    def set_recording(self, on: bool):
        self._recording = on
        if on: self._blink_t.start(600)
        else:
            self._blink_t.stop(); self._blink = False
        self.update()

    def get_capture_region(self):
        pos = self.pos()
        return {"left": pos.x(), "top": pos.y() + TOPBAR,
                "width": WIN_W,  "height": max(1, self._ch)}

    def position_on_video(self, vx, vy, vw, vh):
        cx, cy = vx + vw//2, vy + vh//2
        self.move(cx - WIN_W//2, cy - self.height()//2)

    # ── 字幕控制 ──────────────────────────────────────────────────────────
    def _toggle_subtitle(self, checked: bool):
        if checked:
            lang_code = SUBTITLE_LANGS[self._sub_lang_idx][1]
            self._subtitle_proc = SubtitleProcess(self._on_subtitle_text)
            ok = self._subtitle_proc.start(lang_code)
            if not ok:
                print(f"[字幕] 启动失败: {self._subtitle_proc.error}")
                self._sub_btn.setChecked(False)
                self._subtitle_proc = None
            else:
                self._sub_btn.setText("字幕 ON")
        else:
            if self._subtitle_proc:
                self._subtitle_proc.stop()
                self._subtitle_proc = None
            self._subtitle_card.clear()
            self._sub_btn.setText("字幕")

    def _cycle_lang(self):
        self._sub_lang_idx = (self._sub_lang_idx + 1) % len(SUBTITLE_LANGS)
        label, code = SUBTITLE_LANGS[self._sub_lang_idx]
        self._lang_btn.setText(label)
        # 若字幕正在运行则重启
        if self._subtitle_proc and self._subtitle_proc.active:
            self._subtitle_proc.stop()
            self._subtitle_proc = SubtitleProcess(self._on_subtitle_text)
            self._subtitle_proc.start(code)

    def _on_subtitle_text(self, text: str):
        # 从后台线程安全更新 UI（通过 QTimer 单次触发）
        QTimer.singleShot(0, lambda: self._subtitle_card.show_text(text))

    def closeEvent(self, e):
        if self._subtitle_proc:
            self._subtitle_proc.stop()
        e.accept()

    # ── 内部 ──────────────────────────────────────────────────────────────
    def _do_blink(self):
        self._blink = not self._blink
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        h = self.height()
        blink_on = self._recording and self._blink
        clr = QColor("#ff3333") if blink_on else QColor("#00aaff")

        p.fillRect(0, 0, WIN_W, TOPBAR, QColor(0, 0, 0, 185))
        p.setPen(QPen(clr, BORDER)); p.setBrush(Qt.BrushStyle.NoBrush)
        b = BORDER // 2
        p.drawRect(b, b, WIN_W - BORDER, h - BORDER)

        mk = 14; p.setPen(QPen(clr, 2))
        for x, y, dx, dy in [(b,b,1,1),(WIN_W-b,b,-1,1),(b,h-b,1,-1),(WIN_W-b,h-b,-1,-1)]:
            p.drawLine(x, y, x+dx*mk, y); p.drawLine(x, y, x, y+dy*mk)

        p.fillRect(0, h-HANDLE, WIN_W, HANDLE, QColor(28, 28, 28, 220))
        cx = WIN_W // 2
        p.setPen(QColor(95, 95, 95))
        for off in (-12, 0, 12):
            p.drawLine(cx+off-5, h-6, cx+off+5, h-6)

        # 顶栏：尺寸 + 系统音频状态
        p.setFont(QFont("Helvetica", 9))
        p.setPen(QColor("#777"))
        p.drawText(8, 16, f"录屏区  {WIN_W}×{self._ch}")
        if blink_on:
            p.setPen(QColor("#ff4444"))
            p.setFont(QFont("Helvetica", 9, QFont.Weight.Bold))
            p.drawText(WIN_W - 140, 16, "⏺  REC")
        p.end()

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton: return
        ly = e.position().toPoint().y()
        gp = e.globalPosition().toPoint()
        if ly >= self.height() - HANDLE:
            self._resizing = True; self._res_y0 = gp.y(); self._res_ch0 = self._ch
        else:
            self._resizing          = False
            self._drag_global_start = gp
            self._drag_win_start    = self.pos()

    def mouseMoveEvent(self, e):
        ly = e.position().toPoint().y()
        self.setCursor(Qt.CursorShape.SizeVerCursor if ly >= self.height()-HANDLE
                       else Qt.CursorShape.OpenHandCursor)
        if e.buttons() != Qt.MouseButton.LeftButton: return
        gp = e.globalPosition().toPoint()
        if self._resizing:
            self.set_content_h(self._res_ch0 + gp.y() - self._res_y0)
        else:
            self.move(self._drag_win_start + (gp - self._drag_global_start))


# ── 摄像区窗口 ────────────────────────────────────────────────────────────────
class CameraWindow(QWidget):
    def __init__(self, screen_win: ScreenWindow, mic_idx, mic_name):
        super().__init__()
        self.screen_win = screen_win
        self.mic_idx    = mic_idx
        self.mic_name   = mic_name if mic_name else "无麦克风"

        self.setWindowFlags(Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.WindowStaysOnTopHint)
        self.setFixedWidth(WIN_W)
        self._ch = INIT_CH
        self.resize(WIN_W, self._ch + HANDLE)
        self.setMouseTracking(True)

        # 摄像画面
        self.cam_label = QLabel(self)
        self.cam_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cam_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        # REC 按钮
        self.rec_btn = QPushButton("● REC", self)
        self.rec_btn.setFixedSize(96, 36)
        self.rec_btn.clicked.connect(self._toggle_recording)
        self._style_rec(False)

        # 关闭按钮
        self.close_btn = QPushButton("✕", self)
        self.close_btn.setFixedSize(28, 28)
        self.close_btn.setStyleSheet(
            "QPushButton{color:#555;background:transparent;border:none;font-size:14px;}"
            "QPushButton:hover{color:#fff;background:rgba(180,0,0,180);border-radius:14px;}")
        self.close_btn.clicked.connect(QApplication.quit)

        # 状态标签（保存进度）
        self.status_label = QLabel("", self)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.status_label.setStyleSheet("color:#888; font-size:10px; background:transparent;")

        self._layout()

        self.on_ch_changed = None

        # 设备
        self.cap = cv2.VideoCapture(0)
        self.sct = mss.MSS()

        # 录制状态
        self.recording    = False
        self._vid_writer  = None
        self._tmp_video   = None
        self._tmp_sys     = None
        self._tmp_mic     = None
        self._rec_sys     = None   # SystemAudioRecorder（ScreenCaptureKit）
        self._rec_mic     = None   # AudioRecorder（麦克风）

        # 合并信号
        self._merge_sig = MergeSignals()
        self._merge_sig.done.connect(self._on_merge_done)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(33)

        # 拖拽/缩放
        self._drag_global_start = QPoint()
        self._drag_win_start    = QPoint()
        self._resizing  = False
        self._res_y0    = 0
        self._res_ch0   = 0

    # ── 音频设备 ──────────────────────────────────────────────────────────
    def set_mic_device(self, idx, name):
        self.mic_idx  = idx
        self.mic_name = name if name else "无麦克风"
        self.update()

    # ── 公开 API ──────────────────────────────────────────────────────────
    @property
    def content_h(self):
        return self._ch

    def set_content_h(self, ch: int, emit=True):
        ch = max(MIN_CH, min(MAX_CH, ch))
        if ch == self._ch: return
        self._ch = ch
        self.resize(WIN_W, ch + HANDLE)
        self._layout()
        self.update()
        if emit and self.on_ch_changed:
            self.on_ch_changed(ch)

    # ── 布局 ──────────────────────────────────────────────────────────────
    def _layout(self):
        ch = self._ch
        self.cam_label.setGeometry(0, 0, WIN_W, ch)
        self.rec_btn.move(WIN_W//2 - 48, ch - 46)
        self.close_btn.move(WIN_W - 34, 6)
        self.status_label.setGeometry(0, ch - 68, WIN_W, 18)

    def resizeEvent(self, e):
        self._layout(); super().resizeEvent(e)

    # ── 绘制 ──────────────────────────────────────────────────────────────
    def paintEvent(self, _):
        p = QPainter(self)
        h = self.height()
        p.fillRect(0, 0, WIN_W, h, QColor(10, 10, 10))
        clr = QColor("#ff3333") if self.recording else QColor("#1e1e1e")
        p.setPen(QPen(clr, BORDER)); p.setBrush(Qt.BrushStyle.NoBrush)
        b = BORDER // 2
        p.drawRect(b, b, WIN_W - BORDER, h - BORDER)

        p.fillRect(0, h-HANDLE, WIN_W, HANDLE, QColor(18, 18, 18))
        cx = WIN_W // 2
        p.setPen(QColor(72, 72, 72))
        for off in (-12, 0, 12):
            p.drawLine(cx+off-5, h-6, cx+off+5, h-6)

        # 手柄：摄像区尺寸 + 麦克风设备
        p.setFont(QFont("Helvetica", 8))
        info = f"摄像区 {WIN_W}×{self._ch}"
        p.setPen(QColor("#303030"))
        p.drawText(8, h - 2, info)

        mic_clr = QColor("#44cc88") if self.mic_idx is not None else QColor("#444")
        p.setPen(mic_clr)
        short_mic = self.mic_name[:20] + ("…" if len(self.mic_name) > 20 else "")
        p.drawText(WIN_W - 8 - p.fontMetrics().horizontalAdvance(f"🎤 {short_mic}"),
                   h - 2, f"🎤 {short_mic}")
        p.end()

    def mouseDoubleClickEvent(self, e):
        """双击手柄区域切换麦克风设备。"""
        if e.position().toPoint().y() >= self.height() - HANDLE:
            self._pick_mic()

    def _pick_mic(self):
        devs  = list_input_devices()
        items = ["（无音频）"] + [f"[{i}] {n}" for i, n in devs]
        cur   = 0
        if self.mic_idx is not None:
            for j, (i, _) in enumerate(devs):
                if i == self.mic_idx: cur = j + 1; break
        choice, ok = QInputDialog.getItem(
            self, "摄像区音频设备", "选择麦克风设备：", items, cur, False)
        if ok:
            if choice == "（无音频）":
                self.set_mic_device(None, None)
            else:
                idx_str = choice.split("]")[0].lstrip("[")
                try:
                    idx  = int(idx_str)
                    name = sd.query_devices(idx)['name']
                    self.set_mic_device(idx, name)
                except Exception:
                    pass

    # ── 录制控制 ──────────────────────────────────────────────────────────
    def _toggle_recording(self):
        if self.recording: self._stop()
        else:              self._start()

    def _start(self):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._tmp_video = f"_tmp_video_{ts}.mp4"
        self._tmp_sys   = f"_tmp_sys_{ts}.caf"   # ScreenCaptureKit 输出 CAF
        self._tmp_mic   = f"_tmp_mic_{ts}.wav"
        self._final_out = f"shadow_{ts}.mp4"

        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        self._vid_writer = cv2.VideoWriter(self._tmp_video, fourcc, 30, (WIN_W, TOTAL_H))

        # 系统音频（ScreenCaptureKit）
        self._rec_sys = SystemAudioRecorder()
        self._rec_sys.start(self._tmp_sys)
        if self._rec_sys.error:
            print(f"[系统音频] {self._rec_sys.error}")
            self.screen_win.set_sys_audio_ok(False)
        else:
            self.screen_win.set_sys_audio_ok(True)

        # 麦克风
        self._rec_mic = AudioRecorder(self.mic_idx)
        self._rec_mic.start()

        self.recording = True
        self.screen_win.set_recording(True)
        self._style_rec(True)
        self.status_label.setText("")
        self.update()

    def _stop(self):
        self.recording = False
        self.screen_win.set_recording(False)
        self._style_rec(False)
        self.rec_btn.setEnabled(False)
        self.status_label.setText("⏳ 正在保存...")
        self.update()

        # 停止视频
        if self._vid_writer:
            self._vid_writer.release()
            self._vid_writer = None

        # 停止系统音频（Swift 进程写 .caf 文件）
        sys_wav = None
        if self._rec_sys:
            self._rec_sys.stop()
            if os.path.exists(self._tmp_sys) and os.path.getsize(self._tmp_sys) > 0:
                sys_wav = self._tmp_sys
            self._rec_sys = None

        # 停止麦克风并保存 WAV
        mic_wav = None
        if self._rec_mic:
            if self._rec_mic.stop_and_save(self._tmp_mic):
                mic_wav = self._tmp_mic
            self._rec_mic = None

        # 后台合并
        merge_and_save(self._tmp_video, sys_wav, mic_wav,
                       self._final_out, self._merge_sig)

    def _on_merge_done(self, ok: bool, path: str):
        self.rec_btn.setEnabled(True)
        if ok:
            name = os.path.basename(path)
            self.status_label.setText(f"✓ 已保存：{name}")
            self.status_label.setStyleSheet("color:#44cc88;font-size:10px;background:transparent;")
        else:
            self.status_label.setText("⚠ 合并失败，检查 ffmpeg")
            self.status_label.setStyleSheet("color:#ff6644;font-size:10px;background:transparent;")

    def _style_rec(self, on: bool):
        if on:
            self.rec_btn.setText("■  STOP")
            self.rec_btn.setStyleSheet(
                "QPushButton{color:white;background:#555;border:none;"
                "border-radius:18px;font-size:13px;font-weight:bold;}"
                "QPushButton:hover{background:#666;}")
        else:
            self.rec_btn.setText("● REC")
            self.rec_btn.setStyleSheet(
                "QPushButton{color:white;background:#cc2222;border:none;"
                "border-radius:18px;font-size:13px;font-weight:bold;}"
                "QPushButton:hover{background:#dd3333;}"
                "QPushButton:pressed{background:#aa1111;}")

    # ── 帧循环 ────────────────────────────────────────────────────────────
    def _tick(self):
        ret, frame = self.cap.read()
        cam_bgr = None
        if ret:
            cam_bgr = letterbox(cv2.flip(frame, 1), WIN_W, self._ch)
            rgb = cv2.cvtColor(cam_bgr, cv2.COLOR_BGR2RGB)
            h, w, c = rgb.shape
            self.cam_label.setPixmap(
                QPixmap.fromImage(QImage(rgb.data, w, h, w*c, QImage.Format.Format_RGB888)))

        if self.recording and self._vid_writer and cam_bgr is not None:
            try:
                region  = self.screen_win.get_capture_region()
                shot    = self.sct.grab(region)
                raw     = np.frombuffer(shot.raw, dtype=np.uint8).reshape(
                              (shot.height, shot.width, 4))
                sch     = self.screen_win.content_h
                scr_bgr = cv2.resize(cv2.cvtColor(raw, cv2.COLOR_BGRA2BGR),
                                     (WIN_W, sch), interpolation=cv2.INTER_AREA)
                cch = TOTAL_H - sch
                if cam_bgr.shape[0] != cch:
                    cam_bgr = letterbox(cam_bgr, WIN_W, cch)
                self._vid_writer.write(np.vstack([scr_bgr, cam_bgr]))
            except Exception as ex:
                print(f"[record] {ex}")

    # ── 鼠标 ──────────────────────────────────────────────────────────────
    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton: return
        ly = e.position().toPoint().y()
        gp = e.globalPosition().toPoint()
        if ly >= self.height() - HANDLE:
            self._resizing = True; self._res_y0 = gp.y(); self._res_ch0 = self._ch
        else:
            self._resizing          = False
            self._drag_global_start = gp
            self._drag_win_start    = self.pos()

    def mouseMoveEvent(self, e):
        ly = e.position().toPoint().y()
        self.setCursor(Qt.CursorShape.SizeVerCursor if ly >= self.height()-HANDLE
                       else Qt.CursorShape.OpenHandCursor)
        if e.buttons() != Qt.MouseButton.LeftButton: return
        gp = e.globalPosition().toPoint()
        if self._resizing:
            self.set_content_h(self._res_ch0 + gp.y() - self._res_y0)
        else:
            self.move(self._drag_win_start + (gp - self._drag_global_start))

    def closeEvent(self, e):
        self.timer.stop()
        if self.recording: self._stop()
        if self.cap.isOpened(): self.cap.release()
        e.accept()


# ── Chrome 视频检测 ───────────────────────────────────────────────────────────
def detect_video_bounds():
    js = ("(function(){var vs=document.querySelectorAll('video'),best=null,ba=0;"
          "for(var v of vs){var r=v.getBoundingClientRect(),a=r.width*r.height;"
          "if(a>ba){ba=a;best=r;}}"
          "if(!best)return 'none';"
          "var uiH=window.outerHeight-window.innerHeight;"
          "return[Math.round(window.screenX+best.left),Math.round(window.screenY+uiH+best.top),"
          "Math.round(best.width),Math.round(best.height)].join(',');})()")
    script = ('tell application "Google Chrome"\n'
              f'  set r to execute front window\'s active tab javascript "{js}"\n'
              '  return r\nend tell')
    try:
        out = subprocess.run(["osascript", "-e", script],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        if out and out != "none" and "," in out:
            return tuple(int(x) for x in out.split(","))
    except Exception:
        pass
    return None


# ── 入口 ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = QApplication(sys.argv)

    _, mic_idx = auto_detect_devices()
    mic_name   = sd.query_devices(mic_idx)['name'] if mic_idx is not None else None

    sys_ok = os.path.exists(SWIFT_BIN)
    print(f"系统音频: {'ScreenCaptureKit ✓' if sys_ok else '工具缺失 (audio_capture)'}")
    print(f"麦克风:   [{mic_idx}] {mic_name or '未检测到'}")

    screen_win = ScreenWindow()
    camera_win = CameraWindow(screen_win, mic_idx, mic_name)

    # 高度联动
    def _on_scr_ch(ch): camera_win.set_content_h(TOTAL_H - ch, emit=False)
    def _on_cam_ch(ch): screen_win.set_content_h(TOTAL_H - ch, emit=False)
    screen_win.on_ch_changed = _on_scr_ch
    camera_win.on_ch_changed = _on_cam_ch

    # 定位
    bounds = detect_video_bounds()
    sc     = app.primaryScreen().geometry()
    if bounds:
        vx, vy, vw, vh = bounds
        print(f"视频检测成功: {vx},{vy}  {vw}×{vh}")
        screen_win.position_on_video(vx, vy, vw, vh)
        sp = screen_win.pos()
        camera_win.move(sp.x(), sp.y() + screen_win.height() + 6)
    else:
        cx = sc.width()//2 - WIN_W//2
        cy = max(0, sc.height()//2 - (screen_win.height() + 6 + camera_win.height())//2)
        screen_win.move(cx, cy)
        camera_win.move(cx, cy + screen_win.height() + 6)

    screen_win.show()
    camera_win.show()
    sys.exit(app.exec())
