"""
Shadow Recorder
  录屏区 — 透明悬浮框 + 系统音频轨（ScreenCaptureKit，无需虚拟声卡）
  摄像区 — 摄像头画面 + 麦克风音轨

高度联动：录屏区内容高 + 摄像区内容高 = 800px（恒定）
输出：shadow_时间戳.mp4（450×800，含两条独立音轨）
首次运行需在「系统设置 → 隐私 → 屏幕录制」中授权。
"""
import sys, cv2, numpy as np, subprocess, threading, wave, os, time, atexit, signal
from datetime import datetime
from enum import Enum

SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
SWIFT_BIN     = os.path.join(SCRIPT_DIR, "audio_capture")
SUBTITLE_BIN  = os.path.join(SCRIPT_DIR, "subtitle_recognizer")
OUTPUT_DIR    = os.path.join(SCRIPT_DIR, "screentest")
os.makedirs(OUTPUT_DIR, exist_ok=True)

import sounddevice as sd

from PyQt6.QtWidgets import (QApplication, QWidget, QLabel, QPushButton,
                              QInputDialog, QScrollArea, QPlainTextEdit, QTextEdit,
                              QVBoxLayout, QHBoxLayout, QFileDialog)
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
SUB_INIT_H = 220
SUB_MIN_H  = 80
SUB_MAX_H  = 1400   # 放宽上限，允许字幕区拉到接近屏幕高度
SUB_TOPBAR = 22

# 圆形摄像气泡尺寸（悬浮窗，不参与窗口高度联动）
CAM_BUBBLE_W   = 220
CAM_BUBBLE_H   = 220   # 圆形区域
CAM_BTN_AREA_H = 60    # 按钮行高
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


