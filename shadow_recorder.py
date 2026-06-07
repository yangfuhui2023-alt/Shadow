"""
Shadow Recorder
  录屏区 — 透明悬浮框 + 系统音频轨（ScreenCaptureKit，无需虚拟声卡）
  摄像区 — 摄像头画面 + 麦克风音轨

高度联动：录屏区内容高 + 摄像区内容高 = 800px（恒定）
输出：shadow_时间戳.mp4（450×800，含两条独立音轨）
首次运行需在「系统设置 → 隐私 → 屏幕录制」中授权。
"""
import sys, cv2, numpy as np, subprocess, threading, wave, os, time, atexit, signal, shutil, tempfile, queue
from datetime import datetime

if getattr(sys, 'frozen', False):
    # PyInstaller .app bundle：Swift 二进制在 Contents/Frameworks/Shadow/
    _contents  = os.path.dirname(os.path.dirname(sys.executable))
    _SWIFT_DIR = os.path.join(_contents, 'Frameworks', 'Shadow')
    OUTPUT_DIR = os.path.expanduser('~/Movies/Shadow')   # 安装版录制输出（通用路径）
else:
    _SWIFT_DIR = os.path.dirname(os.path.abspath(__file__))
    OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'screentest')

SWIFT_BIN    = os.path.join(_SWIFT_DIR, "audio_capture")
SUBTITLE_BIN = os.path.join(_SWIFT_DIR, "subtitle_recognizer")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# 临时文件目录：bundle 只读，统一写 /tmp
_TMPDIR = tempfile.gettempdir()

# ffmpeg：打包后 .app 的 PATH 不含 Homebrew，需显式查找
_FFMPEG = shutil.which("ffmpeg", path=os.environ.get("PATH","") + ":/opt/homebrew/bin:/usr/local/bin") or "ffmpeg"

import sounddevice as sd

from PyQt6.QtWidgets import (QApplication, QWidget, QLabel, QPushButton,
                              QInputDialog)
from PyQt6.QtCore    import QTimer, Qt, QPoint, QPointF, QSize, QObject, pyqtSignal, QRect, QRectF
from PyQt6.QtGui     import (QImage, QPixmap, QPainter, QColor, QPen, QFont,
                              QFontMetrics, QPainterPath, QLinearGradient,
                              QRadialGradient, QBrush, QIcon, QPolygonF)


WIN_W   = 450
TOTAL_H = 800
OUT_W   = 720   # 输出视频宽度（高于 UI 窗口，提升摄像画质）
OUT_H   = 1280  # 输出视频高度（9:16 标准竖屏）
INIT_CH = 400
MIN_CH  = 100
MAX_CH  = 700
BORDER  = 3
HANDLE  = 12
TOPBAR  = 24
SR      = 44100

# ── Ghost UI 设计 Token ───────────────────────────────────────────────────────
GHOST_FRAME   = QColor(255, 255, 255, 128)   # 录制区白线框 0.5
GHOST_DIVIDER = QColor(255, 255, 255, 72)    # 区内分界线 0.28
REC_DOT       = QColor(255, 59, 48)          # 录制红点 #FF3B30
STATUS_DIM    = QColor(255, 255, 255, 102)   # 状态文字 0.4
STATUS_BRIGHT = QColor(255, 255, 255, 178)   # 计时文字 0.7
GRIP_CLR      = QColor(255, 255, 255, 140)   # 底部拖拽把手 0.55
CAPSULE_BG    = QColor(255, 255, 255, 18)    # 胶囊磨砂底 0.07
CAPSULE_BD    = QColor(255, 255, 255, 33)    # 胶囊边框 0.13
CAPSULE_DOT   = QColor(255, 255, 255, 150)

# 按钮三态（主：白底位移；次：深磨砂玻璃透明度变化）
PRIMARY_QSS = (
    "QPushButton{color:#111;background:rgba(255,255,255,0.90);border:none;"
    "border-radius:18px;font-size:13px;font-weight:600;}"
    "QPushButton:hover{background:rgba(255,255,255,1.0);}"
    "QPushButton:pressed{background:rgba(232,232,232,1.0);}"
    "QPushButton:disabled{color:#999;background:rgba(255,255,255,0.45);}")
SECONDARY_QSS = (
    "QPushButton{color:rgba(255,255,255,0.92);background:rgba(15,15,15,0.5);"
    "border:1px solid rgba(255,255,255,0.28);border-radius:18px;"
    "font-size:13px;font-weight:600;}"
    "QPushButton:hover{background:rgba(42,42,42,0.62);"
    "border-color:rgba(255,255,255,0.45);}"
    "QPushButton:pressed{background:rgba(8,8,8,0.7);}"
    "QPushButton:disabled{color:rgba(255,255,255,0.4);"
    "border-color:rgba(255,255,255,0.15);}")
# 录屏区顶部磨砂小 chip（字幕 / 语言 / 折叠）
CHIP_QSS = (
    "QPushButton{color:rgba(255,255,255,0.5);background:rgba(15,15,15,0.42);"
    "border:1px solid rgba(255,255,255,0.18);border-radius:8px;font-size:9px;}"
    "QPushButton:hover{color:rgba(255,255,255,0.85);"
    "border-color:rgba(255,255,255,0.4);}"
    "QPushButton:checked{color:rgba(255,255,255,1.0);"
    "background:rgba(255,255,255,0.16);border-color:rgba(255,255,255,0.5);}")

# ── 图标按钮（聚合控制条；纯矢量图标，无文字）──────────────────────────────────
ICON_BTN_R = 12   # 圆角
# 主图标按钮（录制 / 保存）：白底位移
ICON_PRIMARY_QSS = (
    f"QPushButton{{background:rgba(255,255,255,0.92);border:none;border-radius:{ICON_BTN_R}px;}}"
    "QPushButton:hover{background:rgba(255,255,255,1.0);}"
    "QPushButton:pressed{background:rgba(232,232,232,1.0);}"
    "QPushButton:disabled{background:rgba(255,255,255,0.4);}")
# 次图标按钮（停止 / 取消 / 重录 / 收起）：深磨砂玻璃
ICON_SECONDARY_QSS = (
    f"QPushButton{{background:rgba(20,20,20,0.55);border:1px solid rgba(255,255,255,0.22);"
    f"border-radius:{ICON_BTN_R}px;}}"
    "QPushButton:hover{background:rgba(48,48,48,0.66);border-color:rgba(255,255,255,0.42);}"
    "QPushButton:pressed{background:rgba(10,10,10,0.72);}"
    "QPushButton:disabled{background:rgba(20,20,20,0.3);border-color:rgba(255,255,255,0.1);}")
# 关闭按钮：更弱，hover 转红
ICON_CLOSE_QSS = (
    f"QPushButton{{background:rgba(20,20,20,0.4);border:1px solid rgba(255,255,255,0.16);"
    f"border-radius:{ICON_BTN_R}px;}}"
    "QPushButton:hover{background:rgba(190,40,40,0.75);border-color:rgba(255,255,255,0.4);}"
    "QPushButton:pressed{background:rgba(150,20,20,0.85);}")

ICON_DARK  = QColor(20, 20, 20)            # 白底按钮上的图标色
ICON_LIGHT = QColor(255, 255, 255, 235)    # 磨砂按钮上的图标色

