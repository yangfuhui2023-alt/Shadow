"""
record_test.py — 720p / 25fps 字幕录制测试
字幕实时烧录进视频帧，输出 test_720p_时间戳.mp4
"""
import subprocess, threading, cv2, numpy as np
import time, os, sys

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
SUBTITLE_BIN = os.path.join(SCRIPT_DIR, "subtitle_recognizer")
AUDIO_BIN    = os.path.join(SCRIPT_DIR, "audio_capture")

# ── YouTube Shorts 视频区域（基于截图目测，可微调） ────────────────────────────
VIDEO_REGION = {"left": 60,  "top": 35, "width": 390, "height": 845}

# ── 输出参数 ─────────────────────────────────────────────────────────────────
OUT_W     = 405    # 720p Shorts: 9:16 → 405×720
OUT_H     = 720
FPS       = 25
DURATION  = int(sys.argv[1]) if len(sys.argv) > 1 else 60   # 默认录 60 秒
LANG      = sys.argv[2] if len(sys.argv) > 2 else "en-US"


# ── 字幕状态 ──────────────────────────────────────────────────────────────────
_sub_text   = ""
_sub_lock   = threading.Lock()
_sub_time   = 0.0    # 最近一次收到字幕的时间
SUB_LINGER  = 5.0    # 字幕消失前保留秒数

# SRT 条目列表：每条 (start_sec, end_sec, text)
_srt_entries  = []
_srt_lock     = threading.Lock()
_rec_start    = 0.0  # 录制开始时间，用于计算相对时间戳


def _read_subtitles(proc):
    global _sub_text, _sub_time
    prev_text   = ""
    prev_start  = 0.0
    for line in proc.stdout:
        text = line.strip()
        if not text:
            continue
        now = time.time()
        rel = now - _rec_start   # 相对录制起始的秒数
        with _sub_lock:
            _sub_text = text
            _sub_time = now
        # 只记录"最终结果"变化（避免每个局部结果都写一条）
        if text != prev_text:
            if prev_text:
                # 为上一条写入结束时间
                with _srt_lock:
                    _srt_entries.append((prev_start, rel, prev_text))
            prev_text  = text
            prev_start = rel
        print(f"  💬 {text}")


# ── 字幕卡片烧录（OpenCV） ──────────────────────────────────────────────────
def _burn_subtitle(frame: np.ndarray, text: str) -> np.ndarray:
    if not text:
        return frame
    out   = frame.copy()
    font  = cv2.FONT_HERSHEY_SIMPLEX
    scale = 0.52
    thick = 1
    pad_h, pad_v, radius = 14, 9, 10
    max_w = out.shape[1] - 40

    # 自动换行
    lines, cur = [], ""
    for word in text.split():
        test = (cur + " " + word).strip()
        if cv2.getTextSize(test, font, scale, thick)[0][0] > max_w and cur:
            lines.append(cur); cur = word
        else:
            cur = test
    if cur: lines.append(cur)

    lh       = cv2.getTextSize("Ag", font, scale, thick)[0][1] + 8
    card_h   = len(lines) * lh + pad_v * 2
    card_w   = min(max(cv2.getTextSize(l, font, scale, thick)[0][0]
                       for l in lines) + pad_h * 2, out.shape[1] - 20)
    cx       = (out.shape[1] - card_w) // 2
    cy       = out.shape[0] - card_h - 18

    # 半透明圆角背景（用矩形近似）
    overlay = out.copy()
    cv2.rectangle(overlay, (cx, cy), (cx + card_w, cy + card_h), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.78, out, 0.22, 0, out)

    # 文字
    for i, line in enumerate(lines):
        tw = cv2.getTextSize(line, font, scale, thick)[0][0]
        tx = cx + (card_w - tw) // 2
        ty = cy + pad_v + (i + 1) * lh - 3
        cv2.putText(out, line, (tx, ty), font, scale,
                    (255, 255, 255), thick, cv2.LINE_AA)
    return out