# ── Cover（填满 + 居中裁剪，无黑边） ─────────────────────────────────────────
def cover(frame_bgr: np.ndarray, tw: int, th: int) -> np.ndarray:
    fh, fw = frame_bgr.shape[:2]
    scale  = max(tw / fw, th / fh)
    nw, nh = int(fw * scale + 0.5), int(fh * scale + 0.5)
    r = cv2.resize(frame_bgr, (nw, nh), interpolation=cv2.INTER_AREA)
    x0, y0 = (nw - tw) // 2, (nh - th) // 2
    return r[y0:y0+th, x0:x0+tw]


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
    """返回所有可用输入设备列表 [(idx, name), ...]。
    每次调用前重新初始化 PortAudio，确保能看到 Shadow 启动后才连接的设备
    （如 AirPods）。"""
    try:
        sd._terminate()
        sd._initialize()
    except Exception:
        pass
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
def merge_and_save(tmp_video, sys_wav, mic_wav, output, signals: MergeSignals,
                   video_scale: float = 1.0):
    def _run():
        # 视频流时间戳缩放（修正 cv2 写入器以固定 30fps 标签时长 ≠ 实际录制时长的问题）
        inputs = [(tmp_video, ['-itsscale', f'{video_scale:.6f}'])]
        audio_info = []   # [(input_idx, title)]
        for wav, title in [(sys_wav, 'System Audio'), (mic_wav, 'Microphone')]:
            if wav and os.path.exists(wav):
                inputs.append((wav, []))
                audio_info.append((len(inputs) - 1, title))

        maps  = ['-map', '0:v']
        meta  = []
        disp  = []
        ai    = 0
        fcomp = None

        # 两路音频齐全时，合成一条默认混音轨（系统音 + 麦克风），原始两轨保留供剪辑
        if len(audio_info) == 2:
            a0, a1 = audio_info[0][0], audio_info[1][0]
            fcomp  = f"[{a0}:a][{a1}:a]amix=inputs=2:duration=longest[mixed]"
            maps += ['-map', '[mixed]']
            meta += [f'-metadata:s:a:{ai}', 'title=Mixed (System + Mic)']
            disp += [f'-disposition:a:{ai}', 'default']
            ai   += 1

        for in_idx, title in audio_info:
            maps += ['-map', f'{in_idx}:a']
            meta += [f'-metadata:s:a:{ai}', f'title={title}']
            if fcomp:
                disp += [f'-disposition:a:{ai}', '0']
            ai   += 1

        cmd = ['ffmpeg', '-y']
        for path, in_opts in inputs:
            cmd += in_opts + ['-i', path]
        if fcomp:
            cmd += ['-filter_complex', fcomp]
        cmd += maps + meta + disp + ['-c:v', 'libx264', '-preset', 'fast', '-crf', '22',
                                      '-pix_fmt', 'yuv420p', '-c:a', 'aac', output]

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
            # 给 server→local fallback 留时间（服务端探测最长约 6s + 重启缓冲）
            if not ready.wait(timeout=18):
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
            line = line.strip()
            if not line: continue
            if line.startswith("F:"):
                self._on_text(line[2:], True)
            elif line.startswith("P:"):
                self._on_text(line[2:], False)
            else:
                # 兼容旧版无前缀输出 → 当作 partial
                self._on_text(line, False)

    def stop(self):
        self.active = False
        if self._proc:
            # 优雅退出 1.5s 即放弃，强杀避免 ScreenCaptureKit 资源被僵尸占住
            try:
                self._proc.terminate()
                try: self._proc.wait(timeout=1.5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    try: self._proc.wait(timeout=1.0)
                    except subprocess.TimeoutExpired: pass
            except Exception: pass
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
        self.on_subtitle_srt = None   # callback(text) 供 CameraWindow 记录 SRT
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

    def _on_subtitle_text(self, text: str, is_final: bool):
        QTimer.singleShot(0, lambda: self._subtitle_card.show_text(text))
        if self.on_subtitle_srt:
            self.on_subtitle_srt(text, is_final)

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
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # 由于按钮已转移到 ControlCard，气泡只有圆形预览，去掉按钮行
        self.setFixedSize(CAM_BUBBLE_W, CAM_BUBBLE_H)
        # _ch 仅用于录制端合成（保留 WIN_W × _ch 的摄像区，不再驱动窗口尺寸）
        self._ch = INIT_CH
        self._latest_pix = None  # 圆形预览最新帧
        self.setMouseTracking(True)

        # REC / 预扫描 按钮 — 保留为占位但隐藏（操作走 ControlCard）
        self.rec_btn = QPushButton("● REC", self)
        self.rec_btn.setFixedSize(86, 32)
        self.rec_btn.clicked.connect(self._toggle_recording)
        self._style_rec(False)
        self.rec_btn.hide()

        self._prescan_btn = QPushButton("预扫描", self)
        self._prescan_btn.setFixedSize(76, 32)
        self._prescan_btn.clicked.connect(self._toggle_prescan)
        self._style_prescan(False)
        self._prescan_btn.hide()

        # 关闭按钮
        self.close_btn = QPushButton("✕", self)
        self.close_btn.setFixedSize(22, 22)
        self.close_btn.setStyleSheet(
            "QPushButton{color:#888;background:rgba(0,0,0,140);border:none;"
            "border-radius:11px;font-size:11px;}"
            "QPushButton:hover{color:#fff;background:rgba(180,0,0,200);}")
        self.close_btn.clicked.connect(QApplication.quit)

        # 状态标签
        self.status_label = QLabel("", self)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.status_label.setStyleSheet("color:#888;font-size:9px;background:transparent;")

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

        # 预扫描状态
        self.prescanning     = False
        self._prescan_data   = []      # [(start_sec, end_sec, text)]
        self._prescan_t0     = None
        self._prescan_prev   = ""
        self._prescan_prev_t = 0.0
        self.on_prescan_saved = None   # callback(path) — 完成后通知字幕窗口

        # 连接字幕 → SRT 记录
        self._prev_sub    = ""
        self._prev_sub_t  = 0.0
        screen_win.on_subtitle_srt = self._record_srt_entry

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
        # 圆形气泡窗口尺寸固定；_ch 仅用于录制端合成
        ch = max(MIN_CH, min(MAX_CH, ch))
        if ch == self._ch: return
        self._ch = ch
        if emit and self.on_ch_changed:
            self.on_ch_changed(ch)

    # ── 布局 ──────────────────────────────────────────────────────────────
    def _layout(self):
        gap   = 6
        total = self._prescan_btn.width() + gap + self.rec_btn.width()
        x0    = (CAM_BUBBLE_W - total) // 2
        btn_y = CAM_BUBBLE_H + (CAM_BTN_AREA_H - self.rec_btn.height()) // 2
        self._prescan_btn.move(x0, btn_y)
        self.rec_btn.move(x0 + self._prescan_btn.width() + gap, btn_y)
        self.close_btn.move(CAM_BUBBLE_W - 26, 4)
        self.status_label.setGeometry(0, CAM_BUBBLE_H - 16, CAM_BUBBLE_W, 14)

    def resizeEvent(self, e):
        self._layout(); super().resizeEvent(e)

    # ── 绘制 ──────────────────────────────────────────────────────────────
    def paintEvent(self, _):
        from PyQt6.QtCore import QRectF
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 圆形摄像区
        circle = QRectF(0, 0, CAM_BUBBLE_W, CAM_BUBBLE_H)
        from PyQt6.QtGui import QPainterPath
        path = QPainterPath()
        path.addEllipse(circle.adjusted(1, 1, -1, -1))

        p.save()
        p.setClipPath(path)
        p.fillRect(circle, QColor(8, 8, 8))
        if self._latest_pix is not None:
            p.drawPixmap(0, 0, CAM_BUBBLE_W, CAM_BUBBLE_H, self._latest_pix)
        p.restore()

        # 圆形边框：录制时红色亮，闲时柔灰
        clr = QColor("#ff3333") if self.recording else QColor(255, 255, 255, 40)
        p.setPen(QPen(clr, 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(circle.adjusted(1, 1, -1, -1))

        p.end()

    def mouseDoubleClickEvent(self, e):
        """双击圆形区域切换麦克风设备。"""
        if e.position().toPoint().y() <= CAM_BUBBLE_H:
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
        self._final_out = os.path.join(OUTPUT_DIR, f"shadow_{ts}.mp4")
        self._srt_out   = os.path.join(OUTPUT_DIR, f"shadow_{ts}.srt")
        self._srt_data  = []   # [(start_sec, end_sec, text)]
        self._rec_t0    = None # 录制起始时间
        self._frames_written = 0

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

        self._rec_t0 = time.time()
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

        # 写 SRT 字幕文件
        self._write_srt()

        # 计算视频时间戳缩放比：cv2 写入器以 30fps 标签时长，但实际录制帧率往往更低
        video_scale = 1.0
        if self._frames_written > 0 and self._rec_t0:
            elapsed     = time.time() - self._rec_t0
            nominal_dur = self._frames_written / 30.0
            if nominal_dur > 0:
                video_scale = elapsed / nominal_dur
                print(f"[视频] 写入 {self._frames_written} 帧 / {elapsed:.2f}s "
                      f"= {self._frames_written/elapsed:.1f} fps "
                      f"(itsscale={video_scale:.3f})")

        # 后台合并
        merge_and_save(self._tmp_video, sys_wav, mic_wav,
                       self._final_out, self._merge_sig, video_scale)

    def _record_srt_entry(self, text: str, is_final: bool):
        """由 ScreenWindow.on_subtitle_srt 回调；录制或预扫描期间均记录。
        Partial 仅用于记下当前句的起始时间；Final 时正式落条，避免重复堆叠。"""
        now = time.time()
        if self.recording and self._rec_t0 is not None:
            rel = now - self._rec_t0
            if self._prev_sub == "":
                self._prev_sub_t = rel
            self._prev_sub = text
            if is_final:
                self._srt_data.append((self._prev_sub_t, rel, text))
                self._prev_sub   = ""
                self._prev_sub_t = 0.0
        if self.prescanning and self._prescan_t0 is not None:
            rel = now - self._prescan_t0
            if self._prescan_prev == "":
                self._prescan_prev_t = rel
            self._prescan_prev = text
            if is_final:
                self._prescan_data.append((self._prescan_prev_t, rel, text))
                self._prescan_prev   = ""
                self._prescan_prev_t = 0.0

    # ── 预扫描 ────────────────────────────────────────────────────────────
    def _toggle_prescan(self):
        if self.recording:
            return  # 录制中禁止启动预扫描
        if self.prescanning:
            self._stop_prescan()
        else:
            self._start_prescan()

    def _start_prescan(self):
        # 自动开启字幕识别（如果尚未开启）
        sw = self.screen_win
        if not sw._sub_btn.isChecked():
            sw._sub_btn.setChecked(True)
            sw._toggle_subtitle(True)

        self._prescan_data   = []
        self._prescan_prev   = ""
        self._prescan_prev_t = 0.0
        self._prescan_t0     = time.time()
        self.prescanning     = True
        self._prescan_btn.setText("■ 结束扫描")
        self._style_prescan(True)
        self.rec_btn.setEnabled(False)
        self.status_label.setText("正在预扫描字幕…")
        self.status_label.setStyleSheet("color:#66aaff;font-size:10px;background:transparent;")

    def _stop_prescan(self):
        # 收尾阶段 1：保留 prescanning=True 继续收集 ASR 在管线中的尾段，
        # 2 秒后才真正写盘。覆盖音频/识别管线的 1~2s 端到端延迟。
        self._prescan_btn.setEnabled(False)
        self._prescan_btn.setText("收尾中…")
        self.status_label.setText("正在收尾，等待 ASR 末段…")
        self.status_label.setStyleSheet("color:#aaa;font-size:10px;background:transparent;")
        QTimer.singleShot(2000, self._finalize_prescan)

    def _finalize_prescan(self):
        self.prescanning = False
        # 收尾最后一条
        if self._prescan_prev and self._prescan_t0:
            dur = time.time() - self._prescan_t0
            self._prescan_data.append((self._prescan_prev_t, dur, self._prescan_prev))
        self._prescan_prev = ""

        def fmt(sec):
            h, r = divmod(max(0, sec), 3600)
            m, s = divmod(r, 60)
            return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int((s%1)*1000):03d}"

        ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = os.path.join(OUTPUT_DIR, f"prescan_{ts}.srt")
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            for i, (s, e, text) in enumerate(self._prescan_data, 1):
                e = max(e, s + 0.5)
                f.write(f"{i}\n{fmt(s)} --> {fmt(e)}\n{text}\n\n")

        count = len(self._prescan_data)
        print(f"[预扫描] {out}  ({count} 条)")

        self._prescan_btn.setText("预扫描")
        self._prescan_btn.setEnabled(True)
        self._style_prescan(False)
        self.rec_btn.setEnabled(True)
        self.status_label.setText(f"✓ 预扫描 {count} 条 → prescan_{ts}.srt")
        self.status_label.setStyleSheet("color:#44cc88;font-size:10px;background:transparent;")

        if self.on_prescan_saved:
            self.on_prescan_saved(out)

    def _style_prescan(self, on: bool):
        if on:
            self._prescan_btn.setStyleSheet(
                "QPushButton{color:#fff;background:#555;border:none;border-radius:18px;font-weight:bold;}"
                "QPushButton:hover{background:#666;}"
                "QPushButton:pressed{background:#444;}")
        else:
            self._prescan_btn.setStyleSheet(
                "QPushButton{color:#fff;background:#2266dd;border:none;border-radius:18px;font-weight:bold;}"
                "QPushButton:hover{background:#3377ee;}"
                "QPushButton:pressed{background:#1155bb;}")

    def _write_srt(self):
        # 收尾最后一条
        if self._prev_sub and self._rec_t0:
            dur = time.time() - self._rec_t0
            self._srt_data.append((self._prev_sub_t, dur, self._prev_sub))
        self._prev_sub = ""

        def fmt(sec):
            h, r = divmod(max(0, sec), 3600)
            m, s = divmod(r, 60)
            return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int((s%1)*1000):03d}"

        with open(self._srt_out, "w", encoding="utf-8") as f:
            for i, (s, e, text) in enumerate(self._srt_data, 1):
                e = max(e, s + 0.5)
                f.write(f"{i}\n{fmt(s)} --> {fmt(e)}\n{text}\n\n")
        count = len(self._srt_data)
        self._srt_data = []
        print(f"[SRT] {self._srt_out}  ({count} 条)")

    def _on_merge_done(self, ok: bool, path: str):
        self.rec_btn.setEnabled(True)
        if ok:
            name     = os.path.basename(path)
            srt_name = os.path.basename(self._srt_out)
            self.status_label.setText(f"✓ 已保存至 screentest/")
            self.status_label.setStyleSheet("color:#44cc88;font-size:10px;background:transparent;")
            print(f"[保存] 视频: {path}")
            print(f"[保存] 字幕: {self._srt_out}")
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
        flipped = None
        if ret:
            flipped = cv2.flip(frame, 1)
            # 圆形预览（220×220 cover）
            preview = cover(flipped, CAM_BUBBLE_W, CAM_BUBBLE_H)
            rgb = cv2.cvtColor(preview, cv2.COLOR_BGR2RGB)
            h, w, c = rgb.shape
            self._latest_pix = QPixmap.fromImage(
                QImage(rgb.data, w, h, w*c, QImage.Format.Format_RGB888))
            self.update()

        if self.recording and self._vid_writer and flipped is not None:
            try:
                region  = self.screen_win.get_capture_region()
                shot    = self.sct.grab(region)
                raw     = np.frombuffer(shot.raw, dtype=np.uint8).reshape(
                              (shot.height, shot.width, 4))
                sch     = self.screen_win.content_h
                scr_bgr = cv2.resize(cv2.cvtColor(raw, cv2.COLOR_BGRA2BGR),
                                     (WIN_W, sch), interpolation=cv2.INTER_AREA)
                cch     = TOTAL_H - sch
                # 录制用更宽的摄像帧（与屏幕区同宽，保留原合成格式）
                rec_cam = cover(flipped, WIN_W, cch)
                self._vid_writer.write(np.vstack([scr_bgr, rec_cam]))
                self._frames_written += 1
            except Exception as ex:
                print(f"[record] {ex}")

    # ── 鼠标（圆形气泡：整体可拖动；无 resize handle） ───────────────────
    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton: return
        self._drag_global_start = e.globalPosition().toPoint()
        self._drag_win_start    = self.pos()

    def mouseMoveEvent(self, e):
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        if e.buttons() != Qt.MouseButton.LeftButton: return
        gp = e.globalPosition().toPoint()
        self.move(self._drag_win_start + (gp - self._drag_global_start))

    def closeEvent(self, e):
        self.timer.stop()
        if self.recording: self._stop()
        if self.cap.isOpened(): self.cap.release()
        e.accept()


# ── 字幕条目（单条 SRT entry，时间戳 + 可编辑文本） ───────────────────────────
def _fmt_srt_time(sec: float) -> str:
    h, r = divmod(max(0, sec), 3600)
    m, s = divmod(r, 60)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int((s%1)*1000):03d}"


