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
    # 安装版录制输出：用户指定存到项目内 screentest（本机硬编码绝对路径，换机/分发需改回通用路径）
    OUTPUT_DIR = '/Users/yangxiaohui/Desktop/Claude Shadow/screentest'
else:
    _SWIFT_DIR = os.path.dirname(os.path.abspath(__file__))
    OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'screentest')

SWIFT_BIN    = os.path.join(_SWIFT_DIR, "audio_capture")
SUBTITLE_BIN = os.path.join(_SWIFT_DIR, "subtitle_recognizer")
SUBFILE_BIN  = os.path.join(_SWIFT_DIR, "subtitle_file")   # 文件级字幕识别（旧苹果 SFSpeech，已弃用）
# whisper.cpp 本地识别（替代苹果方案）：dev 用 homebrew 的 whisper-cli，打包则用 bundle 内二进制
WHISPER_BIN   = shutil.which("whisper-cli", path=os.environ.get("PATH","") + ":/opt/homebrew/bin:/usr/local/bin") \
    or os.path.join(_SWIFT_DIR, "whisper-cli")
WHISPER_MODEL = os.path.join(_SWIFT_DIR, "ggml-base.en.bin")
WHISPER_VAD   = os.path.join(_SWIFT_DIR, "ggml-silero-v5.1.2.bin")   # 语音活动检测：贴真实说话定时间戳
os.makedirs(OUTPUT_DIR, exist_ok=True)

# 临时文件目录：bundle 只读，统一写 /tmp
_TMPDIR = tempfile.gettempdir()

# ffmpeg：打包后 .app 的 PATH 不含 Homebrew，需显式查找
_FFMPEG = shutil.which("ffmpeg", path=os.environ.get("PATH","") + ":/opt/homebrew/bin:/usr/local/bin") or "ffmpeg"
_FFPROBE = shutil.which("ffprobe", path=os.environ.get("PATH","") + ":/opt/homebrew/bin:/usr/local/bin") or "ffprobe"

import sounddevice as sd
import wave

from PyQt6.QtWidgets import (QApplication, QWidget, QLabel, QPushButton,
                              QInputDialog, QSystemTrayIcon, QMenu, QSlider, QLineEdit)
from PyQt6.QtCore    import QTimer, Qt, QPoint, QPointF, QSize, QObject, pyqtSignal, QRect, QRectF, QEvent
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
    elif kind == "scissors":                              # 剪刀（裁切）
        p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
        rr = size * 0.11
        lh = QPointF(cx - size * 0.17, cy + size * 0.22)  # 左环（手柄）
        rh = QPointF(cx + size * 0.17, cy + size * 0.22)  # 右环
        p.drawEllipse(lh, rr, rr)
        p.drawEllipse(rh, rr, rr)
        # 两刃从环交叉伸向上方
        p.drawLine(QPointF(lh.x() + rr * 0.5, lh.y() - rr), QPointF(cx + size * 0.22, cy - size * 0.24))
        p.drawLine(QPointF(rh.x() - rr * 0.5, rh.y() - rr), QPointF(cx - size * 0.22, cy - size * 0.24))
    elif kind == "play":                                  # 实心三角（播放）
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(color)
        s = size * 0.30
        p.drawPolygon(QPolygonF([QPointF(cx - s * 0.7, cy - s),
                                 QPointF(cx - s * 0.7, cy + s),
                                 QPointF(cx + s * 0.95, cy)]))
    elif kind == "pause":                                 # 两竖条（暂停）
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(color)
        bw, bh, g = size * 0.15, size * 0.5, size * 0.11
        p.drawRoundedRect(QRectF(cx - g - bw, cy - bh / 2, bw, bh), 1.5, 1.5)
        p.drawRoundedRect(QRectF(cx + g,      cy - bh / 2, bw, bh), 1.5, 1.5)
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
    """从指定设备录音，停止后写为 WAV 文件。

    每次 start() 都刷新 PortAudio 并按「设备名」重新解析索引：蓝牙耳机
    (AirPods 等) 在 app 启动后热插拔，启动时缓存的索引会失效甚至错位，
    只按索引开流会拿不到任何样本 → 麦克风轨静默丢失。
    采样率也不再硬编码 44100：蓝牙 HFP 输入常被锁在 24k/16k，硬开 44100
    可能开得起来却收不到回调；优先用设备原生采样率，写 WAV 时如实记录，
    最终重采样交给 ffmpeg 合成时统一处理。
    """

    def __init__(self, device_idx=None, device_name=None):
        self.device  = device_idx
        # "无麦克风" 是 UI 占位串，不是真实设备名，按"未指定名字"处理
        self.name    = device_name if device_name and device_name != "无麦克风" else None
        self.ch      = 1
        self.sr      = SR          # 实际打开的采样率（写 WAV 用，可能 != SR）
        self._lock   = threading.Lock()
        self._chunks = []
        self._stream = None
        self.active  = False
        self.error   = None

    def _resolve_device(self):
        """刷新 PortAudio 后重新定位设备：优先按名字匹配（索引会随热插拔漂移），
        名字找不到再退回原索引、再退回当前系统默认输入。返回索引或 None。"""
        try:
            sd._terminate(); sd._initialize()
        except Exception:
            pass
        try:
            devs = sd.query_devices()
        except Exception:
            return self.device
        if self.name:                                  # 1) 按名字匹配仍在线的输入设备
            for i, d in enumerate(devs):
                if d['max_input_channels'] > 0 and d['name'] == self.name:
                    return i
        if self.device is not None:                    # 2) 原索引仍是有效输入设备就沿用
            try:
                di = int(self.device)
                if 0 <= di < len(devs) and devs[di]['max_input_channels'] > 0:
                    return di
            except Exception:
                pass
        try:                                           # 3) 退回系统默认输入
            di = sd.default.device[0]
            if di is not None and int(di) >= 0:
                return int(di)
        except Exception:
            pass
        return None

    def start(self):
        self._chunks = []
        self.error   = None
        self.active  = False
        if self.device is None and not self.name:
            return                                     # 用户明确选了"无音频"
        dev = self._resolve_device()
        if dev is None:
            self.error = "未找到可用麦克风设备"
            return
        self.device = dev
        try:
            info      = sd.query_devices(dev)
            self.ch   = max(1, min(2, info['max_input_channels']))
            native_sr = int(info.get('default_samplerate') or SR)
        except Exception:
            self.ch = 1; native_sr = SR
        # 候选采样率：原生优先（蓝牙 HFP 锁 24k/16k），再退常见值；第一个开得起来即用
        candidates = []
        for sr in (native_sr, SR, 48000, 24000, 16000):
            if sr and sr not in candidates:
                candidates.append(sr)
        for sr in candidates:
            try:
                self._stream = sd.InputStream(
                    device=dev, samplerate=sr,
                    channels=self.ch, dtype='float32',
                    blocksize=1024, callback=self._cb)
                self._stream.start()
                self.sr     = sr
                self.active = True
                self.error  = None
                return
            except Exception as e:
                self.error  = str(e)
                self._stream = None
        # 全部采样率都失败：self.error 保留最后一次异常，active 仍为 False

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
                wf.setframerate(self.sr)
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
def _render_sub_png(text: str, font_px: float, max_w: int):
    """把一句字幕渲染成透明 PNG（白粗体 + 半透明黑圆角底，同预览样式），
    返回 (路径, 宽, 高) 或 None。本机 ffmpeg 没编 libass/drawtext，只能走 overlay 叠图。"""
    from PyQt6.QtCore import QRect as _QRect
    text = (text or '').strip()
    if not text:
        return None
    f = QFont("PingFang SC"); f.setPixelSize(max(8, int(font_px))); f.setBold(True)
    fm = QFontMetrics(f)
    flags = int(Qt.TextFlag.TextWordWrap) | int(Qt.AlignmentFlag.AlignCenter)
    br = fm.boundingRect(_QRect(0, 0, int(max_w), 4000), flags, text)
    tw, th = max(1, br.width()), max(1, br.height())
    pad_x, pad_y = int(font_px * 0.5), int(font_px * 0.32)
    w, h = tw + pad_x * 2, th + pad_y * 2
    img = QImage(w, h, QImage.Format.Format_ARGB32)
    img.fill(QColor(0, 0, 0, 0))
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor(0, 0, 0, 153))      # rgba(0,0,0,0.6)，同预览
    rad = int(font_px * 0.3)
    p.drawRoundedRect(0, 0, w, h, rad, rad)
    p.setFont(f); p.setPen(QColor(255, 255, 255))
    p.drawText(_QRect(pad_x, pad_y, tw, th), flags, text)
    p.end()
    path = os.path.join(_TMPDIR, f"_subpng_{int(time.time()*1000000)}.png")
    return (path, w, h) if img.save(path, "PNG") else None