def make_icon(kind: str, color: QColor, size: int = 20) -> QIcon:
    """纯 QPainter 自绘矢量图标（不依赖字体），2x 像素密度保证 Retina 清晰。"""
    import math
    dpr = 2
    pm = QPixmap(size * dpr, size * dpr)
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    cx = cy = size / 2
    pen = QPen(color, 2.0)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)

    if kind == "record":                                  # 实心圆
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(color)
        r = size * 0.30
        p.drawEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))
    elif kind == "stop":                                  # 实心圆角方块
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(color)
        s = size * 0.44
        p.drawRoundedRect(QRectF(cx - s / 2, cy - s / 2, s, s), 2.5, 2.5)
    elif kind == "x":                                     # 叉（取消 / 关闭）
        p.setPen(pen); d = size * 0.24
        p.drawLine(QPointF(cx - d, cy - d), QPointF(cx + d, cy + d))
        p.drawLine(QPointF(cx + d, cy - d), QPointF(cx - d, cy + d))
    elif kind == "check":                                 # 对勾（保存）
        p.setPen(pen); path = QPainterPath()
        path.moveTo(cx - size * 0.26, cy + size * 0.02)
        path.lineTo(cx - size * 0.04, cy + size * 0.22)
        path.lineTo(cx + size * 0.28, cy - size * 0.22)
        p.drawPath(path)
    elif kind == "redo":                                  # 环形箭头（重录）
        p.setPen(pen); r = size * 0.27
        rect = QRectF(cx - r, cy - r, r * 2, r * 2)
        p.drawArc(rect, int(115 * 16), int(290 * 16))     # 留口在右上
        a = math.radians(115)                              # 弧线起点（开口端）放箭头
        ex, ey = cx + r * math.cos(a), cy - r * math.sin(a)
        ah = size * 0.16
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(color)
        tri = QPolygonF([QPointF(ex + ah * 0.1, ey - ah),
                         QPointF(ex + ah, ey + ah * 0.5),
                         QPointF(ex - ah * 0.7, ey + ah * 0.5)])
        p.drawPolygon(tri)
    elif kind == "collapse":                              # 下尖角（收起）
        p.setPen(pen); w = size * 0.22
        p.drawLine(QPointF(cx - w, cy - size * 0.07), QPointF(cx, cy + size * 0.13))
        p.drawLine(QPointF(cx, cy + size * 0.13), QPointF(cx + w, cy - size * 0.07))
    p.end()
    return QIcon(pm)


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
    interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_LANCZOS4
    r = cv2.resize(frame_bgr, (nw, nh), interpolation=interp)
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