class SubtitleEntryRow(QWidget):
    """单条 SRT entry — 顶部时间戳（只读小字），下面可编辑文本框。"""
    text_changed = pyqtSignal()

    def __init__(self, start: float, end: float, text: str, parent=None):
        super().__init__(parent)
        self.start = start
        self.end   = end

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 6)
        lay.setSpacing(2)

        self.ts_label = QLabel(f"{_fmt_srt_time(start)}  →  {_fmt_srt_time(end)}")
        self.ts_label.setStyleSheet("color:#888;font-size:9px;background:transparent;")
        lay.addWidget(self.ts_label)

        # 用 QTextEdit（非 QPlainTextEdit）— QPlainTextEdit 的 document.size() 不返回真实像素高度
        self.edit = QTextEdit()
        self.edit.setPlainText(text)
        self.edit.setStyleSheet(
            "QTextEdit{color:#eee;background:#1c1c1c;"
            "border:1px solid #2c2c2c;border-radius:4px;font-size:13px;padding:4px;}"
            "QTextEdit:focus{border:1px solid #4488dd;}")
        self.edit.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.edit.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.edit.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.edit.setFixedWidth(WIN_W - 28)
        self.edit.document().setDocumentMargin(4)
        # 文档高度变化时重设固定高度
        self.edit.document().documentLayout().documentSizeChanged.connect(self._on_doc_size_changed)
        self.edit.textChanged.connect(self.text_changed.emit)
        lay.addWidget(self.edit)
        QTimer.singleShot(0, self._refit)

    def _on_doc_size_changed(self, size):
        h = int(size.height() + 14)
        self.edit.setFixedHeight(max(28, h))

    def _refit(self):
        w = self.edit.viewport().width()
        if w > 10:
            self.edit.document().setTextWidth(w)
        h = int(self.edit.document().size().height() + 14)
        self.edit.setFixedHeight(max(28, h))

    def resizeEvent(self, e):
        self.edit.document().setTextWidth(self.edit.viewport().width())
        super().resizeEvent(e)

    def get_text(self) -> str:
        return self.edit.toPlainText()