# ── 主流程 ────────────────────────────────────────────────────────────────────
def main():
    from datetime import datetime
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # 1. 启动字幕识别
    print(f"[1/4] 启动字幕识别器（语言: {LANG}）...")
    sub_proc = subprocess.Popen(
        [SUBTITLE_BIN, LANG],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1)
    threading.Thread(target=_read_subtitles, args=(sub_proc,), daemon=True).start()
    for line in sub_proc.stderr:
        line = line.strip()
        if "READY" in line:
            print("  字幕识别就绪 ✓")
            break
        elif "ERROR" in line:
            print(f"  ✗ {line}")
            sub_proc = None
            break

    # 2. 启动系统音频录制（超时 8 秒后继续，无音频则仅保存视频）
    tmp_audio  = f"/tmp/test_audio_{ts}.caf"
    audio_proc = None
    print("[2/4] 启动音频录制...")
    _ap = subprocess.Popen(
        [AUDIO_BIN, tmp_audio],
        stderr=subprocess.PIPE, text=True)
    _audio_ready = threading.Event()

    def _wait_audio():
        for line in _ap.stderr:
            if "READY" in line:
                _audio_ready.set(); return
            elif "ERROR" in line:
                print(f"  ✗ {line.strip()}"); return
    threading.Thread(target=_wait_audio, daemon=True).start()
    if _audio_ready.wait(timeout=8):
        audio_proc = _ap
        print("  音频录制就绪 ✓")
    else:
        print("  ⚠ 音频超时，继续纯视频录制")
        _ap.terminate()

    # 3. 视频帧录制
    tmp_video = f"/tmp/test_video_{ts}.mp4"
    fourcc    = cv2.VideoWriter_fourcc(*"mp4v")
    writer    = cv2.VideoWriter(tmp_video, fourcc, FPS, (OUT_W, OUT_H))

    print(f"[3/4] 开始录制 {DURATION} 秒  →  {OUT_W}×{OUT_H} @ {FPS}fps")
    print(f"      捕获区域: {VIDEO_REGION}")

    import mss
    global _rec_start
    sct           = mss.MSS()
    frame_int     = 1.0 / FPS
    next_t        = time.time()
    t0            = time.time()
    _rec_start    = t0
    frame_count   = 0

    while time.time() - t0 < DURATION:
        now = time.time()
        if now >= next_t:
            shot = sct.grab(VIDEO_REGION)
            raw  = np.frombuffer(shot.raw, dtype=np.uint8).reshape(
                       (shot.height, shot.width, 4))
            bgr  = cv2.resize(cv2.cvtColor(raw, cv2.COLOR_BGRA2BGR),
                               (OUT_W, OUT_H), interpolation=cv2.INTER_AREA)

            # 字幕烧录
            with _sub_lock:
                sub  = _sub_text if (time.time() - _sub_time) < SUB_LINGER else ""
            if sub:
                bgr = _burn_subtitle(bgr, sub)

            writer.write(bgr)
            frame_count += 1
            next_t += frame_int

            elapsed = int(time.time() - t0)
            if frame_count % (FPS * 5) == 0:
                print(f"  [{elapsed:>3}s] {frame_count} 帧已录制")
        else:
            time.sleep(0.001)

    print(f"  录制完成：共 {frame_count} 帧")
    writer.release()
    sct.close()

    # 停止子进程
    duration_sec = time.time() - t0
    for p in [sub_proc, audio_proc]:
        if p:
            p.terminate()
            try: p.wait(timeout=5)
            except: p.kill()

    # 收尾：把最后一条字幕的结束时间补上
    with _srt_lock:
        if _srt_entries or _sub_text:
            if _sub_text:
                last_start = _sub_time - _rec_start
                _srt_entries.append((last_start, duration_sec, _sub_text))

    # 写 SRT 字幕文件
    srt_file = os.path.join(SCRIPT_DIR, f"test_720p_{ts}.srt")
    with _srt_lock:
        entries = list(_srt_entries)

    def _fmt(sec):
        h  = int(sec // 3600)
        m  = int((sec % 3600) // 60)
        s  = int(sec % 60)
        ms = int((sec - int(sec)) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    with open(srt_file, "w", encoding="utf-8") as f:
        for idx, (s, e, text) in enumerate(entries, 1):
            e = max(e, s + 0.5)   # 最短 0.5 秒
            f.write(f"{idx}\n{_fmt(s)} --> {_fmt(e)}\n{text}\n\n")
    if entries:
        print(f"  字幕文件: {os.path.basename(srt_file)}  ({len(entries)} 条)")
    else:
        print("  字幕文件: 无识别结果（视频未播放音频？）")

    # 4. 合并音视频（无音频时直接复制视频）
    out_file  = os.path.join(SCRIPT_DIR, f"test_720p_{ts}.mp4")
    has_audio = audio_proc is not None and os.path.exists(tmp_audio) and \
                os.path.getsize(tmp_audio) > 1024
    print(f"[4/4] {'合并音视频' if has_audio else '导出视频（无音频）'} → {os.path.basename(out_file)}")
    encode = ["-c:v", "libx264", "-preset", "fast", "-crf", "22", "-pix_fmt", "yuv420p"]
    if has_audio:
        cmd = ["ffmpeg", "-y", "-i", tmp_video, "-i", tmp_audio,
               "-map", "0:v", "-map", "1:a"] + encode + \
              ["-c:a", "aac", "-metadata:s:a:0", "title=System Audio", out_file]
    else:
        cmd = ["ffmpeg", "-y", "-i", tmp_video] + encode + [out_file]
    r = subprocess.run(cmd, capture_output=True, text=True)
    for f in [tmp_video, tmp_audio]:
        try: os.remove(f)
        except: pass
    if r.returncode == 0:
        size_mb = os.path.getsize(out_file) / 1024 / 1024
        print(f"\n✓ 完成！{os.path.basename(out_file)}  ({size_mb:.1f} MB)")
    else:
        print(f"✗ ffmpeg 错误:\n{r.stderr[-500:]}")


if __name__ == "__main__":
    main()