# ── 系统音频录制器（常驻 audio_capture daemon） ──────────────────────────────
class SystemAudioDaemon:
    """常驻 audio_capture 子进程：通过 stdin 发 START/STOP 指令复用同一进程。

    屏幕录制授权按"进程首次抓屏"触发，复用进程后每个 app 会话最多弹一次，
    而非每次重录都弹（修复反复授权问题）。
    """

    def __init__(self):
        self._proc   = None
        self._evq    = None          # 后台读取 stderr 推送的事件队列
        self._lock   = threading.Lock()
        self.active  = False         # 当前是否在录制（音频）
        self.video_active = False    # 当前是否在抓屏（视频）
        self.error   = None
        self.audio_epoch = None      # 系统音频首帧 host-clock 时刻（秒）
        self.video_epoch = None      # 屏幕画面首帧 host-clock 时刻（秒，同一时钟）

    def _ensure_proc(self) -> bool:
        if self._proc and self._proc.poll() is None:
            return True
        if not os.path.exists(SWIFT_BIN):
            self.error = "audio_capture 工具不存在"
            return False
        try:
            self._proc = subprocess.Popen(
                [SWIFT_BIN, "--daemon"],
                stdin=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, bufsize=1)
            self._evq = queue.Queue()
            threading.Thread(target=self._reader, args=(self._proc, self._evq),
                             daemon=True).start()
            return True
        except Exception as e:
            self.error = str(e)
            return False

    def _reader(self, proc, evq):
        # 把关心的事件行推入队列，其余（stream stopped 等）丢弃，顺便防 stderr 阻塞
        for line in proc.stderr:
            line = line.strip()
            # 首帧时刻不进事件队列（_wait_for 会把非匹配行丢弃），直接存到 self
            if line.startswith("AEPOCH "):
                try: self.audio_epoch = float(line.split()[1])
                except Exception: pass
            elif line.startswith("VEPOCH "):
                try: self.video_epoch = float(line.split()[1])
                except Exception: pass
            elif line in ("READY", "SAVED", "VREADY", "VSAVED") or line.startswith(("ERROR", "TIP")):
                evq.put(line)

    def _drain_events(self):
        try:
            while True:
                self._evq.get_nowait()
        except queue.Empty:
            pass

    def _wait_for(self, token: str, timeout: float):
        deadline = time.time() + timeout
        while True:
            remain = deadline - time.time()
            if remain <= 0:
                return None
            try:
                ev = self._evq.get(timeout=remain)
            except queue.Empty:
                return None
            if ev == token:
                return ev
            if ev.startswith(("ERROR", "TIP")):
                self.error = ev
                return ev

    def start(self, out_path: str):
        with self._lock:
            self.error  = None
            self.active = False
            self.audio_epoch = None    # 复用进程：清掉上一轮的首帧时刻
            if not self._ensure_proc():
                return
            self._drain_events()       # 清掉上一轮残留的 SAVED 等事件
            try:
                self._proc.stdin.write(f"START {out_path}\n")
                self._proc.stdin.flush()
            except Exception as e:
                self.error = str(e)
                return
            # 授权已存在时 READY 很快；首次授权窗口下若超时则本次无系统音频（与旧行为一致）
            ev = self._wait_for("READY", timeout=6)
            if ev == "READY":
                self.active = True
            elif ev is None:
                self.error = "READY 超时"

    def stop(self):
        with self._lock:
            self.active = False
            if not self._proc or self._proc.poll() is not None:
                return
            try:
                self._proc.stdin.write("STOP\n")
                self._proc.stdin.flush()
            except Exception:
                return
            self._wait_for("SAVED", timeout=15)   # 等写盘完成再返回

    # ── 屏幕画面（ScreenCaptureKit 视频，复用同一常驻进程）────────────────────
    def start_video(self, path, x, y, w, h, out_w, out_h) -> bool:
        """开始抓屏到 path（H.264）。排除本 app 窗口 ⇒ 画面不含 UI/系统红框。
        返回是否成功就绪。"""
        with self._lock:
            if not self._ensure_proc():
                return False
            self.video_active = False
            self.video_epoch = None    # 复用进程：清掉上一轮的首帧时刻
            try:
                self._proc.stdin.write(
                    f"VSTART {int(x)} {int(y)} {int(w)} {int(h)} {int(out_w)} {int(out_h)} {path}\n")
                self._proc.stdin.flush()
            except Exception as e:
                self.error = str(e)
                return False
            ev = self._wait_for("VREADY", timeout=6)
            self.video_active = (ev == "VREADY")
            return self.video_active

    def set_video_region(self, x, y, w, h):
        """录制中移动录屏区 → 实时更新 SCK 捕获矩形（fire-and-forget，不阻塞 UI）。"""
        if not self.video_active or not self._proc or self._proc.poll() is not None:
            return
        try:
            self._proc.stdin.write(f"VREGION {int(x)} {int(y)} {int(w)} {int(h)}\n")
            self._proc.stdin.flush()
        except Exception:
            pass

    def stop_video(self):
        with self._lock:
            self.video_active = False
            if not self._proc or self._proc.poll() is not None:
                return
            try:
                self._proc.stdin.write("VSTOP\n")
                self._proc.stdin.flush()
            except Exception:
                return
            self._wait_for("VSAVED", timeout=15)

    def quit(self):
        with self._lock:
            if self._proc and self._proc.poll() is None:
                try:
                    self._proc.stdin.write("QUIT\n")
                    self._proc.stdin.flush()
                    self._proc.wait(timeout=3)
                except Exception:
                    try: self._proc.kill()
                    except Exception: pass
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
def merge_and_save(scr_video, cam_video, sys_wav, mic_wav, output, signals: MergeSignals,
                   cam_scale: float = 1.0, sys_audio_offset: float = 0.0):
    """录屏区视频（SCK，PTS 精准）置顶 + 摄像区视频（cv2，按 cam_scale 校正时长）置底，
    上下拼接成 9:16，再叠加系统音/麦克风。"""
    def _run():
        inputs = []        # [(path, in_opts)]
        scr_idx = cam_idx = None
        if scr_video and os.path.exists(scr_video):
            inputs.append((scr_video, []))                       # SCK 实时时间戳，无需缩放
            scr_idx = len(inputs) - 1
        if cam_video and os.path.exists(cam_video):
            # cv2 写入器以固定 30fps 打标 ⇒ 标签时长 ≠ 实际，用 itsscale 校正
            inputs.append((cam_video, ['-itsscale', f'{cam_scale:.6f}']))
            cam_idx = len(inputs) - 1

        # 音频输入：不在输入级做偏移。系统音的延后在滤镜图内用 adelay 垫静音实现，
        # 避免 amix 因等待“延后才到的”系统音而把麦克风也一起拖后（混音轨整体延迟）。
        sys_in = mic_in = None
        if sys_wav and os.path.exists(sys_wav):
            inputs.append((sys_wav, [])); sys_in = len(inputs) - 1
        if mic_wav and os.path.exists(mic_wav):
            inputs.append((mic_wav, [])); mic_in = len(inputs) - 1
        delay_ms = int(round(sys_audio_offset * 1000)) if sys_audio_offset and sys_audio_offset > 0 else 0

        filters = []
        # 视频：录屏区在上、摄像区在下，统一 30fps 后 vstack
        if scr_idx is not None and cam_idx is not None:
            filters.append(f"[{scr_idx}:v]fps=30,setpts=PTS-STARTPTS[s]")
            filters.append(f"[{cam_idx}:v]fps=30,setpts=PTS-STARTPTS[c]")
            filters.append("[s][c]vstack=inputs=2[v]")
            vmap = '[v]'
        elif scr_idx is not None:
            vmap = f'{scr_idx}:v'
        elif cam_idx is not None:
            vmap = f'{cam_idx}:v'
        else:
            vmap = None

        maps = (['-map', vmap] if vmap else [])
        meta = []
        disp = []

        if sys_in is not None and mic_in is not None:
            # 系统音延后 delay_ms（adelay 垫前导静音）后一分为二：一路进混音、一路作独立轨。
            # 麦克风不延后（其起始已≈画面）。混音从 0 起 ⇒ 麦克风不被拖后。
            sd_pre = f"[{sys_in}:a]adelay={delay_ms}:all=1," if delay_ms > 0 else f"[{sys_in}:a]"
            filters.append(f"{sd_pre}asplit=2[sa_mix][sa_out]")
            filters.append(f"[{mic_in}:a]asplit=2[ma_mix][ma_out]")
            filters.append("[sa_mix][ma_mix]amix=inputs=2:duration=longest[mixed]")
            maps += ['-map', '[mixed]', '-map', '[sa_out]', '-map', '[ma_out]']
            meta += ['-metadata:s:a:0', 'title=Mixed (System + Mic)',
                     '-metadata:s:a:1', 'title=System Audio',
                     '-metadata:s:a:2', 'title=Microphone']
            disp += ['-disposition:a:0', 'default',
                     '-disposition:a:1', '0', '-disposition:a:2', '0']
        elif sys_in is not None:
            if delay_ms > 0:
                filters.append(f"[{sys_in}:a]adelay={delay_ms}:all=1[sa_out]")
                maps += ['-map', '[sa_out]']
            else:
                maps += ['-map', f'{sys_in}:a']
            meta += ['-metadata:s:a:0', 'title=System Audio']
        elif mic_in is not None:
            maps += ['-map', f'{mic_in}:a']
            meta += ['-metadata:s:a:0', 'title=Microphone']

        cmd = [_FFMPEG, '-y']
        for path, in_opts in inputs:
            cmd += in_opts + ['-i', path]
        if filters:
            cmd += ['-filter_complex', ';'.join(filters)]
        # -shortest: 以最短流为准截断，消除 ScreenCaptureKit stop 后的音频尾段
        cmd += maps + meta + disp + ['-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
                                      '-pix_fmt', 'yuv420p', '-c:a', 'aac',
                                      '-shortest', output]

        print(f"[ffmpeg] 开始编码: {' '.join(cmd)}")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            ok = result.returncode == 0
            if not ok:
                print(f"[ffmpeg] 编码失败 (code={result.returncode}):\n{result.stderr[-3000:]}")
        except subprocess.TimeoutExpired:
            print("[ffmpeg] 编码超时（10分钟），已放弃")
            ok = False
        except Exception as e:
            print(f"[ffmpeg] 启动失败: {e}")
            ok = False

        for f in [scr_video, cam_video, sys_wav, mic_wav]:
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
            text = line.strip()
            if text:
                self._on_text(text)

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