# ── 字幕窗口 ─────────────────────────────────────────────────────────────────
class SubtitleWindow(QWidget):
    """独立字幕区：滚动列表，每条 SRT entry 可编辑。
    自动加载 OUTPUT_DIR 下最新的 prescan_*.srt；编辑后 debounce 500ms 写回原文件。"""

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.WindowStaysOnTopHint)
        self.setFixedWidth(WIN_W)
        self._ch = SUB_INIT_H
        self.setFixedHeight(self._ch + SUB_TOPBAR + HANDLE)
        self.setMouseTracking(True)

        self._srt_path = None
        self._rows     = []
        self.on_ch_changed = None
        # 默认折叠成一条 pill
        self._collapsed   = True
        self._expanded_h  = SUB_INIT_H

        # 顶部条 + 滚动区
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setStyleSheet(
            "QScrollArea{background:#0d0d0d;}"
            "QScrollBar:vertical{background:#1a1a1a;width:8px;}"
            "QScrollBar::handle:vertical{background:#3a3a3a;border-radius:3px;}")

        self.container = QWidget()
        self.container.setStyleSheet("background:#0d0d0d;")
        self.box = QVBoxLayout(self.container)
        self.box.setContentsMargins(2, 2, 2, 2)
        self.box.setSpacing(2)
        self.scroll.setWidget(self.container)

        self.placeholder = QLabel("暂无字幕\n\n点「预扫描字幕」生成", self.container)
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setStyleSheet(
            "color:#555;font-size:11px;padding:30px;background:transparent;")
        self.box.addWidget(self.placeholder)
        self.box.addStretch(1)

        # debounce 保存
        self._save_t = QTimer(self)
        self._save_t.setSingleShot(True)
        self._save_t.timeout.connect(self._save_now)

        # 拖拽 / 缩放
        self._resizing  = False
        self._res_y0    = 0
        self._res_ch0   = 0
        self._drag_global_start = QPoint()
        self._drag_win_start    = QPoint()

        # 启动默认折叠
        self._apply_collapsed_state()
        self._layout()

    # ── 几何 ──────────────────────────────────────────────────────────────
    @property
    def content_h(self):
        return 0 if self._collapsed else self._ch

    def set_content_h(self, ch: int, emit=True):
        if self._collapsed: return
        ch = max(SUB_MIN_H, min(SUB_MAX_H, ch))
        if ch == self._ch: return
        self._ch = ch
        self.setFixedHeight(ch + SUB_TOPBAR + HANDLE)
        self._layout()
        self.update()
        if emit and self.on_ch_changed:
            self.on_ch_changed(ch)

    def _apply_collapsed_state(self):
        if self._collapsed:
            self.scroll.setVisible(False)
            self.setFixedHeight(SUB_TOPBAR + 4)
        else:
            self.scroll.setVisible(True)
            self.setFixedHeight(self._ch + SUB_TOPBAR + HANDLE)
        self.update()
        if self.on_ch_changed:
            self.on_ch_changed(self.content_h)

    def toggle_collapsed(self):
        if self._collapsed:
            self._collapsed = False
            self._ch = self._expanded_h
        else:
            self._expanded_h = self._ch
            self._collapsed = True
        self._apply_collapsed_state()
        self._layout()

    def _layout(self):
        if self._collapsed:
            return
        self.scroll.setGeometry(BORDER, SUB_TOPBAR,
                                WIN_W - 2*BORDER, self._ch - BORDER)

    def resizeEvent(self, e):
        self._layout(); super().resizeEvent(e)

    # ── 绘制 ──────────────────────────────────────────────────────────────
    def paintEvent(self, _):
        p = QPainter(self)
        h = self.height()
        # pill / 顶部条
        bg = QColor(0, 0, 0, 220) if self._collapsed else QColor(0, 0, 0, 200)
        p.fillRect(0, 0, WIN_W, SUB_TOPBAR, bg)
        if not self._collapsed:
            p.setPen(QPen(QColor("#1e1e1e"), BORDER)); p.setBrush(Qt.BrushStyle.NoBrush)
            b = BORDER // 2
            p.drawRect(b, b, WIN_W - BORDER, h - BORDER)
        p.setPen(QColor("#ddd")); p.setFont(QFont("Helvetica", 11, QFont.Weight.Bold))
        p.drawText(10, 15, "字幕")
        cnt = len(self._rows)
        # 右侧：N 条 · ▾/▴
        arrow = "▴" if not self._collapsed else "▾"
        info  = f"{cnt} 条  {arrow}" if cnt else f"暂无  {arrow}"
        p.setPen(QColor("#888")); p.setFont(QFont("Helvetica", 9))
        fm = p.fontMetrics()
        p.drawText(WIN_W - 10 - fm.horizontalAdvance(info), 15, info)
        # 底部 handle（仅展开态显示）
        if not self._collapsed:
            p.fillRect(0, h-HANDLE, WIN_W, HANDLE, QColor(28, 28, 28, 220))
            cx = WIN_W // 2
            p.setPen(QPen(QColor("#666"), 2))
            p.drawLine(cx-12, h-HANDLE//2, cx+12, h-HANDLE//2)
        p.end()

    # ── 鼠标（顶部右侧 ▾/▴ 切换折叠；顶部其余区域拖动；底部 handle 缩放） ─
    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton: return
        lp = e.position().toPoint()
        ly, lx = lp.y(), lp.x()
        gp = e.globalPosition().toPoint()
        # 顶部最右 80px 视作"折叠开关"区
        if ly <= SUB_TOPBAR and lx >= WIN_W - 80:
            self.toggle_collapsed()
            return
        if not self._collapsed and ly >= self.height() - HANDLE:
            self._resizing = True
            self._res_y0   = gp.y()
            self._res_ch0  = self._ch
        elif ly <= SUB_TOPBAR:
            self._resizing = False
            self._drag_global_start = gp
            self._drag_win_start    = self.pos()

    def mouseMoveEvent(self, e):
        ly = e.position().toPoint().y()
        if ly >= self.height() - HANDLE:
            self.setCursor(Qt.CursorShape.SizeVerCursor)
        elif ly <= SUB_TOPBAR:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)
        if e.buttons() != Qt.MouseButton.LeftButton: return
        gp = e.globalPosition().toPoint()
        if self._resizing:
            self.set_content_h(self._res_ch0 + gp.y() - self._res_y0)
        elif not self._drag_global_start.isNull():
            self.move(self._drag_win_start + (gp - self._drag_global_start))

    # ── 字幕加载 / 保存 ──────────────────────────────────────────────────
    def load_srt(self, path: str, auto_expand: bool = False):
        """读取 SRT 文件，渲染为可编辑行。auto_expand=True 时若当前折叠则自动展开。"""
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as ex:
            print(f"[字幕] 读取失败: {ex}")
            return

        entries = []
        for block in content.strip().split("\n\n"):
            lines = block.strip().split("\n")
            if len(lines) < 3: continue
            ts = lines[1]
            try:
                a, b = ts.split(" --> ")
                def parse(t):
                    h, m, rest = t.split(":")
                    s, ms = rest.split(",")
                    return int(h)*3600 + int(m)*60 + int(s) + int(ms)/1000
                start, end = parse(a), parse(b)
            except Exception:
                continue
            text = "\n".join(lines[2:])
            entries.append((start, end, text))

        self._srt_path = path
        self._render_entries(entries)
        if auto_expand and entries and self._collapsed:
            self.toggle_collapsed()

    def _render_entries(self, entries):
        # 清掉旧 row
        for r in self._rows:
            r.setParent(None)
            r.deleteLater()
        self._rows = []
        self.placeholder.setVisible(len(entries) == 0)

        # 在 placeholder 之前/stretch 之前插入
        insert_idx = self.box.indexOf(self.placeholder) + 1
        for s, e, t in entries:
            row = SubtitleEntryRow(s, e, t, self.container)
            row.text_changed.connect(lambda: self._save_t.start(500))
            self.box.insertWidget(insert_idx, row)
            insert_idx += 1
            self._rows.append(row)
        self.update()
        # 等所有 row 完成 _fit_height 后，再适配窗口高度
        QTimer.singleShot(50, self._fit_window_to_content)

    def _fit_window_to_content(self):
        """加载新字幕后扩大窗口以显示全部 entry — **只增不减**，保护用户已编辑的状态。"""
        if not self._rows: return
        total = sum(r.sizeHint().height() + 2 for r in self._rows) + 8
        from PyQt6.QtWidgets import QApplication
        sc_h = QApplication.primaryScreen().geometry().height()
        needed = max(SUB_MIN_H, min(total, sc_h - 200, SUB_MAX_H))
        if self._collapsed:
            # 折叠态下记忆为"展开后的最小高度"（也只增不减）
            self._expanded_h = max(self._expanded_h, needed)
        else:
            # 当前比需要的小才扩张；用户已经调好的高度不动
            if needed > self._ch:
                self._ch = needed
                self.setFixedHeight(needed + SUB_TOPBAR + HANDLE)
                self._layout()
                if self.on_ch_changed:
                    self.on_ch_changed(needed)
                self.update()

    def _save_now(self):
        if not self._srt_path or not self._rows:
            return
        try:
            with open(self._srt_path, "w", encoding="utf-8") as f:
                for i, row in enumerate(self._rows, 1):
                    text = row.get_text().strip()
                    if not text: continue
                    f.write(f"{i}\n{_fmt_srt_time(row.start)} --> "
                            f"{_fmt_srt_time(row.end)}\n{text}\n\n")
            print(f"[字幕] 已保存 {self._srt_path}")
        except Exception as ex:
            print(f"[字幕] 保存失败: {ex}")