def merge_and_save(scr_video, cam_video, sys_wav, mic_wav, output, signals: MergeSignals,
                   cam_scale: float = 1.0, sys_audio_offset: float = 0.0,
                   trim_in: float = 0.0, trim_out=None,
                   vol_mix: float = 1.0, vol_sys: float = 1.0, vol_mic: float = 1.0,
                   subtitles=None):
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

        # 硬字幕烧录：每句字幕已在主线程渲染成透明 PNG，这里逐句 overlay 叠到合成画面上，
        # 按 enable=between(t,start,end) 控制显隐（时间为全片时间轴；输出级 -ss/-t 裁切会带着
        # 已烧像素一起平移，无需再对字幕时间做偏移）。本机 ffmpeg 无 libass/drawtext，故走叠图。
        if subtitles and subtitles.get('items') and vmap is not None:
            cur = vmap if vmap.startswith('[') else f'[{vmap}]'
            for i, it in enumerate(subtitles['items']):
                if not os.path.exists(it['png']):
                    continue
                inputs.append((it['png'], ['-loop', '1']))
                img_idx = len(inputs) - 1
                nxt = f'[vsub{i}]'
                filters.append(
                    f"{cur}[{img_idx}:v]overlay={it['x']}:{it['y']}:"
                    f"enable='between(t,{it['start']:.3f},{it['end']:.3f})'{nxt}")
                cur = nxt
            vmap = cur

        maps = (['-map', vmap] if vmap else [])
        meta = []
        disp = []

        if sys_in is not None and mic_in is not None:
            # 系统音延后 delay_ms（adelay 垫前导静音）后一分为二：一路进混音、一路作独立轨。
            # 麦克风不延后（其起始已≈画面）。混音从 0 起 ⇒ 麦克风不被拖后。
            # 每条轨按各自音量 volume=* 缩放（混音整体 / 系统音独立 / 麦克风独立）。
            sd_pre = f"[{sys_in}:a]adelay={delay_ms}:all=1," if delay_ms > 0 else f"[{sys_in}:a]"
            filters.append(f"{sd_pre}asplit=2[sa_mix][sa_out]")
            filters.append(f"[{mic_in}:a]asplit=2[ma_mix][ma_out]")
            filters.append("[sa_mix][ma_mix]amix=inputs=2:duration=longest[mixed0]")
            filters.append(f"[mixed0]volume={vol_mix:.3f}[mixed]")
            filters.append(f"[sa_out]volume={vol_sys:.3f}[sysout]")
            filters.append(f"[ma_out]volume={vol_mic:.3f}[micout]")
            maps += ['-map', '[mixed]', '-map', '[sysout]', '-map', '[micout]']
            meta += ['-metadata:s:a:0', 'title=Mixed (System + Mic)',
                     '-metadata:s:a:1', 'title=System Audio',
                     '-metadata:s:a:2', 'title=Microphone']
            disp += ['-disposition:a:0', 'default',
                     '-disposition:a:1', '0', '-disposition:a:2', '0']
        elif sys_in is not None:
            pre = f"[{sys_in}:a]adelay={delay_ms}:all=1," if delay_ms > 0 else f"[{sys_in}:a]"
            filters.append(f"{pre}volume={vol_sys:.3f}[sysout]")
            maps += ['-map', '[sysout]']
            meta += ['-metadata:s:a:0', 'title=System Audio']
        elif mic_in is not None:
            filters.append(f"[{mic_in}:a]volume={vol_mic:.3f}[micout]")
            maps += ['-map', '[micout]']
            meta += ['-metadata:s:a:0', 'title=Microphone']

        cmd = [_FFMPEG, '-y']
        for path, in_opts in inputs:
            cmd += in_opts + ['-i', path]
        if filters:
            cmd += ['-filter_complex', ';'.join(filters)]
        # -shortest: 以最短流为准截断，消除 ScreenCaptureKit stop 后的音频尾段
        cmd += maps + meta + disp + ['-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
                                      '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest']
        # 首尾裁切：输出级 -ss/-t（精确，作用于合成后统一时间轴的全部轨）
        if trim_in and trim_in > 0:
            cmd += ['-ss', f'{trim_in:.3f}']
        if trim_out is not None and trim_out > (trim_in or 0):
            cmd += ['-t', f'{trim_out - (trim_in or 0):.3f}']
        cmd += [output]

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


# ── 录制遮罩（录制中非录制区深色压暗，仅录制区镂空清晰；main 视觉） ────────────────
class FogOverlay(QWidget):
    """
    覆盖整个虚拟桌面的深色遮罩：录制中非录制区被深色压暗，仅录制区镂空清晰。
    鼠标穿透（拖动可透传到下方录屏区 ⇒ 录制中可移动），永不被录入（SCK 已排除本 app）。
    录制开始 → fade_in()，停止 → fade_out()；移动录屏区时 set_regions() 让镂空跟随。
    """
    MASK_ALPHA = 150   # 遮罩不透明度（≈60%，桌面明显变暗、录制区镂空清晰；非雾化）

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
        # clip = 全屏 − 所有录制区，再整体填深色 ⇒ 非录制区深色压暗、录制区镂空清晰
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
        self.on_moved      = None   # callback()：拖动录屏区后让控制条跟随
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
            if self.on_moved:
                self.on_moved()          # 控制条跟随

    def mouseReleaseEvent(self, e):
        # 折叠态：未拖动的点击 = 展开
        if self._collapsed and not self._moved:
            self._toggle_collapse()


# ── 独立控制条（横排 icon 模块；脱离录制区的顶层窗口，可拖动）────────────────────
class ControlBar(QWidget):
    """
    把原来嵌在摄像区底部的聚合控制条独立成顶层窗口：横排矢量图标 + 磨砂底 + 左侧拖动把手。
    · 录制阶段默认锚在 session 右侧、垂直居中（anchor_fn 提供 session 全局矩形），
      近右屏边自动翻到左侧；
    · 用户可从左侧把手拖动；拖走后转自由态（_user_pos），不再自动跟随，
      直到 reset_detach()（召唤/居中）复位；
    · 状态机仍在 CameraWindow，靠 set_stage() 驱动本条显示哪些图标。
    """
    BTN_W, BTN_H, BTN_GAP, PAD, GRIP_W = 40, 34, 6, 7, 0   # GRIP_W=0：钉死不可拖,无把手

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMouseTracking(True)

        isz   = QSize(20, 20)
        _hand = Qt.CursorShape.PointingHandCursor
        self._ic_record = make_icon("record",   REC_DOT,    20)   # 红点 = 录制
        self._ic_stop   = make_icon("stop",     ICON_LIGHT, 20)
        self._ic_cancel = make_icon("x",        ICON_LIGHT, 20)
        self._ic_save   = make_icon("check",    ICON_DARK,  20)
        self._ic_redo   = make_icon("redo",     ICON_LIGHT, 20)
        self._ic_trim   = make_icon("scissors", ICON_LIGHT, 20)
        self._ic_close  = make_icon("x",        ICON_LIGHT, 20)

        # 主操作（录制 / 停止 / 取消 / 保存，按状态变形）
        self.primary_btn = QPushButton(self)
        self.primary_btn.setFixedSize(self.BTN_W, self.BTN_H)
        self.primary_btn.setIconSize(isz); self.primary_btn.setCursor(_hand)
        self.primary_btn.clicked.connect(lambda: self.on_primary and self.on_primary())

        # 重录（仅 review 阶段可见）
        self.rerecord_btn = QPushButton(self)
        self.rerecord_btn.setFixedSize(self.BTN_W, self.BTN_H)
        self.rerecord_btn.setIcon(self._ic_redo); self.rerecord_btn.setIconSize(isz)
        self.rerecord_btn.setStyleSheet(ICON_SECONDARY_QSS); self.rerecord_btn.setCursor(_hand)
        self.rerecord_btn.clicked.connect(lambda: self.on_rerecord and self.on_rerecord())

        # 裁切（review 阶段：打开居中编辑页）
        self.trim_btn = QPushButton(self)
        self.trim_btn.setFixedSize(self.BTN_W, self.BTN_H)
        self.trim_btn.setIcon(self._ic_trim); self.trim_btn.setIconSize(isz)
        self.trim_btn.setStyleSheet(ICON_SECONDARY_QSS); self.trim_btn.setCursor(_hand)
        self.trim_btn.clicked.connect(lambda: self.on_trim and self.on_trim())

        # 关闭（收回菜单栏）
        self.close_btn = QPushButton(self)
        self.close_btn.setFixedSize(self.BTN_W, self.BTN_H)
        self.close_btn.setIcon(self._ic_close); self.close_btn.setIconSize(isz)
        self.close_btn.setStyleSheet(ICON_CLOSE_QSS); self.close_btn.setCursor(_hand)
        self.close_btn.clicked.connect(lambda: self.on_close and self.on_close())

        self.on_primary = self.on_rerecord = self.on_trim = self.on_close = None
        self.anchor_fn  = None        # () -> 摄像区窗口全局 QRect，用于贴底锚定
        self._pos_mode  = 'cam_bottom'  # 'cam_bottom'=贴摄像区底部居中 / 'corner'=屏幕左下角固定
        self._user_pos  = False       # 用户拖动过 → 自由态，不再自动跟随
        self._dock_rect = QRect()
        self._dragging  = False
        self._drag_gp   = QPoint(); self._drag_wp = QPoint()
        self.set_stage('ready')

    # ── 阶段 → 图标集 + 定位模式 ─────────────────────────────────────────────
    def set_stage(self, stage: str):
        b = self.primary_btn
        if stage == 'countdown':
            b.setIcon(self._ic_cancel); b.setStyleSheet(ICON_SECONDARY_QSS); b.setEnabled(True)
            btns = [b]
        elif stage == 'recording':
            b.setIcon(self._ic_stop);   b.setStyleSheet(ICON_SECONDARY_QSS); b.setEnabled(True)
            btns = [b]
        elif stage == 'stopping':       # 收尾过渡态：单个禁用主键
            b.setIcon(self._ic_record); b.setStyleSheet(ICON_PRIMARY_QSS);   b.setEnabled(False)
            btns = [b]
        elif stage == 'review':         # 后处理：↺ 重录 + ✂ 裁切 + ✓ 保存（主键先禁用，外部 400ms 后启用）
            b.setIcon(self._ic_save);   b.setStyleSheet(ICON_PRIMARY_QSS);   b.setEnabled(False)
            btns = [self.rerecord_btn, self.trim_btn, b]
        else:                           # 'ready'：● 录制 + ✕ 关闭
            b.setIcon(self._ic_record); b.setStyleSheet(ICON_PRIMARY_QSS);   b.setEnabled(True)
            btns = [b, self.close_btn]
        for w in (self.primary_btn, self.rerecord_btn, self.trim_btn, self.close_btn):
            w.setVisible(w in btns)
        # 录制/就绪/倒计时 = 贴摄像区底部居中；后处理(review) = 屏幕左下角固定。
        mode = 'corner' if stage == 'review' else 'cam_bottom'
        if mode != self._pos_mode:
            self._pos_mode = mode
            self._user_pos = False
        self._relayout(btns)
        self.follow()      # 每次都重摆：摄像区缩放/移动时贴底跟随（corner 则固定左下）

    def _relayout(self, btns):
        gap, pad, grip = self.BTN_GAP, self.PAD, self.GRIP_W
        total = len(btns) * self.BTN_W + (len(btns) - 1) * gap
        w = pad + grip + total + pad
        h = self.BTN_H + pad * 2
        self.resize(w, h)
        x = pad + grip
        for wdg in btns:
            wdg.move(x, pad); x += self.BTN_W + gap
        self._dock_rect = QRect(0, 0, w, h)
        self.update()

    def set_primary_enabled(self, on):  self.primary_btn.setEnabled(on)
    def set_rerecord_enabled(self, on): self.rerecord_btn.setEnabled(on)
    def reset_detach(self):             self._user_pos = False

    # ── 锚定 / 跟随 ─────────────────────────────────────────────────────────
    def follow(self):
        """录制/就绪阶段钉死在摄像区底部、水平居中；后处理固定屏幕左下角。
        固定不可拖（操作区=摄像区，控制条必须稳定在内、始终可见可点）。"""
        scr = QApplication.primaryScreen().geometry()
        bw, bh = self.width(), self.height()
        if self._pos_mode == 'corner':             # 后处理：屏幕左下角固定
            self.move(scr.left() + 24, scr.bottom() - bh - 24)
            return
        if not self.anchor_fn:                      # 录制/就绪：贴摄像区底部
            return
        a = self.anchor_fn()
        if a is None:
            return
        x = a.left() + (a.width() - bw) // 2        # 摄像区水平居中
        y = a.bottom() - HANDLE - bh - 6            # 叠在画面内底部、把手之上
        x = max(scr.left() + 4, min(x, scr.right() - bw - 4))
        y = max(scr.top() + 4, min(y, scr.bottom() - bh - 4))
        self.move(x, y)

    # ── 绘制 ────────────────────────────────────────────────────────────────
    def paintEvent(self, _):
        if self._dock_rect.isNull():
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self._dock_rect).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(QColor(255, 255, 255, 28), 1))
        p.setBrush(QColor(18, 18, 18, 150))
        p.drawRoundedRect(r, 16, 16)
        p.end()

    # 钉死在摄像区内,不可拖动：不处理鼠标拖拽（点击只归子按钮，磨砂底吞掉不移动）。