# ── 雾遮罩（录制中非录制区雾化） ───────────────────────────────────────────────
class FogOverlay(QWidget):
    """
    覆盖整个虚拟桌面的高不透明度遮罩：录制中非录制区被深色遮住，仅录制区镂空清晰。
    鼠标穿透（拖动可透传到下方录屏区 ⇒ 录制中可移动），永不被录入（SCK 已排除本 app）。
    录制开始 → fade_in()，停止 → fade_out()；移动录屏区时 set_regions() 让镂空跟随。
    """
    MASK_ALPHA = 150   # 遮罩不透明度（≈60%，桌面明显变暗但文件隐约可辨）

    def __init__(self):
        super().__init__()
        # 不用 Tool 标志：macOS 上 Tool 窗口在 app 失活时会自动隐藏，
        # 会导致"点击非录制区遮罩消失"。仅用 StaysOnTop + 输入穿透 + 不抢焦点。
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.WindowStaysOnTopHint |
                            Qt.WindowType.WindowTransparentForInput |
                            Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._rects   = []     # 录制区（本地坐标 QRect）
        self._target  = 0.0
        self._fade_t  = QTimer(self)
        self._fade_t.timeout.connect(self._tick_fade)
        self.setWindowOpacity(0.0)
        self.hide()

    def _cover_desktop(self):
        self.setGeometry(QApplication.primaryScreen().virtualGeometry())

    def set_regions(self, rects):
        """rects: 全局屏幕坐标 QRect 列表。"""
        g = self.geometry()
        self._rects = [QRect(r.x() - g.x(), r.y() - g.y(), r.width(), r.height())
                       for r in rects]
        if self.isVisible():
            self.update()

    def fade_in(self):
        self._cover_desktop()
        self.setWindowOpacity(self.windowOpacity())
        self.show(); self.raise_()
        self.update()                  # 仅在显示/区域变化时重绘
        self._target = 1.0
        self._fade_t.start(16)

    def fade_out(self):
        self._target = 0.0
        self._fade_t.start(16)

    def _tick_fade(self):
        # 用 windowOpacity 做淡入淡出 → 合成器处理，不触发整屏重绘
        o = self.windowOpacity()
        o = min(self._target, o + 0.09) if o < self._target else max(self._target, o - 0.09)
        self.setWindowOpacity(o)
        if o == self._target:
            self._fade_t.stop()
            if self._target == 0.0:
                self.hide()

    def paintEvent(self, _):
        if not self._rects:
            return
        p = QPainter(self)
        p.setPen(Qt.PenStyle.NoPen)
        # clip = 全屏 − 所有录制区，再整体填深色 ⇒ 非录制区高不透明遮罩、录制区镂空清晰
        full  = QPainterPath(); full.addRect(QRectF(self.rect()))
        holes = QPainterPath()
        for r in self._rects:
            holes.addRect(QRectF(r))
        p.setClipPath(full.subtracted(holes))
        p.fillRect(self.rect(), QColor(0, 0, 0, self.MASK_ALPHA))
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
        self._lock_resize = False    # 录制中：锁定缩放但允许移动（输出尺寸固定，位置可动）
        self._rec_t0    = None
        self._count_n   = 0          # 倒计时大数字（0 = 不显示）
        self._blink     = False
        self._blink_t   = QTimer(self)
        self._blink_t.timeout.connect(self._do_blink)

        self._drag_global_start = QPoint()
        self._drag_win_start    = QPoint()
        self._press_gp  = QPoint()
        self._moved     = False
        self._resizing  = False
        self._res_y0    = 0
        self._res_ch0   = 0

        self.on_ch_changed = None
        self._collapsed    = False
        self.on_collapse_changed = None   # callback(collapsed: bool)

        self.fog_overlay = None   # 录制中雾遮罩（main 注入）

        # 折叠按钮（顶部 strip 最左，磨砂小 chip）
        self._fold_btn = QPushButton("—", self)
        self._fold_btn.setFixedSize(18, 16)
        self._fold_btn.move(6, 4)
        self._fold_btn.setStyleSheet(CHIP_QSS)
        self._fold_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._fold_btn.clicked.connect(self._toggle_collapse)
        self._fold_btn.hide()    # 收起功能已移到摄像区聚合控制条

        # ── 字幕卡片 ──────────────────────────────────────────────────────
        self._subtitle_card = SubtitleCard(self)
        self._subtitle_proc = None
        self._sub_lang_idx  = 0   # 语言索引（对应 SUBTITLE_LANGS）

        # 字幕 chip（顶部 strip 右上）
        self._sub_btn = QPushButton("字幕", self)
        self._sub_btn.setFixedSize(42, 16)
        self._sub_btn.move(WIN_W - 56, 4)
        self._sub_btn.setStyleSheet(CHIP_QSS)
        self._sub_btn.setCheckable(True)
        self._sub_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._sub_btn.clicked.connect(self._toggle_subtitle)

        self._lang_btn = QPushButton("EN", self)
        self._lang_btn.setFixedSize(24, 16)
        self._lang_btn.move(WIN_W - 56 - 28, 4)
        self._lang_btn.setStyleSheet(CHIP_QSS)
        self._lang_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._lang_btn.clicked.connect(self._cycle_lang)

        # 字幕/语言按钮暂隐藏（功能保留，仅不在 UI 显示）
        self._sub_btn.hide()
        self._lang_btn.hide()

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
        if not self._collapsed:
            self.resize(WIN_W, ch + TOPBAR + HANDLE)
        self.update()
        if emit and self.on_ch_changed:
            self.on_ch_changed(ch)

    # ── 折叠 / 展开（胶囊） ────────────────────────────────────────────────
    CAP_W = 56
    CAP_H = 28

    def _toggle_collapse(self):
        # 所有 chip/按钮均已隐藏或外移，折叠/展开只切换窗口尺寸与重绘
        if self._collapsed:
            self._collapsed = False
            self.resize(WIN_W, self._ch + TOPBAR + HANDLE)
        else:
            self._collapsed = True
            self._subtitle_card.clear()
            self.resize(self.CAP_W, self.CAP_H)
        self.update()
        if self.on_collapse_changed:
            self.on_collapse_changed(self._collapsed)

    def set_locked(self, on: bool):
        self._lock_resize = on

    def set_recording(self, on: bool):
        self._recording = on
        self._rec_t0    = time.time() if on else None
        if on: self._blink_t.start(600)
        else:
            self._blink_t.stop(); self._blink = False
        self.update()

    def set_countdown(self, n: int):
        self._count_n = n
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
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # ── 折叠态：磨砂胶囊 + 三个点 ──────────────────────────────────────
        if self._collapsed:
            p.setPen(QPen(CAPSULE_BD, 1)); p.setBrush(CAPSULE_BG)
            p.drawRoundedRect(QRectF(0.5, 0.5, self.CAP_W - 1, self.CAP_H - 1), 14, 14)
            cx, cy = self.CAP_W / 2, self.CAP_H / 2
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(CAPSULE_DOT)
            for off, r in ((-8, 1.6), (0, 2.2), (8, 1.6)):
                p.drawEllipse(QRectF(cx + off - r, cy - r, r * 2, r * 2))
            p.end(); return

        h  = self.height()
        b  = TOPBAR
        # 内容洞（录制区）矩形：strip 之下、handle 之上
        frame = QRectF(0.5, b + 0.5, WIN_W - 1, self._ch - 1)

        # 录制前/倒计时/完成：白线框；录制中：不画（靠雾遮罩界定）
        if not self._recording:
            p.setPen(QPen(GHOST_FRAME, 1)); p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(frame)

        # 倒计时大数字
        if self._count_n > 0:
            p.setPen(QColor(255, 255, 255, 235))
            p.setFont(QFont("Helvetica", 64, QFont.Weight.Light))
            p.drawText(QRectF(0, b, WIN_W, self._ch),
                       Qt.AlignmentFlag.AlignCenter, str(self._count_n))

        # 录制计时已移到摄像区右上角（见 CameraWindow.rec_time_label）

        # 底部中央小横把手
        cx = WIN_W // 2
        p.setPen(QPen(GRIP_CLR, 2))
        p.drawLine(cx - 10, h - 6, cx + 10, h - 6)
        p.end()

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton: return
        ly = e.position().toPoint().y()
        gp = e.globalPosition().toPoint()
        self._press_gp = gp
        self._moved    = False
        in_handle = (not self._collapsed) and ly >= self.height() - HANDLE
        if in_handle and not self._lock_resize:     # 录制中锁缩放 ⇒ 手柄区也按移动处理
            self._resizing = True; self._res_y0 = gp.y(); self._res_ch0 = self._ch
        else:
            self._resizing          = False
            self._drag_global_start = gp
            self._drag_win_start    = self.pos()

    def mouseMoveEvent(self, e):
        ly = e.position().toPoint().y()
        in_handle = (not self._collapsed) and ly >= self.height() - HANDLE and not self._lock_resize
        self.setCursor(Qt.CursorShape.SizeVerCursor if in_handle
                       else Qt.CursorShape.OpenHandCursor)
        if e.buttons() != Qt.MouseButton.LeftButton: return
        gp = e.globalPosition().toPoint()
        if (gp - self._press_gp).manhattanLength() > 3:
            self._moved = True
        if self._resizing:
            self.set_content_h(self._res_ch0 + gp.y() - self._res_y0)
        else:
            self.move(self._drag_win_start + (gp - self._drag_global_start))

    def mouseReleaseEvent(self, e):
        # 折叠态：未拖动的点击 = 展开
        if self._collapsed and not self._moved:
            self._toggle_collapse()


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

        # 摄像画面（内缩 1px 让白线框露出）
        self.cam_label = QLabel(self)
        self.cam_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cam_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        # 倒计时大数字（浮在摄像画面之上）
        self.count_label = QLabel("", self)
        self.count_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.count_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.count_label.setStyleSheet(
            "color:rgba(255,255,255,0.92);background:transparent;"
            "font-size:72px;font-weight:300;")
        self.count_label.hide()

        # 录制计时（右上角，磨砂底；录制中显示）
        self.rec_time_label = QLabel("", self)
        self.rec_time_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.rec_time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.rec_time_label.setStyleSheet(
            "color:rgba(255,255,255,0.95);background:rgba(15,15,15,0.5);"
            "border:1px solid rgba(255,255,255,0.18);border-radius:13px;"
            "font-size:17px;font-weight:600;padding:0 4px;")
        self.rec_time_label.hide()

        # 状态标志（聚合控制条按状态切换图标，须在 _layout 前就绪）
        self.recording  = False
        self._counting  = False
        self._reviewing = False
        self._stopping  = False      # STOP 后等后台收尾的过渡态

        # ── 聚合控制条（全部图标按钮，无文字；居中浮在摄像区底部内侧）─────────
        self.BTN_W, self.BTN_H, self.BTN_GAP = 40, 34, 6
        isz = QSize(20, 20)
        _hand = Qt.CursorShape.PointingHandCursor
        # 预生成矢量图标
        self._ic_record   = make_icon("record",   REC_DOT,    20)   # 红点 = 录制
        self._ic_stop     = make_icon("stop",     ICON_LIGHT, 20)
        self._ic_cancel   = make_icon("x",        ICON_LIGHT, 20)
        self._ic_save     = make_icon("check",    ICON_DARK,  20)
        self._ic_redo     = make_icon("redo",     ICON_LIGHT, 20)
        self._ic_collapse = make_icon("collapse", ICON_LIGHT, 20)
        self._ic_close    = make_icon("x",        ICON_LIGHT, 20)

        # 收起（左）— 触发录屏区折叠成胶囊
        self.fold_btn = QPushButton(self)
        self.fold_btn.setFixedSize(self.BTN_W, self.BTN_H)
        self.fold_btn.setIcon(self._ic_collapse); self.fold_btn.setIconSize(isz)
        self.fold_btn.setStyleSheet(ICON_SECONDARY_QSS)
        self.fold_btn.setCursor(_hand)
        self.fold_btn.clicked.connect(self.screen_win._toggle_collapse)

        # 主操作（录制 / 停止 / 取消 / 保存，按状态变形）
        self.rec_btn = QPushButton(self)
        self.rec_btn.setFixedSize(self.BTN_W, self.BTN_H)
        self.rec_btn.setIconSize(isz)
        self.rec_btn.setCursor(_hand)
        self.rec_btn.clicked.connect(self._btn_clicked)

        # 重录（仅 review 阶段可见）
        self.rerecord_btn = QPushButton(self)
        self.rerecord_btn.setFixedSize(self.BTN_W, self.BTN_H)
        self.rerecord_btn.setIcon(self._ic_redo); self.rerecord_btn.setIconSize(isz)
        self.rerecord_btn.setStyleSheet(ICON_SECONDARY_QSS)
        self.rerecord_btn.setCursor(_hand)
        self.rerecord_btn.clicked.connect(self._do_rerecord)
        self.rerecord_btn.hide()

        # 关闭（右）
        self.close_btn = QPushButton(self)
        self.close_btn.setFixedSize(self.BTN_W, self.BTN_H)
        self.close_btn.setIcon(self._ic_close); self.close_btn.setIconSize(isz)
        self.close_btn.setStyleSheet(ICON_CLOSE_QSS)
        self.close_btn.setCursor(_hand)
        self.close_btn.clicked.connect(QApplication.quit)

        self._dock_rect = QRect()    # 控制条磨砂底（_layout 计算，paintEvent 绘制）
        self._style_rec(False)

        # 状态标签
        self.status_label = QLabel("", self)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.status_label.setStyleSheet("color:#888; font-size:10px; background:transparent;")

        self._layout()

        self.on_ch_changed = None

        # 设备
        self.cap = cv2.VideoCapture(0)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        self._cam_fail_count = 0  # 连续读取失败帧数，用于权限授权后自动重连

        # 录制状态
        self.fog_overlay   = None    # 遮罩（main 注入）
        self._last_fog_regions = None
        self._scr_region_dirty = None  # 待发给 SCK 的新录屏区（限速发送）
        self._last_vregion_t   = 0.0
        self._rec_last_sec     = -1    # 计时标签上次显示的秒数
        self._counting     = False   # 倒计时中
        self._count_n      = 0
        self._count_t      = QTimer(self)
        self._count_t.setSingleShot(True)
        self._count_t.timeout.connect(self._count_tick)
        self.recording     = False
        self._lock_resize  = False   # 录制中：锁定缩放但允许移动
        self._reviewing    = False   # 停止后等待保存/重录决策
        self._vid_writer   = None
        self._tmp_video    = None   # 摄像区临时视频（cv2）
        self._tmp_scr      = None   # 录屏区临时视频（SCK 抓屏）
        self._tmp_sys      = None
        self._tmp_mic      = None
        self._rec_sch_out  = OUT_H  # 冻结的录屏区输出高
        self._rec_cch_out  = 0      # 冻结的摄像区输出高
        self._sys_daemon   = SystemAudioDaemon()  # 常驻系统音频进程（复用，避免反复授权）
        self._rec_mic      = None   # AudioRecorder（麦克风）
        self._held_scr_video = None # _stop_and_hold 后保留的录屏区视频路径
        self._held_sys_wav = None   # _stop_and_hold 后保留的系统音频路径
        self._held_mic_wav = None   # _stop_and_hold 后保留的麦克风路径
        self._video_scale  = 1.0    # 时间戳缩放比
        self._av_offset    = 0.0    # 系统音相对画面的延后量（秒），对齐口型
        self._write_queue  = None
        self._writer_thread = None

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
        self.cam_label.setGeometry(1, 1, WIN_W - 2, ch - 2)
        self.count_label.setGeometry(0, 0, WIN_W, ch)
        self.status_label.setGeometry(0, ch - 72, WIN_W, 18)
        self.rec_time_label.setGeometry(WIN_W - 104, 8, 96, 28)

        # 按状态决定聚合控制条里出现哪些图标（从左到右）
        if self._reviewing:
            btns = [self.rerecord_btn, self.rec_btn]          # ↺ 重录 + ✓ 保存
        elif self.recording or self._counting or self._stopping:
            btns = [self.rec_btn]                             # ■ 停止 / ✕ 取消（收尾时禁用）
        else:
            btns = [self.fold_btn, self.rec_btn, self.close_btn]  # ⌄ 收起 + ● 录制 + ✕ 关闭
        for b in (self.fold_btn, self.rec_btn, self.rerecord_btn, self.close_btn):
            b.setVisible(b in btns)

        gap   = self.BTN_GAP
        total = len(btns) * self.BTN_W + (len(btns) - 1) * gap
        x0    = (WIN_W - total) // 2
        y     = ch - 12 - self.BTN_H
        x     = x0
        for b in btns:
            b.move(x, y); x += self.BTN_W + gap
        pad = 7
        self._dock_rect = QRect(x0 - pad, y - pad, total + pad * 2, self.BTN_H + pad * 2)

    def resizeEvent(self, e):
        self._layout(); super().resizeEvent(e)

    # ── 绘制 ──────────────────────────────────────────────────────────────
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        h = self.height()
        # 暗底（露出的 1px 边 + 手柄区）；摄像画面由 cam_label 覆盖
        p.fillRect(0, 0, WIN_W, h, QColor(12, 12, 12))

        # 录制前/倒计时/完成：白线框（围摄像区）；录制中：不画（靠雾遮罩）
        if not self.recording:
            p.setPen(QPen(GHOST_FRAME, 1)); p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(QRectF(0.5, 0.5, WIN_W - 1, self._ch - 1))

        # 聚合控制条磨砂底（衬在图标按钮后面）
        if not self._dock_rect.isNull():
            r = QRectF(self._dock_rect)
            p.setPen(QPen(QColor(255, 255, 255, 28), 1))
            p.setBrush(QColor(18, 18, 18, 150))
            p.drawRoundedRect(r, 16, 16)

        # 底部中央小横把手
        cx = WIN_W // 2
        p.setPen(QPen(GRIP_CLR, 2))
        p.drawLine(cx - 10, h - 6, cx + 10, h - 6)
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

    def _set_locked(self, on: bool):
        """录制中锁定两个窗口的缩放（输出尺寸固定），但允许移动；移动时遮罩+SCK 跟随。"""
        self._lock_resize = on
        self.screen_win.set_locked(on)

    # ── 录制控制 ──────────────────────────────────────────────────────────
    def _btn_clicked(self):
        if self._counting:
            self._cancel_countdown()
        elif self.recording:
            self._stop_and_hold()
        elif self._reviewing:
            self._do_save()
        else:
            self._start_countdown()

    # ── 倒计时 ──────────────────────────────────────────────────────────────
    def _start_countdown(self):
        self._counting = True
        self._count_n  = 3
        self._show_count(3)
        self.rec_btn.setIcon(self._ic_cancel)        # 倒计时中：✕ 取消
        self.rec_btn.setStyleSheet(ICON_SECONDARY_QSS)
        self.status_label.setText("")
        self._layout()
        self._count_t.start(1000)

    def _count_tick(self):
        self._count_n -= 1
        if self._count_n <= 0:
            self._end_countdown()
            self._start()
        else:
            self._show_count(self._count_n)
            self._count_t.start(1000)

    def _show_count(self, n: int):
        self.count_label.setText(str(n))
        self.count_label.raise_()
        self.count_label.show()
        self.screen_win.set_countdown(n)

    def _end_countdown(self):
        self._counting = False
        self._count_n  = 0
        self.count_label.hide()
        self.screen_win.set_countdown(0)

    def _cancel_countdown(self):
        self._count_t.stop()
        self._end_countdown()
        self._style_rec(False)
        self.status_label.setText("")
        self._layout()

    def _start(self):
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        self._tmp_video = os.path.join(_TMPDIR, f"_tmp_cam_{ts}.avi")   # 摄像区（本地 cv2）
        self._tmp_scr   = os.path.join(_TMPDIR, f"_tmp_scr_{ts}.mp4")   # 录屏区（SCK 抓屏）
        self._tmp_sys   = os.path.join(_TMPDIR, f"_tmp_sys_{ts}.caf")
        self._tmp_mic   = os.path.join(_TMPDIR, f"_tmp_mic_{ts}.wav")
        self._final_out = os.path.join(OUTPUT_DIR, f"shadow_{ts}.mp4")
        self._rec_t0    = None # 录制起始时间
        self._frames_written = 0

        # 冻结捕获几何：录屏区按比例缩放到 OUT_W，摄像区补足到 OUT_H
        region = self.screen_win.get_capture_region()   # 全局点坐标
        self._rec_sch_out = round(region["height"] * OUT_W / WIN_W)
        self._rec_cch_out = OUT_H - self._rec_sch_out
        # 录制期间锁定两窗口，保证捕获矩形与画面一致
        self._set_locked(True)

        # 摄像区写帧器（仅摄像头画面，录屏区交给 SCK）
        fourcc = cv2.VideoWriter_fourcc(*"MJPG")
        self._vid_writer = cv2.VideoWriter(self._tmp_video, fourcc, 30,
                                           (OUT_W, self._rec_cch_out))

        # 异步写帧：采集线程只入队，独立 worker 线程负责磁盘写入，避免 I/O 阻塞帧率
        self._vid_writer.set(cv2.VIDEOWRITER_PROP_QUALITY, 95)
        self._write_queue  = queue.Queue(maxsize=30)
        _vw = self._vid_writer
        _q  = self._write_queue      # capture local ref so crash-safe when self._write_queue is cleared
        def _frame_writer():
            while True:
                item = _q.get()
                if item is None:
                    break
                _vw.write(item)
        self._writer_thread = threading.Thread(target=_frame_writer, daemon=True)
        self._writer_thread.start()

        # 系统音频（ScreenCaptureKit，常驻 daemon 复用进程）
        self._sys_daemon.start(self._tmp_sys)
        if self._sys_daemon.error:
            print(f"[系统音频] {self._sys_daemon.error}")
            self.screen_win.set_sys_audio_ok(False)
        else:
            self.screen_win.set_sys_audio_ok(True)

        # 录屏画面（ScreenCaptureKit，排除本 app 窗口 ⇒ 画面不含 UI/系统录屏红框）
        ok = self._sys_daemon.start_video(
            self._tmp_scr, region["left"], region["top"],
            region["width"], region["height"], OUT_W, self._rec_sch_out)
        if not ok:
            print(f"[录屏] 抓屏启动失败: {self._sys_daemon.error}")

        # 麦克风
        self._rec_mic = AudioRecorder(self.mic_idx)
        self._rec_mic.start()

        self._rec_t0          = time.time()
        self._last_frame_time = None   # 最后一帧写入时刻，用于精准计算 itsscale
        self.recording = True
        self.screen_win.set_recording(True)
        # 遮罩：录制中非录制区高不透明遮罩，仅录制区镂空
        if self.fog_overlay:
            self._last_fog_regions = self._record_regions()
            self._scr_region_dirty = None
            self.fog_overlay.set_regions(self._last_fog_regions)
            self.fog_overlay.fade_in()
            self.raise_()            # 摄像区浮在遮罩之上（录屏区在遮罩之下，镂空透出桌面）
        # 右上角录制计时
        self._rec_last_sec = -1
        self.rec_time_label.setText('<span style="color:#FF3B30;">●</span> 00:00')
        self.rec_time_label.show(); self.rec_time_label.raise_()
        self._style_rec(True)
        self._layout()                 # 控制条收成单个「停止」
        self.status_label.setText("")
        self.update()

    def _record_regions(self):
        """两个录制区的全局屏幕坐标 QRect（屏幕内容洞 + 摄像区）。"""
        sp = self.screen_win.pos()
        scr = QRect(sp.x(), sp.y() + TOPBAR, WIN_W, self.screen_win.content_h)
        cp = self.pos()
        cam = QRect(cp.x(), cp.y(), WIN_W, self._ch)
        return [scr, cam]

    def _stop_and_hold(self):
        """停止录制，保留临时文件，进入 review 状态（用户决策保存或重录）。"""
        self.recording = False
        self._stopping = True          # 收尾过渡态：控制条只留禁用的主键，禁止误点关闭
        self.rec_time_label.hide()
        _stop_t = self._last_frame_time or time.time()
        self.screen_win.set_recording(False)
        self._set_locked(False)        # 解锁两窗口，进入 review 可自由调整下一条取景
        if self.fog_overlay:
            self.fog_overlay.fade_out()
        self._style_rec(False)
        self.rec_btn.setEnabled(False)
        self._layout()
        self.status_label.setText("停止中…")
        self.update()

        # 快照后立即置 None，后台线程接管阻塞操作，UI 线程不等待
        vid_writer     = self._vid_writer;    self._vid_writer = None
        write_queue    = self._write_queue;   self._write_queue = None
        writer_thread  = self._writer_thread; self._writer_thread = None
        sys_daemon     = self._sys_daemon     # 常驻进程，不置 None，停止录制即可
        rec_mic        = self._rec_mic;       self._rec_mic    = None
        tmp_sys        = self._tmp_sys
        tmp_mic        = self._tmp_mic
        tmp_scr        = self._tmp_scr
        frames_written = self._frames_written
        rec_t0         = self._rec_t0

        def _do_stop():
            # 先排空写帧队列，再 release，保证所有帧都落盘
            if write_queue and writer_thread:
                write_queue.put(None)
                writer_thread.join(timeout=10)
            if vid_writer:
                vid_writer.release()

            # 录屏画面（SCK）：无条件停止并等写盘（即使 VREADY 超时也兜底收尾）
            held_scr_video = None
            sys_daemon.stop_video()
            if tmp_scr and os.path.exists(tmp_scr) and os.path.getsize(tmp_scr) > 0:
                held_scr_video = tmp_scr

            held_sys_wav = None
            if sys_daemon.active:
                sys_daemon.stop()
                if os.path.exists(tmp_sys) and os.path.getsize(tmp_sys) > 0:
                    held_sys_wav = tmp_sys

            held_mic_wav = None
            if rec_mic:
                if rec_mic.stop_and_save(tmp_mic):
                    held_mic_wav = tmp_mic

            video_scale = 1.0
            if frames_written > 0 and rec_t0:
                elapsed     = _stop_t - rec_t0
                nominal_dur = frames_written / 30.0
                if nominal_dur > 0:
                    video_scale = elapsed / nominal_dur
                    print(f"[视频] 写入 {frames_written} 帧 / {elapsed:.2f}s "
                          f"= {frames_written/elapsed:.1f} fps "
                          f"(itsscale={video_scale:.3f})")

            # 音画对齐：系统音频与屏幕画面同用 host 时钟，首帧时刻之差即真实采集
            # 起始偏移。系统音 START 先于视频 VSTART + SCK 首帧延迟 ⇒ 系统音超前画面，
            # 合成时把系统音整体延后 av_offset 秒即可对上口型。麦克风在 VSTART 之后才
            # 启动，与画面起始已基本同步，不施加偏移。
            av_offset = 0.0
            ae, ve = sys_daemon.audio_epoch, sys_daemon.video_epoch
            if ae is not None and ve is not None:
                av_offset = max(0.0, ve - ae)
                print(f"[音画] 系统音首帧={ae:.3f}s 画面首帧={ve:.3f}s "
                      f"→ 系统音延后 {av_offset:.3f}s 对齐画面")

            self._held_scr_video = held_scr_video
            self._held_sys_wav = held_sys_wav
            self._held_mic_wav = held_mic_wav
            self._video_scale  = video_scale
            self._av_offset    = av_offset

            if frames_written == 0:
                QTimer.singleShot(0, self._discard_and_reenable)
            else:
                QTimer.singleShot(0, self._enter_review)

        threading.Thread(target=_do_stop, daemon=True).start()

    def _discard_and_reenable(self):
        self._stopping = False
        self._discard_temp()
        self.rec_btn.setEnabled(True)
        self.status_label.setText("")
        self._layout()

    def _enter_review(self):
        """显示"重录 / 保存"操作区，等待用户决策。"""
        self._stopping  = False
        self._reviewing = True
        self.rec_btn.setIcon(self._ic_save)          # ✓ 保存
        self.rec_btn.setStyleSheet(ICON_PRIMARY_QSS)
        # 保存按钮延迟启用：防止按 STOP 时鼠标未松开、切换后 mouseRelease 误触"保存"
        self.rec_btn.setEnabled(False)
        self._layout()
        self.status_label.setText("留还是重来？")
        self.status_label.setStyleSheet("color:#aaa;font-size:10px;background:transparent;")
        self.update()
        QTimer.singleShot(400, self._enable_save_btn)

    def _enable_save_btn(self):
        if self._reviewing:
            self.rec_btn.setEnabled(True)

    def _exit_review(self):
        """退出 review 状态，恢复 REC 按钮。"""
        self._reviewing = False
        self.rerecord_btn.hide()
        self._style_rec(False)
        self._layout()
        self.update()

    def _do_rerecord(self):
        """丢弃本次录制，直接准备重录。"""
        self._discard_temp()
        self._exit_review()
        self.status_label.setText("")
        self.status_label.setStyleSheet("color:#888;font-size:10px;background:transparent;")

    def _discard_temp(self):
        """删除所有临时录制文件，重置状态。"""
        for f in [self._tmp_video, self._tmp_scr,
                  self._held_scr_video, self._held_sys_wav, self._held_mic_wav]:
            if f and os.path.exists(f):
                try: os.remove(f)
                except: pass
        self._tmp_video      = None
        self._tmp_scr        = None
        self._tmp_sys        = None
        self._tmp_mic        = None
        self._held_scr_video = None
        self._held_sys_wav   = None
        self._held_mic_wav   = None
        self._rec_t0         = None
        self._frames_written = 0
        self._final_out      = None

    def _do_save(self):
        self.rec_btn.setEnabled(False)
        self.rerecord_btn.setEnabled(False)
        self._save_t0 = time.time()
        self.status_label.setText("⏳ 正在保存… 0s")
        self.status_label.setStyleSheet("color:#888;font-size:10px;background:transparent;")
        self._save_progress_timer = QTimer(self)
        self._save_progress_timer.timeout.connect(self._update_save_progress)
        self._save_progress_timer.start(1000)
        merge_and_save(self._held_scr_video, self._tmp_video,
                       self._held_sys_wav, self._held_mic_wav,
                       self._final_out, self._merge_sig, self._video_scale,
                       self._av_offset)

    def _update_save_progress(self):
        elapsed = int(time.time() - self._save_t0)
        self.status_label.setText(f"⏳ 正在保存… {elapsed}s")

    def _on_merge_done(self, ok: bool, path: str):
        if hasattr(self, '_save_progress_timer'):
            self._save_progress_timer.stop()
            self._save_progress_timer = None
        self.rerecord_btn.setEnabled(True)
        self.rec_btn.setEnabled(True)   # 保存完成后恢复「开始录制」可点击（_do_save 里曾禁用）
        self._exit_review()
        if ok:
            print(f"[保存] 视频: {path}")
            self.status_label.setText("✓ 已保存至 screentest/")
            self.status_label.setStyleSheet("color:#44cc88;font-size:10px;background:transparent;")
        else:
            self.status_label.setText("⚠ 合并失败，检查 ffmpeg")
            self.status_label.setStyleSheet("color:#ff6644;font-size:10px;background:transparent;")

    def _style_rec(self, on: bool):
        if on:                                   # 录制中：停止（次）
            self.rec_btn.setIcon(self._ic_stop)
            self.rec_btn.setStyleSheet(ICON_SECONDARY_QSS)
        else:                                    # 待机：录制（主）
            self.rec_btn.setIcon(self._ic_record)
            self.rec_btn.setStyleSheet(ICON_PRIMARY_QSS)

    # ── 帧循环 ────────────────────────────────────────────────────────────
    def _tick(self):
        ret, frame = self.cap.read()
        cam_bgr = None
        if ret:
            self._cam_fail_count = 0
            # 按 Retina devicePixelRatio 渲染，避免 2x 屏幕上的马赛克感
            dpr   = self.devicePixelRatio()
            dw, dh = int(WIN_W * dpr), int(self._ch * dpr)
            cam_bgr = cover(cv2.flip(frame, 1), dw, dh)
            rgb = cv2.cvtColor(cam_bgr, cv2.COLOR_BGR2RGB)
            h, w, c = rgb.shape
            img = QImage(rgb.data, w, h, w * c, QImage.Format.Format_RGB888)
            pix = QPixmap.fromImage(img)
            pix.setDevicePixelRatio(dpr)
            self.cam_label.setPixmap(pix)
        else:
            self._cam_fail_count += 1
            # 每 ~2 秒（60 帧）尝试重新打开摄像头，处理授权后不刷新的情况
            if self._cam_fail_count % 60 == 0:
                self.cap.release()
                self.cap = cv2.VideoCapture(0)
                self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
                self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)

        # 摄像区写本地视频；录屏区画面由 SCK 抓屏（合并时与摄像区上下拼接）
        if self.recording and self._vid_writer and cam_bgr is not None:
            try:
                # 原始帧（翻转）覆盖到输出尺寸，比从缩小后的 cam_bgr 保留更多细节
                cam_out = cover(cv2.flip(frame, 1), OUT_W, self._rec_cch_out)
                try:
                    self._write_queue.put_nowait(cam_out)
                except queue.Full:
                    pass  # writer 来不及时丢帧，优先保证采集流畅
                self._frames_written   += 1
                self._last_frame_time   = time.time()
            except Exception as ex:
                print(f"[record] {ex}")

        # 右上角计时（每秒更新）
        if self.recording and self._rec_t0:
            el = int(time.time() - self._rec_t0)
            if el != self._rec_last_sec:
                self._rec_last_sec = el
                self.rec_time_label.setText(
                    f'<span style="color:#FF3B30;">●</span> {el//60:02d}:{el%60:02d}')

        # 录制中移动窗口 → 遮罩镂空立即跟随；SCK 捕获矩形限速跟随（≤10/s，避免抖动）
        if self.recording and self.fog_overlay:
            regs = self._record_regions()
            if regs != self._last_fog_regions:
                self._last_fog_regions = regs
                self.fog_overlay.set_regions(regs)
                self._scr_region_dirty = regs[0]
            if self._scr_region_dirty is not None and \
               (time.time() - self._last_vregion_t) > 0.1:
                r = self._scr_region_dirty
                self._sys_daemon.set_video_region(r.x(), r.y(), r.width(), r.height())
                self._scr_region_dirty = None
                self._last_vregion_t   = time.time()

    # ── 鼠标 ──────────────────────────────────────────────────────────────
    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton: return
        ly = e.position().toPoint().y()
        gp = e.globalPosition().toPoint()
        if ly >= self.height() - HANDLE and not self._lock_resize:   # 录制中手柄区按移动处理
            self._resizing = True; self._res_y0 = gp.y(); self._res_ch0 = self._ch
        else:
            self._resizing          = False
            self._drag_global_start = gp
            self._drag_win_start    = self.pos()

    def mouseMoveEvent(self, e):
        ly = e.position().toPoint().y()
        in_handle = ly >= self.height() - HANDLE and not self._lock_resize
        self.setCursor(Qt.CursorShape.SizeVerCursor if in_handle
                       else Qt.CursorShape.OpenHandCursor)
        if e.buttons() != Qt.MouseButton.LeftButton: return
        gp = e.globalPosition().toPoint()
        if self._resizing:
            self.set_content_h(self._res_ch0 + gp.y() - self._res_y0)
        else:
            self.move(self._drag_win_start + (gp - self._drag_global_start))

    def closeEvent(self, e):
        self.timer.stop()
        if self.recording:
            self._stop_and_hold()
        if self.fog_overlay:
            self.fog_overlay.hide()
        self._discard_temp()
        if self.cap.isOpened():
            self.cap.release()
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