# ── 应用阶段 ─────────────────────────────────────────────────────────────────
class AppState(Enum):
    ENTRY     = "entry"      # S0 — 入口卡片
    PRESCAN   = "prescan"    # S1 — 正在预扫描字幕
    PRACTICE  = "practice"   # S2 — 看字幕跟读练习
    RECORDING = "recording"  # S3 — 正在录制
    FINISHED  = "finished"   # S4 — 录制完成


# ── 控制卡片 ─────────────────────────────────────────────────────────────────
class ControlCard(QWidget):
    """悬浮卡片 — 按用户流程阶段切换主操作。"""

    CARD_W = 300
    CARD_H = 130

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(self.CARD_W, self.CARD_H)

        self.state = AppState.ENTRY
        self._last_video         = None
        self._last_prescan_path  = None

        # 回调（由 main 装载）
        self.on_start_prescan = None
        self.on_stop_prescan  = None
        self.on_continue_last = None
        self.on_start_record  = None
        self.on_stop_record   = None
        self.on_record_again  = None
        self.on_open_video    = None

        # 标题
        self.title = QLabel("开始", self)
        self.title.setStyleSheet("color:#aaa;font-size:11px;background:transparent;")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title.setGeometry(0, 14, self.CARD_W, 18)

        # 主按钮
        self.primary_btn = QPushButton("", self)
        self.primary_btn.setFixedSize(240, 42)
        self.primary_btn.move((self.CARD_W - 240)//2, 40)
        self.primary_btn.clicked.connect(self._on_primary)

        # 次要按钮（链接样式）
        self.secondary_btn = QPushButton("", self)
        self.secondary_btn.setFixedSize(160, 20)
        self.secondary_btn.move((self.CARD_W - 160)//2, 92)
        self.secondary_btn.clicked.connect(self._on_secondary)
        self.secondary_btn.setStyleSheet(
            "QPushButton{color:#888;background:transparent;border:none;"
            "font-size:10px;text-decoration:underline;}"
            "QPushButton:hover{color:#fff;}")

        # 首句预览（仅 ENTRY 显示）
        self.preview_label = QLabel("", self)
        self.preview_label.setStyleSheet(
            "color:#666;font-size:9px;background:transparent;font-style:italic;")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setGeometry(12, 112, self.CARD_W - 24, 14)
        self.preview_label.setVisible(False)

        # 关闭
        self.close_btn = QPushButton("✕", self)
        self.close_btn.setFixedSize(22, 22)
        self.close_btn.move(self.CARD_W - 28, 6)
        self.close_btn.setStyleSheet(
            "QPushButton{color:#666;background:transparent;border:none;font-size:12px;}"
            "QPushButton:hover{color:#fff;background:rgba(180,0,0,180);border-radius:11px;}")
        self.close_btn.clicked.connect(QApplication.quit)

        # 拖动
        self._drag_global_start = QPoint()
        self._drag_win_start    = QPoint()

        self.set_state(AppState.ENTRY)

    def paintEvent(self, _):
        from PyQt6.QtCore import QRectF
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor(22, 22, 22, 240))
        p.setPen(QPen(QColor(60, 60, 60), 1))
        p.drawRoundedRect(QRectF(0.5, 0.5, self.width()-1, self.height()-1), 14, 14)
        p.end()

    def _style_primary(self, color: str):
        styles = {
            "blue": ("#2266dd", "#3377ee"),
            "red":  ("#dd2222", "#e63333"),
            "gray": ("#555",    "#666"),
            "green":("#22aa66", "#33bb77"),
        }
        base, hover = styles.get(color, styles["blue"])
        self.primary_btn.setStyleSheet(
            f"QPushButton{{color:#fff;background:{base};border:none;"
            f"border-radius:10px;font-weight:bold;font-size:13px;}}"
            f"QPushButton:hover{{background:{hover};}}"
            f"QPushButton:disabled{{color:#888;background:#333;}}")

    def set_state(self, state: AppState):
        self.state = state
        s = state
        self.primary_btn.setEnabled(True)
        if s == AppState.ENTRY:
            self.title.setVisible(False)
            self.primary_btn.setText("▶ 开始预扫描字幕")
            # 是否有可继续的上次预扫描？
            latest = self._find_latest_prescan()
            if latest:
                path, first_line = latest
                self._last_prescan_path = path
                self.secondary_btn.setText("继续上次预扫描")
                self.secondary_btn.setVisible(True)
                snippet = first_line[:42] + ("…" if len(first_line) > 42 else "")
                self.preview_label.setText(f"“{snippet}”")
                self.preview_label.setVisible(True)
            else:
                self.secondary_btn.setVisible(False)
                self.preview_label.setVisible(False)
            self._style_primary("blue")
        elif s == AppState.PRESCAN:
            self.title.setVisible(True)
            self.preview_label.setVisible(False)
            self.title.setText("正在预扫描字幕…")
            self.primary_btn.setText("■ 结束扫描")
            self.secondary_btn.setVisible(False)
            self._style_primary("gray")
        elif s == AppState.PRACTICE:
            self.title.setVisible(True)
            self.preview_label.setVisible(False)
            self.title.setText("练习中 — 看字幕跟读")
            self.primary_btn.setText("● 开始录制")
            self.secondary_btn.setText("重新预扫描")
            self.secondary_btn.setVisible(True)
            self._style_primary("red")
        elif s == AppState.RECORDING:
            self.title.setVisible(True)
            self.preview_label.setVisible(False)
            self.title.setText("正在录制…")
            self.primary_btn.setText("■ 停止录制")
            self.secondary_btn.setVisible(False)
            self._style_primary("gray")
        elif s == AppState.FINISHED:
            self.title.setVisible(True)
            self.preview_label.setVisible(False)
            self.title.setText("✓ 已保存到 screentest/")
            self.primary_btn.setText("再录一次")
            self.secondary_btn.setText("打开视频")
            self.secondary_btn.setVisible(True)
            self._style_primary("green")
        self.update()

    def _on_primary(self):
        s = self.state
        if s == AppState.ENTRY     and self.on_start_prescan: self.on_start_prescan()
        elif s == AppState.PRESCAN  and self.on_stop_prescan:  self.on_stop_prescan()
        elif s == AppState.PRACTICE and self.on_start_record:  self.on_start_record()
        elif s == AppState.RECORDING and self.on_stop_record:  self.on_stop_record()
        elif s == AppState.FINISHED and self.on_record_again:  self.on_record_again()

    def _on_secondary(self):
        s = self.state
        if s == AppState.ENTRY     and self.on_continue_last: self.on_continue_last(self._last_prescan_path)
        elif s == AppState.PRACTICE and self.on_start_prescan: self.on_start_prescan()
        elif s == AppState.FINISHED and self.on_open_video:    self.on_open_video()

    def _find_latest_prescan(self):
        """返回 (path, first_subtitle_line) 或 None。"""
        import glob
        candidates = sorted(
            glob.glob(os.path.join(OUTPUT_DIR, "prescan_*.srt")),
            key=os.path.getmtime, reverse=True)
        for path in candidates:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                for block in content.strip().split("\n\n"):
                    lines = block.strip().split("\n")
                    if len(lines) >= 3:
                        text = " ".join(lines[2:]).strip()
                        if text:
                            return path, text
            except Exception:
                continue
        return None

    def set_busy(self, text: str):
        """临时锁定（用于 drain 期等等）。"""
        self.primary_btn.setText(text)
        self.primary_btn.setEnabled(False)
        self.update()

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton: return
        self._drag_global_start = e.globalPosition().toPoint()
        self._drag_win_start    = self.pos()

    def mouseMoveEvent(self, e):
        if e.buttons() != Qt.MouseButton.LeftButton: return
        gp = e.globalPosition().toPoint()
        self.move(self._drag_win_start + (gp - self._drag_global_start))


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
def _cleanup_children():
    """退出时强杀所有 audio_capture / subtitle_recognizer 子进程，避免僵尸占住 ScreenCaptureKit。"""
    for name in ("subtitle_recognizer", "audio_capture"):
        try:
            subprocess.run(["pkill", "-9", "-f",
                            f"{SCRIPT_DIR}/{name}"],
                           timeout=2, capture_output=True)
        except Exception: pass

atexit.register(_cleanup_children)
signal.signal(signal.SIGTERM, lambda *_: (_cleanup_children(), sys.exit(0)))
signal.signal(signal.SIGINT,  lambda *_: (_cleanup_children(), sys.exit(0)))


if __name__ == "__main__":
    app = QApplication(sys.argv)

    _, mic_idx = auto_detect_devices()
    mic_name   = sd.query_devices(mic_idx)['name'] if mic_idx is not None else None

    sys_ok = os.path.exists(SWIFT_BIN)
    print(f"系统音频: {'ScreenCaptureKit ✓' if sys_ok else '工具缺失 (audio_capture)'}")
    print(f"麦克风:   [{mic_idx}] {mic_name or '未检测到'}")

    screen_win   = ScreenWindow()
    camera_win   = CameraWindow(screen_win, mic_idx, mic_name)
    subtitle_win = SubtitleWindow()
    card         = ControlCard()

    # 起步：仅显示控制卡片
    screen_win.hide()
    camera_win.hide()
    subtitle_win.hide()

    state = {"current": AppState.ENTRY, "last_video": None}

    def position_subtitle_below_card():
        cp = card.pos()
        subtitle_win.move(cp.x() - (WIN_W - card.width())//2,
                          cp.y() + card.height() + 8)

    def position_camera_beside_card():
        cp = card.pos()
        camera_win.move(cp.x() + card.width() + 8, cp.y())

    def transition(new_state: AppState):
        state["current"] = new_state
        card.set_state(new_state)
        if new_state == AppState.ENTRY:
            screen_win.hide(); camera_win.hide(); subtitle_win.hide()
        elif new_state == AppState.PRESCAN:
            screen_win.hide(); camera_win.hide()
            position_subtitle_below_card()
            subtitle_win.show()
        elif new_state == AppState.PRACTICE:
            screen_win.hide(); camera_win.hide()
            position_subtitle_below_card()
            subtitle_win.show()
            if subtitle_win._collapsed:
                subtitle_win.toggle_collapsed()
        elif new_state == AppState.RECORDING:
            position_subtitle_below_card()
            subtitle_win.show()
            position_camera_beside_card()
            camera_win.show()
            # 屏幕框：自动定位到 Chrome 视频，否则放屏幕中央
            bounds = detect_video_bounds()
            if bounds:
                vx, vy, vw, vh = bounds
                print(f"视频检测成功: {vx},{vy}  {vw}×{vh}")
                screen_win.position_on_video(vx, vy, vw, vh)
            else:
                sc = app.primaryScreen().geometry()
                screen_win.move(sc.width()//2 - WIN_W//2,
                                sc.height()//2 - screen_win.height()//2)
            screen_win.show()
        elif new_state == AppState.FINISHED:
            screen_win.hide(); camera_win.hide()

    # ── 状态转换回调 ─────────────────────────────────────────────────────
    def do_start_prescan():
        # 从 ENTRY/PRACTICE 进入 PRESCAN
        transition(AppState.PRESCAN)
        camera_win._start_prescan()

    def do_stop_prescan():
        # PRESCAN → 等待 drain → PRACTICE
        card.set_busy("收尾中…")
        camera_win._stop_prescan()

    def on_prescan_saved(path: str):
        # _finalize_prescan 完成后调用 → 加载到字幕区 → 进入 PRACTICE
        subtitle_win.load_srt(path, auto_expand=False)
        transition(AppState.PRACTICE)

    def do_continue_last(path: str):
        if path and os.path.exists(path):
            subtitle_win.load_srt(path, auto_expand=False)
            transition(AppState.PRACTICE)

    def do_start_record():
        transition(AppState.RECORDING)
        camera_win._start()

    def do_stop_record():
        card.set_busy("正在保存…")
        camera_win._stop()

    def on_merge_done(ok: bool, path: str):
        if ok:
            state["last_video"] = path
            card._last_video    = path
        transition(AppState.FINISHED)

    def do_record_again():
        transition(AppState.PRACTICE)

    def do_open_video():
        if state["last_video"]:
            subprocess.run(["open", state["last_video"]])

    card.on_start_prescan = do_start_prescan
    card.on_stop_prescan  = do_stop_prescan
    card.on_continue_last = do_continue_last
    card.on_start_record  = do_start_record
    card.on_stop_record   = do_stop_record
    card.on_record_again  = do_record_again
    card.on_open_video    = do_open_video

    camera_win.on_prescan_saved = on_prescan_saved
    camera_win._merge_sig.done.connect(on_merge_done)

    # 控制卡片定位 — 屏幕中央
    sc = app.primaryScreen().geometry()
    card.move(sc.width()//2 - card.width()//2, sc.height()//2 - card.height()//2)
    card.show()

    sys.exit(app.exec())