# ── 裁切时间轴（灰底轨道 + 黄框保留区间 + 两端手柄/缩略图 + 播放头）──────────────────
class TrimBar(QWidget):
    MARGIN = 10          # 两端留白（容纳手柄）
    MIN_GAP = 0.02       # 首尾最小间隔，防交叉
    HANDLE_HIT = 11      # 命中首/尾手柄的像素阈值（否则算定位播放头）
    YELLOW   = QColor(245, 184, 0)
    WAVE     = QColor(245, 165, 70)        # 选中音轨波纹
    WAVE_DIM = QColor(245, 165, 70, 70)    # 裁掉区间内的波纹

    def __init__(self):
        super().__init__()
        self.setFixedHeight(72)
        self.setMouseTracking(True)
        self.in_frac   = 0.0
        self.out_frac  = 1.0
        self.play_frac = None         # 播放头位置（None=不显示）
        self.peaks     = []           # 选中音轨的波纹峰值（0..1）
        self.on_change = None         # callback(frac, which)：拖首/尾手柄
        self.on_seek   = None         # callback(frac)：点/拖轨道定位播放头
        self._drag     = None         # 'in' | 'out' | 'seek' | None

    def _tw(self):  return max(1, self.width() - 2 * self.MARGIN)
    def _x(self, f): return self.MARGIN + f * self._tw()
    def _frac(self, x): return min(1.0, max(0.0, (x - self.MARGIN) / self._tw()))

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QRectF(self.MARGIN, 8, self._tw(), self.height() - 16)
        # 灰底轨道
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor(70, 70, 72))
        p.drawRoundedRect(track, 8, 8)
        xi, xo = self._x(self.in_frac), self._x(self.out_frac)
        sel = QRectF(xi, track.top(), xo - xi, track.height())
        # 选中音轨波纹（铺满轨道、居中包络；保留区间内亮、区间外暗）
        if self.peaks:
            n = len(self.peaks); cy = track.center().y(); amp = track.height() * 0.42
            bw = self._tw() / n
            for i, pk in enumerate(self.peaks):
                x = self.MARGIN + (i + 0.5) * bw
                p.setPen(QPen(self.WAVE if xi <= x <= xo else self.WAVE_DIM, max(1.0, bw * 0.7)))
                h = max(1.0, pk * amp)
                p.drawLine(QPointF(x, cy - h), QPointF(x, cy + h))
        # 黄框选中保留区间
        p.setBrush(Qt.BrushStyle.NoBrush); p.setPen(QPen(self.YELLOW, 3))
        p.drawRoundedRect(sel.adjusted(1.5, 1.5, -1.5, -1.5), 8, 8)
        # 两端手柄（黄、带竖纹）
        for x in (xi, xo):
            hb = QRectF(x - 7, track.top() - 2, 14, track.height() + 4)
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(self.YELLOW)
            p.drawRoundedRect(hb, 5, 5)
            p.setPen(QPen(QColor(120, 90, 0), 1.4))
            cy = track.center().y()
            for dx in (-2.5, 2.5):
                p.drawLine(QPointF(x + dx, cy - 8), QPointF(x + dx, cy + 8))
        # 播放头
        if self.play_frac is not None:
            px = self._x(self.play_frac)
            p.setPen(QPen(QColor(255, 255, 255, 235), 2))
            p.drawLine(QPointF(px, track.top() - 2), QPointF(px, track.bottom() + 2))
        p.end()

    def mousePressEvent(self, e):
        x = e.position().x()
        di, do = abs(x - self._x(self.in_frac)), abs(x - self._x(self.out_frac))
        if min(di, do) <= self.HANDLE_HIT:
            self._drag = 'in' if di <= do else 'out'   # 命中手柄 → 调首/尾
        else:
            self._drag = 'seek'                        # 轨道其他位置 → 定位播放头
        self._apply_drag(x)

    def mouseMoveEvent(self, e):
        if self._drag and e.buttons() == Qt.MouseButton.LeftButton:
            self._apply_drag(e.position().x())
        else:
            self.setCursor(Qt.CursorShape.SizeHorCursor)

    def mouseReleaseEvent(self, e):
        self._drag = None

    def _apply_drag(self, x):
        f = self._frac(x)
        if self._drag == 'in':
            self.in_frac = min(f, self.out_frac - self.MIN_GAP)
            if self.play_frac is not None and self.play_frac < self.in_frac:
                self.play_frac = self.in_frac
            self.update()
            if self.on_change: self.on_change(self.in_frac, 'in')
        elif self._drag == 'out':
            self.out_frac = max(f, self.in_frac + self.MIN_GAP)
            if self.play_frac is not None and self.play_frac > self.out_frac:
                self.play_frac = self.out_frac
            self.update()
            if self.on_change: self.on_change(self.out_frac, 'out')
        else:   # seek：定位播放头（夹在保留区间内）
            f = min(max(f, self.in_frac), self.out_frac)
            self.play_frac = f
            self.update()
            if self.on_seek: self.on_seek(f)