def _cleanup_children():
    """退出时强杀所有 audio_capture / subtitle_recognizer 子进程，避免僵尸占住 ScreenCaptureKit。"""
    for name in ("subtitle_recognizer", "audio_capture"):
        try:
            subprocess.run(["pkill", "-9", "-f", f"{_SWIFT_DIR}/{name}"],
                           timeout=2, capture_output=True)
        except Exception: pass

atexit.register(_cleanup_children)
signal.signal(signal.SIGTERM, lambda *_: (_cleanup_children(), sys.exit(0)))
signal.signal(signal.SIGINT,  lambda *_: (_cleanup_children(), sys.exit(0)))


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

    # 雾遮罩（录制中非录制区雾化）
    fog_overlay = FogOverlay()
    camera_win.fog_overlay = fog_overlay
    screen_win.fog_overlay = fog_overlay

    # 高度联动 — 改完高度后把摄像区移到录屏区正下方，避免独立窗口位置不同步导致重叠/分离
    def _relayout_cam_below_scr():
        sp = screen_win.pos()
        # screen_win._ch 是 set_content_h 里已同步更新的值，
        # 直接用它算偏移，避免 macOS 下 resize() 后 height() 返回旧值导致位置偏差
        camera_win.move(sp.x(), sp.y() + screen_win._ch + TOPBAR + HANDLE + 6)

    def _on_scr_ch(ch):
        camera_win.set_content_h(TOTAL_H - ch, emit=False)
        _relayout_cam_below_scr()

    def _on_cam_ch(ch):
        screen_win.set_content_h(TOTAL_H - ch, emit=False)
        _relayout_cam_below_scr()

    screen_win.on_ch_changed = _on_scr_ch
    camera_win.on_ch_changed = _on_cam_ch

    def _on_collapse(collapsed):
        if collapsed:
            camera_win.hide()
        else:
            camera_win.show()
            _relayout_cam_below_scr()

    screen_win.on_collapse_changed = _on_collapse

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