# ── 字幕浮层（识别后浮在预览上：可拖动、双击编辑）──────────────────────────────
class SubtitleLabel(QLabel):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWordWrap(True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("color:#fff;background:rgba(0,0,0,0.6);border-radius:6px;"
                           "padding:4px 10px;font-size:10pt;font-weight:600;")
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.hide()
        self.on_edit = None       # 双击就地编辑回调
        self._drag = False; self._gp = QPoint(); self._wp = QPoint()

    def set_subtitle(self, text, recenter=True):
        text = (text or "").strip()
        if not text:
            self.hide(); return
        self.setText(text)
        pw = self.parent().width()
        maxw = max(80, int(pw * 0.9))
        self.setFixedWidth(maxw)
        h = self.heightForWidth(maxw)
        self.resize(maxw, h if h > 0 else self.sizeHint().height())
        # 自适应内容宽度：窄于上限时收窄居中
        nat = self.sizeHint().width()
        if nat < maxw:
            self.setFixedWidth(nat)
            self.resize(nat, self.heightForWidth(nat) or self.height())
        self._center() if recenter else self._clamp()
        self.show(); self.raise_()

    def _center(self):
        pw, ph = self.parent().width(), self.parent().height()
        self.move((pw - self.width()) // 2, (ph - self.height()) // 2)

    def _clamp(self):
        pw, ph = self.parent().width(), self.parent().height()
        self.move(max(0, min(self.x(), pw - self.width())),
                  max(0, min(self.y(), ph - self.height())))

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = True
            self._gp = e.globalPosition().toPoint(); self._wp = self.pos()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, e):
        if self._drag and e.buttons() == Qt.MouseButton.LeftButton:
            self.move(self._wp + (e.globalPosition().toPoint() - self._gp)); self._clamp()

    def mouseReleaseEvent(self, e):
        self._drag = False; self.setCursor(Qt.CursorShape.OpenHandCursor)

    def mouseDoubleClickEvent(self, e):
        if self.on_edit:
            self.on_edit()        # 在当前字幕栏就地编辑（不弹独立编辑框）


# ── 裁切编辑页（桌面正中；参考深色剪辑样式：大预览 + 黄框时间轴 + 参数面板 + 取消/完成）─
class EditorPanel(QWidget):
    W, H, PAD, PV_H = 680, 678, 24, 330
    PANEL_H = 126
    SIZE_RATE = 0.45   # 粗略码率估算：720×1280 H.264 + 音频 ≈ 0.45 MB/s
    WAVE_BINS = 280
    RADIO_QSS = ("QPushButton{border:1.5px solid rgba(255,255,255,0.45);border-radius:8px;background:transparent;}"
                 "QPushButton:hover{border-color:rgba(255,255,255,0.85);}"
                 "QPushButton:checked{background:#F5A53C;border-color:#F5A53C;}")
    TRACK_NAMES = {'mix': '混音', 'sys': '系统音', 'mic': '麦克风'}

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(self.W, self.H)

        self.on_apply    = None   # callback(in,out,infrac,outfrac,vmix,vsys,vmic)
        self.on_cancel   = None   # callback()：取消编辑（不套用），展开回录制框
        self.on_rerecord = None   # 兼容旧注入（未用）
        self._cap_scr = self._cap_cam = None
        self._n_scr = self._n_cam = 0
        self._fps = 30.0; self._dur = 0.0
        self._composed_h = 0
        self._present = set()              # 实际有的音轨 key
        self._active  = 'mix'              # 当前选中的预览/波纹音轨
        self._audio   = {}                 # key -> int16 ndarray（预览缓冲）
        self._audio_wav = {}               # key -> wav 路径（供字幕识别）
        self._audio_sr = 0; self._audio_tmps = []
        self._rec_busy = False; self._rec_done = False; self._rec_text = ""
        self._rec_lang = 0        # SUBTITLE_LANGS 索引（识别语言；麦克风常需切中文）
        self._rec_t = QTimer(self); self._rec_t.timeout.connect(self._rec_poll)
        self._playing = False; self._t0 = 0.0; self._seg_dur = 0.0; self._play_anchor = 0.0
        self._play_proc = None                       # afplay 子进程（走系统输出=耳机）
        self._seg_wav   = os.path.join(_TMPDIR, f"_edit_play_{id(self)}.wav")
        self._phrases = []           # 识别出的分句 [{start,end,text}]（按节奏）
        self._rec_phrases = []
        self._cur_phrase = -2; self._sub_placed = False; self._editing_sub = False
        self._play_t = QTimer(self); self._play_t.timeout.connect(self._play_tick)
        self._wave_t = QTimer(self); self._wave_t.timeout.connect(self._wave_tick)
        self._vol_t  = QTimer(self); self._vol_t.setSingleShot(True)
        self._vol_t.timeout.connect(self._apply_vol)
        self._vol_pending = None
        self._drag_gp = QPoint(); self._drag_wp = QPoint(); self._dragging = False

        P, W = self.PAD, self.W
        # 大预览（黑底，合成画面居中、letterbox）
        self.preview = QLabel(self)
        self.preview.setGeometry(P, P, W - 2 * P, self.PV_H)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setStyleSheet("background:#000;border-radius:10px;")
        self.sub = SubtitleLabel(self.preview)        # 识别字幕浮层（随播放、可拖、双击就地编辑）
        self.sub.on_edit = self._edit_subtitle
        self.sub_edit = QLineEdit(self.preview)       # 就地编辑框（双击字幕时盖在原位）
        self.sub_edit.setStyleSheet(
            "QLineEdit{color:#fff;background:rgba(0,0,0,0.82);border:1px solid #0A84FF;"
            "border-radius:6px;padding:4px 10px;font-size:10pt;font-weight:600;}")
        self.sub_edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.sub_edit.hide()
        self.sub_edit.returnPressed.connect(self._commit_sub_edit)
        self.sub_edit.editingFinished.connect(self._commit_sub_edit)

        # 时间轴行：播放键 + 黄框时间轴（轨上显示选中音轨波纹）
        ty = P + self.PV_H + 18
        self.play_btn = QPushButton(self)
        self.play_btn.setGeometry(P, ty + 12, 48, 48)
        self.play_btn.setIcon(make_icon("play", ICON_LIGHT, 22)); self.play_btn.setIconSize(QSize(22, 22))
        self.play_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.play_btn.setStyleSheet(
            "QPushButton{background:rgba(255,255,255,0.08);border:none;border-radius:24px;}"
            "QPushButton:hover{background:rgba(255,255,255,0.16);}")
        self.play_btn.clicked.connect(self._toggle_play)

        self.bar = TrimBar(); self.bar.setParent(self)
        self.bar.setGeometry(P + 60, ty, W - P - (P + 60), 72)
        self.bar.on_change = self._on_scrub
        self.bar.on_seek = self._on_seek

        self.time_lbl = QLabel("", self)
        self.time_lbl.setGeometry(P + 60, ty + 72 + 2, self.bar.width(), 16)
        self.time_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.time_lbl.setStyleSheet("color:rgba(255,255,255,0.6);font-size:11px;background:transparent;")

        # 参数面板：左=尺寸/大小，右=三条音轨（勾选=选中波纹/预览 + 音量）
        self._py = ty + 96
        cap_s = "color:rgba(255,255,255,0.5);font-size:12px;background:transparent;"
        val_s = "color:rgba(255,255,255,0.92);font-size:13px;background:transparent;"
        half = (self.W - 2 * P - 16) // 2
        rx = P + half + 16                    # 右子面板左内缘
        self._mk_lbl("尺寸", cap_s, P + 20, self._py + 24, 60, 18)
        self.dim_val  = self._mk_lbl("—", val_s, P + 84, self._py + 24, 200, 18)
        self._mk_lbl("大小", cap_s, P + 20, self._py + 64, 60, 18)
        self.size_val = self._mk_lbl("—", val_s, P + 84, self._py + 64, 200, 18)
        self._mk_lbl("音轨", cap_s, rx + 16, self._py + 12, 60, 18)
        # 字幕识别（识别选中音轨 → 居中字幕浮层）+ 语言切换（麦克风常需切中文）
        chip_qss = (
            "QPushButton{color:rgba(255,255,255,0.92);background:rgba(255,255,255,0.12);"
            "border:1px solid rgba(255,255,255,0.2);border-radius:8px;font-size:12px;}"
            "QPushButton:hover{background:rgba(255,255,255,0.2);}"
            "QPushButton:disabled{color:rgba(255,255,255,0.4);background:rgba(255,255,255,0.06);}")
        panel_r = P + half + 16 + half          # 右子面板右边缘
        self.sub_btn = QPushButton("字幕识别", self)
        self.sub_btn.setGeometry(panel_r - 16 - 84, self._py + 10, 84, 22)
        self.sub_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.sub_btn.setStyleSheet(chip_qss)
        self.sub_btn.clicked.connect(self._recognize_subtitle)
        self.lang_btn = QPushButton(SUBTITLE_LANGS[0][0], self)
        self.lang_btn.setGeometry(panel_r - 16 - 84 - 6 - 40, self._py + 10, 40, 22)
        self.lang_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.lang_btn.setStyleSheet(chip_qss)
        self.lang_btn.clicked.connect(self._cycle_lang)
        sl_qss = (
            "QSlider::groove:horizontal{height:4px;background:rgba(255,255,255,0.18);border-radius:2px;}"
            "QSlider::sub-page:horizontal{height:4px;background:#0A84FF;border-radius:2px;}"
            "QSlider::handle:horizontal{width:13px;height:13px;margin:-5px 0;border-radius:6px;background:#fff;}")
        xdot, xn, xs, xp = rx + 12, rx + 34, rx + 86, rx + 224
        self._tracks = {}
        for i, key in enumerate(('mix', 'sys', 'mic')):
            self._tracks[key] = self._mk_track(key, val_s, cap_s, sl_qss,
                                               xdot, xn, xs, xp, self._py + 36 + i * 28)

        # 底部：取消（左）/ 完成（右）
        by = self.H - P - 40
        self.cancel_btn = QPushButton("取消", self)
        self.cancel_btn.setGeometry(P, by, 96, 40)
        self.cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_btn.setStyleSheet(
            "QPushButton{color:rgba(255,255,255,0.9);background:rgba(255,255,255,0.10);"
            "border:1px solid rgba(255,255,255,0.18);border-radius:10px;font-size:14px;}"
            "QPushButton:hover{background:rgba(255,255,255,0.16);}")
        self.cancel_btn.clicked.connect(self._cancel)

        self.done_btn = QPushButton("完成", self)
        self.done_btn.setGeometry(W - P - 116, by, 116, 40)
        self.done_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.done_btn.setStyleSheet(
            "QPushButton{color:#fff;background:#0A84FF;border:none;border-radius:10px;"
            "font-size:14px;font-weight:600;}"
            "QPushButton:hover{background:#3398ff;}QPushButton:pressed{background:#0a6fd6;}")
        self.done_btn.clicked.connect(self._apply)

    def _mk_lbl(self, text, style, x, y, w, h):
        lb = QLabel(text, self); lb.setGeometry(x, y, w, h); lb.setStyleSheet(style)
        return lb

    def _mk_track(self, key, val_s, cap_s, sl_qss, xdot, xn, xs, xp, y):
        dot = QPushButton(self); dot.setGeometry(xdot, y + 3, 16, 16); dot.setCheckable(True)
        dot.setStyleSheet(self.RADIO_QSS); dot.setCursor(Qt.CursorShape.PointingHandCursor)
        dot.clicked.connect(lambda _=False, k=key: self._set_active(k))
        nl = QLabel(self.TRACK_NAMES[key], self); nl.setGeometry(xn, y, 48, 22); nl.setStyleSheet(val_s)
        sl = QSlider(Qt.Orientation.Horizontal, self); sl.setGeometry(xs, y + 3, xp - xs - 8, 16)
        sl.setRange(0, 200); sl.setValue(100); sl.setStyleSheet(sl_qss)
        sl.setCursor(Qt.CursorShape.PointingHandCursor)
        pl = QLabel("100%", self); pl.setGeometry(xp, y, 40, 22); pl.setStyleSheet(cap_s)
        pl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        sl.valueChanged.connect(lambda v, p=pl, k=key: self._on_vol_changed(v, p, k))
        return {'key': key, 'dot': dot, 'name': nl, 'slider': sl, 'pct': pl}

    def _on_vol_changed(self, v, pct_lbl, key):
        pct_lbl.setText(f"{v}%")
        self._vol_pending = key
        self._vol_t.start(130)          # 防抖：停手 130ms 后即时重播套用新音量

    def _apply_vol(self):
        key = self._vol_pending
        if not key or key not in self._present or not self._playing:
            return                      # 没在播：音量已记录，下次播放/导出即生效
        if self._active != key:
            self._set_active(key)       # 调谁就听谁（内部重启播放）
        else:
            self._restart_audio()

    def _row_visible(self, row, show):
        for w in (row['dot'], row['name'], row['slider'], row['pct']):
            w.setVisible(show)

    def _vol_of(self, key):
        row = self._tracks.get(key)
        return row['slider'].value() / 100.0 if row else 1.0

    # ── 公开：打开录屏区+摄像区两段开始裁切 ───────────────────────────────────
    @staticmethod
    def _probe_duration(path):
        """ffprobe 读容器真实时长（秒）。SCK 视频 fps 元数据不可靠，帧数/fps 会偏，
        容器 duration 才是真实墙钟时长。失败返回 0。"""
        if not (path and os.path.exists(path)):
            return 0.0
        try:
            r = subprocess.run([_FFPROBE, '-v', 'error', '-show_entries', 'format=duration',
                                '-of', 'default=nokey=1:noprint_wrappers=1', path],
                               capture_output=True, text=True, timeout=20)
            return float((r.stdout or '0').strip() or 0)
        except Exception:
            return 0.0

    def open_video(self, scr_path, cam_path, sys_path=None, mic_path=None,
                   av_offset=0.0, in_frac=0.0, out_frac=1.0, vols=(1.0, 1.0, 1.0)):
        self._release()
        self._cap_scr = cv2.VideoCapture(scr_path) if scr_path and os.path.exists(scr_path) else None
        self._cap_cam = cv2.VideoCapture(cam_path) if cam_path and os.path.exists(cam_path) else None
        self._n_scr = int(self._cap_scr.get(cv2.CAP_PROP_FRAME_COUNT)) if self._cap_scr else 0
        self._n_cam = int(self._cap_cam.get(cv2.CAP_PROP_FRAME_COUNT)) if self._cap_cam else 0
        self._fps = (self._cap_scr.get(cv2.CAP_PROP_FPS) if self._cap_scr else 30.0) or 30.0
        # _dur 优先用容器真实时长（ffprobe）：SCK 抓屏常把 fps 标成 60(屏幕刷新率)，
        # 帧数/fps 会算成真实时长的一半 ⇒ 后半段字幕的 frac*_dur 永远到不了、整段不显示。
        self._dur = self._probe_duration(scr_path) or self._probe_duration(cam_path) \
            or (self._n_scr / self._fps if self._fps else 0.0)
        # 合成尺寸（与导出一致：录屏区+摄像区按 OUT_W 宽 vstack）
        h = 0
        for cap in (self._cap_scr, self._cap_cam):
            if cap is not None:
                cw = cap.get(cv2.CAP_PROP_FRAME_WIDTH); chh = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
                if cw > 0: h += int(round(OUT_W * chh / cw))
        self._composed_h = h
        self.dim_val.setText(f"{OUT_W} × {h}" if h else "—")
        # 音轨：按实际有的轨显示（混音=系统音+麦克风都有时才有）
        has_s = bool(sys_path and os.path.exists(sys_path))
        has_m = bool(mic_path and os.path.exists(mic_path))
        present = set()
        if has_s and has_m: present.add('mix')
        if has_s: present.add('sys')
        if has_m: present.add('mic')
        self._present = present
        vmix, vsys, vmic = vols
        for key, v in (('mix', vmix), ('sys', vsys), ('mic', vmic)):
            row = self._tracks[key]; show = key in present
            self._row_visible(row, show)
            if show: row['slider'].setValue(int(round(v * 100)))
        self._active = 'mix' if 'mix' in present else (next(iter(present)) if present else 'mix')
        for k, row in self._tracks.items():
            row['dot'].setChecked(k == self._active)
        # 时间轴
        self.bar.peaks = []
        self.bar.in_frac, self.bar.out_frac = in_frac, out_frac
        self.bar.play_frac = in_frac             # 播放头初始在保留区起点
        self.sub.hide(); self.sub_edit.hide()    # 重置字幕浮层
        self._phrases = []; self._cur_phrase = -2; self._sub_placed = False; self._editing_sub = False
        self._rec_busy = False; self._rec_t.stop()
        self.sub_btn.setEnabled(True); self.sub_btn.setText("字幕识别")
        self._show(in_frac)
        self._build_audio_async(sys_path, mic_path, av_offset)   # 后台备好各轨预览声音
        if present:
            self._wave_t.start(160)        # 音频备好就刷波纹
        sc = QApplication.primaryScreen().geometry()
        self.move(sc.x() + (sc.width() - self.width()) // 2,
                  sc.y() + (sc.height() - self.height()) // 2)
        self.show(); self.raise_(); self.activateWindow()

    # ── 取帧 / 合成 ───────────────────────────────────────────────────────────
    def _read(self, cap, n, frac):
        if cap is None or n <= 0:
            return None
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(round(frac * (n - 1))))
        ret, fr = cap.read()
        return fr if ret else None

    def _composite_pix(self, frac):
        """录屏区在上、摄像区在下拼成完整画面（与 vstack 导出一致），返回 QPixmap。"""
        sf = self._read(self._cap_scr, self._n_scr, frac)
        cf = self._read(self._cap_cam, self._n_cam, frac)
        W = 360
        parts = []
        for f in (sf, cf):
            if f is not None:
                h, w = f.shape[:2]
                parts.append(cv2.resize(f, (W, max(1, int(h * W / w)))))
        if not parts:
            return None
        comp = parts[0] if len(parts) == 1 else np.vstack(parts)
        rgb = cv2.cvtColor(comp, cv2.COLOR_BGR2RGB)
        h, w, c = rgb.shape
        return QPixmap.fromImage(QImage(rgb.data, w, h, w * c, QImage.Format.Format_RGB888))

    def _show(self, frac):
        self._update_info()
        pix = self._composite_pix(frac)
        if pix is None:
            return
        self.preview.setPixmap(pix.scaled(
            self.preview.width(), self.preview.height(),
            Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def _on_scrub(self, frac, which):
        self._stop_play()
        pix = self._composite_pix(frac)
        if pix is not None:
            self.preview.setPixmap(pix.scaled(
                self.preview.width(), self.preview.height(),
                Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        self._update_info(); self.bar.update()

    def _update_info(self):
        def mmss(s): return f"{int(s)//60:02d}:{int(s)%60:02d}"
        ti, to = self.bar.in_frac * self._dur, self.bar.out_frac * self._dur
        self.time_lbl.setText(f"保留 {mmss(ti)} – {mmss(to)}    共 {mmss(self._dur)}")
        self.size_val.setText(f"≈ {max(0.0, (to - ti)) * self.SIZE_RATE:.1f} MB（预估）")

    # ── 三轨预览音频（ffmpeg 把系统音 caf / 麦克风 wav / 混音各导成 wav，sd 播放）──────
    def _build_audio_async(self, sys_path, mic_path, av_offset):
        self._audio = {}; self._audio_wav = {}
        has_s = bool(sys_path and os.path.exists(sys_path))
        has_m = bool(mic_path and os.path.exists(mic_path))
        if not (has_s or has_m):
            return
        ms = int(round(av_offset * 1000)) if av_offset and av_offset > 0 else 0

        def work():
            tmps = []
            def run_load(extra, tag):
                tmp = os.path.join(_TMPDIR, f"_edit_{tag}_{int(time.time()*1000)}.wav")
                cmd = [_FFMPEG, '-y'] + extra + ['-ac', '2', '-ar', '44100', tmp]
                try:
                    r = subprocess.run(cmd, capture_output=True, timeout=120)
                    if r.returncode != 0 or not os.path.exists(tmp):
                        return None
                    tmps.append(tmp); self._audio_wav[tag] = tmp
                    wf = wave.open(tmp, 'rb')
                    ch, n = wf.getnchannels(), wf.getnframes()
                    raw = wf.readframes(n); wf.close()
                    a = np.frombuffer(raw, dtype=np.int16)
                    return a.reshape(-1, ch) if ch > 1 else a
                except Exception as ex:
                    print(f"[editor audio:{tag}] {ex}"); return None
            try:
                if has_s and has_m:
                    pre = f"[0:a]adelay={ms}:all=1[s];" if ms > 0 else "[0:a]anull[s];"
                    a = run_load(['-i', sys_path, '-i', mic_path, '-filter_complex',
                                  pre + "[s][1:a]amix=inputs=2:duration=longest[a]", '-map', '[a]'], 'mix')
                    if a is not None: self._audio['mix'] = a
                if has_s:
                    ex = ['-i', sys_path]
                    if ms > 0: ex += ['-filter_complex', f"[0:a]adelay={ms}:all=1[a]", '-map', '[a]']
                    a = run_load(ex, 'sys')
                    if a is not None: self._audio['sys'] = a
                if has_m:
                    a = run_load(['-i', mic_path], 'mic')
                    if a is not None: self._audio['mic'] = a
                self._audio_sr = 44100; self._audio_tmps = tmps
            except Exception as ex:
                print(f"[editor audio] {ex}")
        threading.Thread(target=work, daemon=True).start()

    # ── 选中音轨：波纹 + 预览/播放 ───────────────────────────────────────────
    def _set_active(self, key):
        if key not in self._present:               # 不可选 → 回弹到当前选中
            for k, row in self._tracks.items():
                row['dot'].setChecked(k == self._active)
            return
        self._active = key
        for k, row in self._tracks.items():
            row['dot'].setChecked(k == key)
        if key in self._audio:
            self._refresh_wave()
        else:
            self._wave_t.start(160)
        if self._playing:
            self._restart_audio()                  # 切轨即换声（重写段 wav 再 afplay）

    def _peaks(self, arr):
        if arr is None or len(arr) == 0:
            return []
        mono = np.abs((arr.mean(axis=1) if arr.ndim > 1 else arr).astype(np.float32))
        n = len(mono); B = min(self.WAVE_BINS, n)
        idx = np.linspace(0, n, B + 1).astype(int)
        vals = [float(mono[idx[i]:idx[i+1]].max()) if idx[i+1] > idx[i] else 0.0 for i in range(B)]
        mx = max(vals) or 1.0
        return [v / mx for v in vals]

    def _refresh_wave(self):
        self.bar.peaks = self._peaks(self._audio.get(self._active))
        self.bar.update()

    def _wave_tick(self):
        if self._active in self._audio:
            self._refresh_wave(); self._wave_t.stop()

    # ── 字幕识别（识别选中音轨 → 居中字幕浮层）──────────────────────────────────
    def _cycle_lang(self):
        self._rec_lang = (self._rec_lang + 1) % len(SUBTITLE_LANGS)
        self.lang_btn.setText(SUBTITLE_LANGS[self._rec_lang][0])

    def _recognize_subtitle(self):
        if self._rec_busy:
            return
        wav = self._audio_wav.get(self._active)
        if not wav or not os.path.exists(wav):
            self.sub.set_subtitle("（音频还在准备，请稍候再点）"); return
        if not (os.path.exists(WHISPER_BIN) and os.path.exists(WHISPER_MODEL)):
            self.sub.set_subtitle("（缺少 whisper 识别引擎/模型）"); return
        lang = SUBTITLE_LANGS[self._rec_lang][1]
        self._rec_busy = True; self._rec_done = False; self._rec_text = ""
        self.sub_btn.setEnabled(False); self.sub_btn.setText("识别中…")

        def work():
            try:
                self._rec_phrases = self._recognize_file(wav, lang)
            except Exception as ex:
                print(f"[subtitle recog] {ex}"); self._rec_phrases = []
            self._rec_done = True
        threading.Thread(target=work, daemon=True).start()
        self._rec_t.start(200)

    def _recognize_file(self, wav, lang):
        """whisper.cpp 本地识别：转 16k 单声道 → whisper-cli 出 JSON(段级毫秒时间戳) → 解析。
        返回 [{start,end,text}]（秒，全轨时间轴）。整段识别、不截断、时间戳干净，
        替代旧的苹果 SFSpeech 分块方案（切块/累积偏移/长音频截断全部消除）。"""
        import json as _json, shutil as _sh
        workdir = os.path.join(_TMPDIR, f"_wh_{int(time.time()*1000)}")
        try:
            os.makedirs(workdir, exist_ok=True)
            norm = os.path.join(workdir, "a16k.wav")
            # 噪声门压掉麦克风里微弱的源视频串扰（跟读时源声会漏进麦克风），否则 whisper/VAD
            # 抓到那层弱串扰把时间戳标早 1~3s（字幕比真实说话声早出）。highpass 去低频底噪。
            subprocess.run([_FFMPEG, '-y', '-i', wav,
                            '-af', 'agate=threshold=0.04:ratio=9:attack=5:release=150,highpass=f=120',
                            '-ar', '16000', '-ac', '1', norm],
                           capture_output=True, timeout=180)
            if not os.path.exists(norm):
                return []
            out = os.path.join(workdir, "out")
            cmd = [WHISPER_BIN, '-m', WHISPER_MODEL, '-f', norm,
                   '-oj', '-of', out, '-ml', '42', '-nt', '-sow']
            if os.path.exists(WHISPER_VAD):     # VAD：只在真有语音处识别，时间戳贴真实说话，
                cmd += ['--vad', '--vad-model', WHISPER_VAD]   # 消除静音处幻觉文本导致的字幕早出 1~2s
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            jp = out + '.json'
            if r.returncode != 0 or not os.path.exists(jp):
                print(f"[whisper] rc={r.returncode} {(r.stderr or '')[-200:]}")
                return []
            with open(jp, encoding='utf-8') as f:
                data = _json.load(f)
            phrases = []
            for s in data.get('transcription', []):
                tx = (s.get('text') or '').strip()
                if not tx or tx.startswith('['):       # 过滤 [BLANK_AUDIO] 等非语音标记
                    continue
                o = s.get('offsets') or {}
                st = float(o.get('from', 0)) / 1000.0
                en = float(o.get('to', 0)) / 1000.0
                if en > st:
                    phrases.append({'start': st, 'end': en, 'text': tx})
            return phrases
        except Exception as ex:
            print(f"[whisper] {ex}"); return []
        finally:
            try: _sh.rmtree(workdir)
            except Exception: pass

    def _group_phrases(self, words):
        """按停顿(>0.55s)或句长(>38字符)切句 → 字幕按节奏分段。"""
        phrases, cur = [], []
        for s, e, w in words:
            if not w:
                continue
            if cur:
                gap = s - cur[-1][1]
                curlen = sum(len(x[2]) + 1 for x in cur)
                if gap > 0.55 or curlen + len(w) > 38:
                    phrases.append(self._mk_phrase(cur)); cur = []
            cur.append((s, e, w))
        if cur:
            phrases.append(self._mk_phrase(cur))
        return phrases

    @staticmethod
    def _mk_phrase(words):
        return {'start': words[0][0], 'end': words[-1][1],
                'text': ' '.join(w[2] for w in words).strip()}

    # ── 随播放同步字幕 + 就地编辑 ───────────────────────────────────────────────
    def _phrase_at(self, t):
        """返回当前时刻 t 落在哪一句的「实际语音区间」内（带小余量，停顿时返回 -1）。
        字幕只在该句说话期间显示、停顿即隐藏 ⇒ 出现节奏跟所选音轨的声音一致（剪映式）。"""
        for i, p in enumerate(self._phrases):
            if p['start'] - 0.05 <= t <= p['end'] + 0.35:
                return i
        return -1

    def _update_subtitle(self, frac):
        if self._editing_sub or not self._phrases:
            return
        idx = self._phrase_at(frac * self._dur)
        if idx == self._cur_phrase:
            return
        self._cur_phrase = idx
        if idx < 0:
            self.sub.hide()
        else:
            ph = self._phrases[idx]
            self._dbg(f"  SUB t={frac*self._dur:.2f} frac={frac:.3f} idx={idx} "
                      f"ph=[{ph['start']:.2f},{ph['end']:.2f}] '{ph['text'][:24]}'")
            self.sub.set_subtitle(ph['text'], recenter=not self._sub_placed)
            self._sub_placed = True

    def _edit_subtitle(self):
        """双击字幕：在当前字幕栏原位起一个编辑框（不弹独立对话框）。"""
        if not self.sub.isVisible():
            return
        self._editing_sub = True
        g = self.sub.geometry()
        self.sub_edit.setGeometry(g.x(), g.y(), max(180, g.width()), max(26, g.height()))
        self.sub_edit.setText(self.sub.text())
        self.sub.hide()
        self.sub_edit.show(); self.sub_edit.raise_()
        self.sub_edit.setFocus(); self.sub_edit.selectAll()
        # 编辑期间全局监听：点到编辑框外即提交（预览/时间轴等不接收焦点，
        # 单靠 editingFinished 失焦不触发）
        QApplication.instance().installEventFilter(self)

    def eventFilter(self, obj, ev):
        if self._editing_sub and ev.type() == QEvent.Type.MouseButtonPress:
            gp = ev.globalPosition().toPoint()
            r = QRect(self.sub_edit.mapToGlobal(QPoint(0, 0)), self.sub_edit.size())
            if not r.contains(gp):
                self._commit_sub_edit()          # 框外按下 → 提交回字幕态（不拦截，继续传递）
        return False

    def _commit_sub_edit(self):
        if not self._editing_sub:
            return
        self._editing_sub = False
        QApplication.instance().removeEventFilter(self)
        txt = self.sub_edit.text().strip()
        self.sub_edit.hide()
        if self._phrases and 0 <= self._cur_phrase < len(self._phrases):
            self._phrases[self._cur_phrase]['text'] = txt
        if txt:
            self.sub.move(self.sub_edit.x(), self.sub_edit.y())
            self.sub.set_subtitle(txt, recenter=False)
        else:
            self.sub.hide()

    def _rec_poll(self):
        if not self._rec_done:
            return
        self._rec_t.stop()
        self._rec_busy = False
        self.sub_btn.setEnabled(True); self.sub_btn.setText("字幕识别")
        self._phrases = self._rec_phrases or []
        self._cur_phrase = -2; self._sub_placed = False
        if self._phrases:
            # 识别完成立即显示首句作确认：播放头常停在首句之前的静音段，
            # 若只按播放头定位会一直空白，让用户误以为没识别出来。播放后 _update_subtitle 接管同步。
            self._cur_phrase = 0
            self.sub.set_subtitle(self._phrases[0]['text'], recenter=True)
            self._sub_placed = True
        else:
            self.sub.set_subtitle("（未识别到语音）"); self._sub_placed = True

    def _write_seg_wav(self, start=None):
        """把选中音轨的 [start,out] 段（套音量）写成临时 wav，供 afplay 播放。"""
        arr = self._audio.get(self._active)
        if arr is None or self._audio_sr <= 0:
            return None
        sr = self._audio_sr
        s = self.bar.in_frac if start is None else start
        i0 = max(0, int(s * self._dur * sr))
        i1 = min(len(arr), int(self.bar.out_frac * self._dur * sr))
        if i1 <= i0:
            return None
        seg = arr[i0:i1]
        g = self._vol_of(self._active)
        if abs(g - 1.0) > 1e-3:
            seg = np.clip(seg.astype(np.float32) * g, -32768, 32767).astype(np.int16)
        seg = np.ascontiguousarray(seg, dtype=np.int16)
        ch = seg.shape[1] if seg.ndim > 1 else 1
        try:
            wf = wave.open(self._seg_wav, 'wb')
            wf.setnchannels(ch); wf.setsampwidth(2); wf.setframerate(sr)
            wf.writeframes(seg.tobytes()); wf.close()
            return self._seg_wav
        except Exception as ex:
            print(f"[editor play] {ex}"); return None

    def _afplay(self, path):
        """用 macOS afplay 播放（CoreAudio，跟随系统当前输出设备=耳机）。"""
        self._kill_afplay()
        try:
            self._play_proc = subprocess.Popen(
                ['afplay', path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as ex:
            print(f"[afplay] {ex}")

    def _kill_afplay(self):
        if self._play_proc and self._play_proc.poll() is None:
            try: self._play_proc.terminate()
            except Exception: pass
        self._play_proc = None

    def _dbg(self, msg):
        try:
            with open('/tmp/shadow_sub_dbg.log', 'a') as f:
                f.write(msg + '\n')
        except Exception:
            pass

    def _restart_audio(self):
        """从当前播放头起播选中音轨（不强制回到起点），对齐墙钟。"""
        start = self.bar.play_frac if self.bar.play_frac is not None else self.bar.in_frac
        start = min(max(start, self.bar.in_frac), self.bar.out_frac)
        if start >= self.bar.out_frac - 1e-4:        # 已到末尾 → 回保留区起点
            start = self.bar.in_frac; self.bar.play_frac = start
        arr = self._audio.get(self._active)
        alen = (len(arr) / self._audio_sr) if (arr is not None and self._audio_sr) else 0
        lastp = self._phrases[-1]['end'] if self._phrases else 0
        self._dbg(f"PLAY active={self._active} _dur={self._dur:.2f} audio_len={alen:.2f} "
                  f"last_phrase_end={lastp:.2f} in={self.bar.in_frac:.3f} out={self.bar.out_frac:.3f} "
                  f"start={start:.3f} nphrases={len(self._phrases)}")
        self._play_anchor = start
        self._seg_dur = max(0.05, (self.bar.out_frac - start) * self._dur)
        self._t0 = time.time()
        path = self._write_seg_wav(start)
        if path:
            self._afplay(path)

    # ── 播放（保留区间内循环；画面跟随音频墙钟，带声音）──────────────────────────
    def _toggle_play(self):
        if self._playing:
            self._stop_play()
        else:
            self._playing = True
            self.play_btn.setIcon(make_icon("pause", ICON_LIGHT, 22))
            self._restart_audio()
            self._play_t.start(33)

    def _stop_play(self):
        if self._playing:
            self._playing = False
            self._play_t.stop()
            self._kill_afplay()
            self.play_btn.setIcon(make_icon("play", ICON_LIGHT, 22))
            self.bar.play_frac = None
            self.bar.update()

    def _play_tick(self):
        elapsed = time.time() - self._t0
        if elapsed >= self._seg_dur:          # 到 out → 回保留区起点循环
            self.bar.play_frac = self.bar.in_frac
            self._restart_audio(); return
        frac = self._play_anchor + (elapsed / self._dur if self._dur else 0.0)
        frac = min(frac, self.bar.out_frac)
        self.bar.play_frac = frac
        self._show(frac)
        self._update_subtitle(frac)
        self.bar.update()

    def _on_seek(self, frac):
        """点/拖轨道定位播放头：预览跳到该帧、字幕同步；在播则从此处接着播。"""
        self._show(frac)
        self._update_subtitle(frac)
        self.bar.update()
        if self._playing:
            self._restart_audio()

    # ── 取消 / 完成 ───────────────────────────────────────────────────────────
    def _cancel(self):
        self._release(); self.hide()
        if self.on_cancel:
            self.on_cancel()

    def _apply(self):
        ti, to = self.bar.in_frac * self._dur, self.bar.out_frac * self._dur
        infrac, outfrac = self.bar.in_frac, self.bar.out_frac
        vmix, vsys, vmic = self._vol_of('mix'), self._vol_of('sys'), self._vol_of('mic')
        subs = self._subtitle_export()
        self._release(); self.hide()
        if self.on_apply:
            self.on_apply(ti, to, infrac, outfrac, vmix, vsys, vmic, subs)

    def _subtitle_export(self):
        """把识别出的字幕逐句渲染成 PNG，并按预览里字幕浮层的位置换算成成片坐标(720×composed_h)，
        供保存时 overlay 烧录（所见即所得：你在预览拖到哪，成片就烧到哪）。无字幕返回 None。"""
        if not self._phrases:
            return None
        playw = OUT_W
        playh = self._composed_h or OUT_H
        pw, ph = self.preview.width(), self.preview.height()
        scale = min(pw / playw, ph / playh) or 1.0          # KeepAspectRatio 实际缩放
        offx, offy = (pw - playw * scale) / 2, (ph - playh * scale) / 2   # 黑边偏移
        sb = self.sub
        cx = (sb.x() + sb.width() / 2 - offx) / scale       # 字幕浮层中心 → 成片坐标
        cy = (sb.y() + sb.height() / 2 - offy) / scale
        try:
            fpx = QFontMetrics(sb.font()).height()
        except Exception:
            fpx = 18
        font_px = max(20, fpx / scale)                      # 预览字号 → 成片字号
        maxw = int(playw * 0.86)
        items = []
        for p in self._phrases:
            if p['end'] <= p['start']:
                continue
            r = _render_sub_png(p.get('text', ''), font_px, maxw)
            if not r:
                continue
            png, w, h = r
            x = max(0, min(int(round(cx - w / 2)), playw - w))
            y = max(0, min(int(round(cy - h / 2)), playh - h))
            items.append({'png': png, 'x': x, 'y': y,
                          'start': float(p['start']), 'end': float(p['end'])})
        return {'items': items, 'playw': playw, 'playh': playh} if items else None

    def _release(self):
        self._stop_play()
        self._wave_t.stop(); self._rec_t.stop(); self._rec_busy = False
        self._audio_wav = {}
        for attr in ('_cap_scr', '_cap_cam'):
            cap = getattr(self, attr, None)
            if cap is not None:
                try: cap.release()
                except Exception: pass
                setattr(self, attr, None)
        self._audio = {}
        for t in self._audio_tmps + [self._seg_wav]:
            if t and os.path.exists(t):
                try: os.remove(t)
                except Exception: pass
        self._audio_tmps = []

    # ── 拖动整个面板（落在子控件外的区域）──────────────────────────────────────
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._dragging = True; self._drag_gp = e.globalPosition().toPoint(); self._drag_wp = self.pos()

    def mouseMoveEvent(self, e):
        if self._dragging and e.buttons() == Qt.MouseButton.LeftButton:
            self.move(self._drag_wp + (e.globalPosition().toPoint() - self._drag_gp))

    def mouseReleaseEvent(self, e):
        self._dragging = False

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(255, 255, 255, 26), 1))
        p.setBrush(QColor(28, 28, 30, 245))
        p.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 16, 16)
        p.setPen(Qt.PenStyle.NoPen)
        # 时间轴行磨砂底
        ty = self.PAD + self.PV_H + 18
        p.setBrush(QColor(255, 255, 255, 12))
        p.drawRoundedRect(QRectF(self.PAD - 6, ty - 8, self.W - 2 * self.PAD + 12, 88), 12, 12)
        # 参数面板：左右两个圆角子面板
        half = (self.W - 2 * self.PAD - 16) // 2
        p.drawRoundedRect(QRectF(self.PAD, self._py, half, self.PANEL_H), 12, 12)
        p.drawRoundedRect(QRectF(self.PAD + half + 16, self._py, half, self.PANEL_H), 12, 12)
        p.end()

    def closeEvent(self, e):
        self._release(); e.accept()


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

        # 控制条已独立成顶层窗口 ControlBar（main 注入 self.ctrl），不再嵌在摄像区里。
        # 状态机仍在本窗口，按阶段调 self.ctrl.set_stage(...) 驱动控制条。
        self.ctrl     = None
        self.editor   = None   # main 注入：裁切编辑页 EditorPanel
        self.on_close = None   # main 注入：收起回菜单栏（而非退出 app）
        self._trim_in  = 0.0   # 裁切保留区间（秒，_held_scr_video 时间轴）
        self._trim_out = None  # None = 不裁尾
        self._trim_in_frac, self._trim_out_frac = 0.0, 1.0
        self._vol_mix = self._vol_sys = self._vol_mic = 1.0   # 三轨音量（编辑页设）

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
    def _stage(self) -> str:
        """当前产品阶段，驱动 ControlBar 显示哪组图标。"""
        if self._reviewing: return 'review'
        if self._stopping:  return 'stopping'
        if self._counting:  return 'countdown'
        if self.recording:  return 'recording'
        return 'ready'

    def _layout(self):
        ch = self._ch
        self.cam_label.setGeometry(1, 1, WIN_W - 2, ch - 2)
        self.count_label.setGeometry(0, 0, WIN_W, ch)
        self.status_label.setGeometry(0, ch - 72, WIN_W, 18)
        self.rec_time_label.setGeometry(WIN_W - 104, 8, 96, 28)
        # 控制条改由独立 ControlBar 承载，按当前阶段驱动其图标/位置
        if self.ctrl:
            self.ctrl.set_stage(self._stage())

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
        self.status_label.setText("")
        self._layout()                               # → set_stage('countdown')：✕ 取消
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
        self.status_label.setText("")
        self._layout()                       # → set_stage('ready')：● 录制 + ✕ 关闭

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

        # 麦克风（按设备名解析，规避蓝牙耳机热插拔后索引失效导致的静默丢轨）
        self._rec_mic = AudioRecorder(self.mic_idx, self.mic_name)
        self._rec_mic.start()
        if self._rec_mic.error:
            print(f"[麦克风] 开流失败: {self._rec_mic.error}")

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
            if self.ctrl: self.ctrl.raise_()   # 控制条也浮在遮罩之上
        # 右上角录制计时
        self._rec_last_sec = -1
        self.rec_time_label.setText('<span style="color:#FF3B30;">●</span> 00:00')
        self.rec_time_label.show(); self.rec_time_label.raise_()
        self._layout()                 # → set_stage('recording')：■ 停止
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
        self._layout()                  # → set_stage('stopping')：单个禁用主键
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

            # 麦克风：用户选了设备(有名字或索引)却没拿到 wav ⇒ 采集失败，需提示
            mic_expected = bool(rec_mic and (rec_mic.name or rec_mic.device is not None))
            held_mic_wav = None
            if rec_mic and rec_mic.stop_and_save(tmp_mic):
                held_mic_wav = tmp_mic
            self._mic_failed = mic_expected and not held_mic_wav
            if self._mic_failed:
                print(f"[麦克风] 未采集到音频(error={rec_mic.error if rec_mic else None}) "
                      f"→ 本条仅系统音单轨")

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
        self.status_label.setText("")
        self._layout()                  # → set_stage('ready')：主键恢复可点

    def _enter_review(self):
        """显示"重录 / 保存"操作区，等待用户决策。"""
        self._stopping  = False
        self._reviewing = True
        self._trim_in,  self._trim_out          = 0.0, None     # 新录制：裁切区间归零
        self._trim_in_frac, self._trim_out_frac = 0.0, 1.0
        self._vol_mix = self._vol_sys = self._vol_mic = 1.0
        self._subtitles = None                                  # 新录制：清掉上条的字幕
        # 保存按钮延迟启用：防止按 STOP 时鼠标未松开、切换后 mouseRelease 误触"保存"
        self._layout()                  # → set_stage('review')：↺ 重录 + ✂ 裁切 + ✓ 保存
        if getattr(self, '_mic_failed', False):
            self.status_label.setText("⚠️ 未采集到麦克风，仅系统音")
            self.status_label.setStyleSheet(
                "color:#FFB020;font-size:10px;background:transparent;")
        else:
            self.status_label.setText("")
        self.update()
        QTimer.singleShot(400, self._enable_save_btn)

    def _enable_save_btn(self):
        if self._reviewing and self.ctrl:
            self.ctrl.set_primary_enabled(True)

    def _exit_review(self):
        """退出 review 状态，恢复 REC 按钮。"""
        self._reviewing = False
        self._layout()                  # → set_stage('ready')：● 录制 + ✕ 关闭
        self.update()

    def _set_frames_visible(self, on: bool):
        """编辑时把录屏区+摄像区+控制条收起，桌面只留居中编辑页；编辑完再展开。"""
        if on:
            self.screen_win.show(); self.screen_win.raise_()
            self.show(); self.raise_()
            if self.ctrl: self.ctrl.show(); self.ctrl.raise_()
        else:
            self.screen_win.hide()
            self.hide()
            if self.ctrl: self.ctrl.hide()

    def _open_editor(self):
        """裁切：录屏区+摄像区两段丢进居中编辑页设首尾；编辑时隐藏两个录制框。"""
        if not (self.editor and self._held_scr_video and os.path.exists(self._held_scr_video)):
            return
        self._set_frames_visible(False)
        self.editor.open_video(self._held_scr_video, self._tmp_video,
                               self._held_sys_wav, self._held_mic_wav, self._av_offset,
                               self._trim_in_frac, self._trim_out_frac,
                               (self._vol_mix, self._vol_sys, self._vol_mic))

    def _apply_trim(self, in_sec, out_sec, in_frac, out_frac,
                    vol_mix=1.0, vol_sys=1.0, vol_mic=1.0, subtitles=None):
        """编辑页「完成」回调：记下裁切区间 + 三轨音量 + 识别字幕（保存时烧录）；展开回录制框。"""
        self._trim_in,  self._trim_out          = in_sec, out_sec
        self._trim_in_frac, self._trim_out_frac = in_frac, out_frac
        self._vol_mix, self._vol_sys, self._vol_mic = vol_mix, vol_sys, vol_mic
        self._subtitles = subtitles
        self._set_frames_visible(True)
        trimmed = in_frac > 0.001 or out_frac < 0.999
        self.status_label.setText("已设裁切区间" if trimmed else "未裁切")
        self.status_label.setStyleSheet("color:#aaa;font-size:10px;background:transparent;")

    def _cancel_edit(self):
        """编辑页「取消」回调：放弃本次编辑（不改裁切区间），展开回录制框。"""
        self._set_frames_visible(True)

    def _do_rerecord(self):
        """丢弃本次录制，直接准备重录。"""
        if self.editor:
            self.editor._release(); self.editor.hide()
        self._set_frames_visible(True)
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
        self._trim_in,  self._trim_out          = 0.0, None
        self._trim_in_frac, self._trim_out_frac = 0.0, 1.0
        self._vol_mix = self._vol_sys = self._vol_mic = 1.0
        self._subtitles = None

    def _do_save(self):
        if self.ctrl:
            self.ctrl.set_primary_enabled(False)
            self.ctrl.set_rerecord_enabled(False)
        self._save_t0 = time.time()
        self.status_label.setText("⏳ 正在保存… 0s")
        self.status_label.setStyleSheet("color:#888;font-size:10px;background:transparent;")
        self._save_progress_timer = QTimer(self)
        self._save_progress_timer.timeout.connect(self._update_save_progress)
        self._save_progress_timer.start(1000)
        merge_and_save(self._held_scr_video, self._tmp_video,
                       self._held_sys_wav, self._held_mic_wav,
                       self._final_out, self._merge_sig, self._video_scale,
                       self._av_offset, self._trim_in, self._trim_out,
                       self._vol_mix, self._vol_sys, self._vol_mic,
                       getattr(self, '_subtitles', None))

    def _update_save_progress(self):
        elapsed = int(time.time() - self._save_t0)
        self.status_label.setText(f"⏳ 正在保存… {elapsed}s")

    def _on_merge_done(self, ok: bool, path: str):
        if hasattr(self, '_save_progress_timer'):
            self._save_progress_timer.stop()
            self._save_progress_timer = None
        if self.ctrl:
            self.ctrl.set_rerecord_enabled(True)
            self.ctrl.set_primary_enabled(True)   # 保存完成后恢复可点击
        self._exit_review()
        if ok:
            print(f"[保存] 视频: {path}")
            self.status_label.setText("✓ 已保存至 screentest/")
            self.status_label.setStyleSheet("color:#44cc88;font-size:10px;background:transparent;")
        else:
            self.status_label.setText("⚠ 合并失败，检查 ffmpeg")
            self.status_label.setStyleSheet("color:#ff6644;font-size:10px;background:transparent;")

    # ── 帧循环 ────────────────────────────────────────────────────────────
    def _tick(self):
        if self.cap is None:      # 空闲态（已收起到菜单栏），无摄像头
            return
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
    def _keep_ctrl_on_top(self):
        # 点击摄像区会被系统抬到最前、盖住控制条;延后一拍(系统抬完后)把控制条重新置顶,
        # 消除"点一下控制条闪现"的 z 轴竞争。
        if self.ctrl and self.ctrl.isVisible():
            QTimer.singleShot(0, self.ctrl.raise_)

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
        self._keep_ctrl_on_top()

    def mouseReleaseEvent(self, e):
        self._keep_ctrl_on_top()

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
            if self.ctrl:
                self.ctrl.follow(); self.ctrl.raise_()   # 贴摄像区底部跟随 + 保持在画面之上

    # ── 菜单栏常驻：空闲/激活切换 ───────────────────────────────────────────
    def enter_idle(self):
        """收起回菜单栏：停预览、释放摄像头（灭绿灯），省资源。
        录制/倒计时/待保存等活动会话期间拒绝空闲，保护录制不丢。"""
        if self.recording or self._counting or self._reviewing:
            return False
        self.timer.stop()
        if self.cap is not None:
            try: self.cap.release()
            except Exception: pass
            self.cap = None
        self.hide()
        return True

    def enter_active(self):
        """从菜单栏召唤：重开摄像头、恢复预览。"""
        if self.cap is None:
            self.cap = cv2.VideoCapture(0)
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
            self._cam_fail_count = 0
        if not self.timer.isActive():
            self.timer.start(33)

    def closeEvent(self, e):
        self.timer.stop()
        if self.recording:
            self._stop_and_hold()
        if self.fog_overlay:
            self.fog_overlay.hide()
        self._discard_temp()
        if self.cap is not None and self.cap.isOpened():
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

    # 独立控制条（横排 icon 模块，脱离录制区、可拖动）
    ctrl_bar = ControlBar()
    camera_win.ctrl = ctrl_bar
    ctrl_bar.on_primary  = camera_win._btn_clicked
    ctrl_bar.on_rerecord = camera_win._do_rerecord
    ctrl_bar.on_close    = lambda: camera_win.on_close and camera_win.on_close()

    def _cam_rect():
        """摄像区窗口的全局矩形（控制条贴其底边、水平居中）。"""
        return QRect(camera_win.x(), camera_win.y(),
                     camera_win.width(), camera_win.height())
    ctrl_bar.anchor_fn  = _cam_rect

    # 裁切编辑页（桌面正中弹出）
    editor_panel = EditorPanel()
    camera_win.editor        = editor_panel
    ctrl_bar.on_trim         = camera_win._open_editor
    editor_panel.on_apply    = camera_win._apply_trim
    editor_panel.on_cancel   = camera_win._cancel_edit

    # 高度联动 — 改完高度后把摄像区移到录屏区正下方，避免独立窗口位置不同步导致重叠/分离
    def _relayout_cam_below_scr():
        sp = screen_win.pos()
        # screen_win._ch 是 set_content_h 里已同步更新的值，
        # 直接用它算偏移，避免 macOS 下 resize() 后 height() 返回旧值导致位置偏差
        camera_win.move(sp.x(), sp.y() + screen_win._ch + TOPBAR + HANDLE + 6)
        if camera_win.ctrl: camera_win.ctrl.follow()   # 控制条贴摄像区底部跟随

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
            # 缩略态展开 → 整体居中（无论从哪个入口展开：点胶囊/折叠键），
            # 防止之前把摄像区拖到屏幕外后展开仍够不到关闭按钮。
            camera_win.show()
            _center_session()

    screen_win.on_collapse_changed = _on_collapse

    # ── 整体 UI 居中 ────────────────────────────────────────────────────────
    def _center_session():
        """把录屏区+摄像区整体摆到屏幕正中（修复被拖出屏幕够不到关闭按钮）。"""
        sc = app.primaryScreen().geometry()
        total_h = (screen_win._ch + TOPBAR + HANDLE) + 6 + \
                  (camera_win._ch + TOPBAR + HANDLE)
        cx = sc.x() + sc.width() // 2 - WIN_W // 2
        cy = sc.y() + max(0, (sc.height() - total_h) // 2)
        screen_win.move(cx, cy)
        _relayout_cam_below_scr()        # 用 _ch 把摄像区贴到录屏区正下方
        ctrl_bar.follow()                # 居中时把控制条（重新）摆到右中初始位

    # ── 菜单栏常驻：召唤 / 收起 ──────────────────────────────────────────────
    def summon():
        """万能复位：无论当前是缩略态/驻留态/被拖出屏幕，一律回到
        「就绪态 + 屏幕正中」——两区展开、摄像头开、整体居中、置顶。"""
        if screen_win._collapsed:        # 缩略态 → 先展开（联动唤回摄像区）
            screen_win._toggle_collapse()
        camera_win.enter_active()        # 摄像头开 + 预览
        ctrl_bar.reset_detach()          # 召唤一律复位控制条锚定（清除用户拖动态）
        _center_session()                # 整体居中
        screen_win.show(); screen_win.raise_(); screen_win.activateWindow()
        camera_win.show(); camera_win.raise_(); camera_win.activateWindow()
        ctrl_bar.set_stage(camera_win._stage())
        ctrl_bar.show(); ctrl_bar.raise_()

    def collapse():
        """关闭按钮：收起回菜单栏（录制/待保存期间拒绝，保护录制）。"""
        if not camera_win.enter_idle():
            return
        screen_win.hide()
        fog_overlay.hide()
        ctrl_bar.hide()

    camera_win.on_close = collapse

    # 关掉浮窗不退出 app —— 常驻菜单栏，只有 Quit 才真退出
    app.setQuitOnLastWindowClosed(False)

    # ── 菜单栏图标 ──────────────────────────────────────────────────────────
    # 用 App 图标（两只企鹅）的单色剪影作模板图标（mask）：macOS 会按深/浅色
    # 菜单栏自动反色，和旁边那排系统单色图标统一风格，而不是塞个橙色 App 方块。
    def _glyph_path():
        base = getattr(sys, '_MEIPASS', None)
        if base:                                   # 打包版：bundle 根目录
            p = os.path.join(base, 'penguins_glyph.png')
            if os.path.exists(p): return p
        # 源码版：项目内 ui_design/
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         'ui_design', 'penguins_glyph.png')
        return p if os.path.exists(p) else None

    def _make_tray_icon():
        gp = _glyph_path()
        if gp:
            ic = QIcon(gp)
            if not ic.isNull():
                ic.setIsMask(True)   # 作为 macOS 模板图标自动适配菜单栏
                return ic
        # 兜底：画个圆点，确保菜单栏一定有图标
        pm = QPixmap(22, 22); pm.fill(Qt.GlobalColor.transparent)
        pr = QPainter(pm); pr.setRenderHint(QPainter.RenderHint.Antialiasing)
        pr.setPen(Qt.PenStyle.NoPen); pr.setBrush(QColor(0, 0, 0))
        pr.drawEllipse(5, 5, 12, 12); pr.end()
        ic = QIcon(pm); ic.setIsMask(True)
        return ic

    tray_icon = _make_tray_icon()

    tray = QSystemTrayIcon(tray_icon, app)
    tray.setToolTip("Shadow")
    menu = QMenu()
    act_rec = menu.addAction("打开 Shadow")
    act_rec.triggered.connect(summon)
    menu.addSeparator()
    act_quit = menu.addAction("退出  Quit")
    act_quit.triggered.connect(app.quit)
    tray.setContextMenu(menu)
    # 点击图标只弹出菜单（不直接展开）；展开由「打开 Shadow」触发
    tray.show()

    # 启动即展开到「就绪态 + 屏幕居中」，并通知用户可以使用了；
    # 菜单栏企鹅图标同时常驻，方便之后多次召唤录制。
    summon()
    tray.showMessage("Shadow 已就绪",
                     "可以开始录制了 — 之后点菜单栏企鹅图标 →「打开 Shadow」召唤",
                     tray_icon, 4000)

    sys.exit(app.exec())
