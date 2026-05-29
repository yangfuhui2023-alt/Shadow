"""
eval_accuracy.py — 字幕识别准确率评测
流程：
  1. 录制 YouTube Shorts 视频（Chrome 前台）+ 运行字幕识别
  2. 从视频帧中提取 YouTube 烧录字幕（Apple Vision OCR）→ Ground Truth
  3. 与 ASR 识别结果对比，计算 WER / CER / 精确率 / 召回率
输出：eval_report_时间戳.txt
"""
import subprocess, threading, cv2, numpy as np, os, sys, time, re
from datetime import datetime

SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
SUBTITLE_BIN = os.path.join(SCRIPT_DIR, "subtitle_recognizer")
OCR_BIN      = os.path.join(SCRIPT_DIR, "ocr_frame")

VIDEO_REGION = {"left": 60, "top": 35, "width": 390, "height": 845}
OUT_W, OUT_H = 405, 720
FPS          = 25
DURATION     = int(sys.argv[1]) if len(sys.argv) > 1 else 45
LANG         = sys.argv[2] if len(sys.argv) > 2 else "en-US"

# ── 字幕收集 ──────────────────────────────────────────────────────────────────
_asr_events = []   # [(rel_sec, text)]
_asr_lock   = threading.Lock()
_rec_t0     = 0.0

def _read_subtitles(proc):
    for line in proc.stdout:
        text = line.strip()
        if text:
            rel = time.time() - _rec_t0
            with _asr_lock:
                _asr_events.append((rel, text))
            print(f"  💬 [{rel:5.1f}s] {text}")


# ── WER 计算 ─────────────────────────────────────────────────────────────────
def _edit_distance(a, b):
    m, n = len(a), len(b)
    dp = list(range(n + 1))
    for i in range(1, m + 1):
        prev, dp[0] = dp[0], i
        for j in range(1, n + 1):
            temp = dp[j]
            dp[j] = prev if a[i-1] == b[j-1] else 1 + min(prev, dp[j], dp[j-1])
            prev = temp
    return dp[n]

def _normalise(text):
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s']", "", text)
    return text.split()

def wer(reference: str, hypothesis: str) -> dict:
    ref = _normalise(reference)
    hyp = _normalise(hypothesis)
    if not ref:
        return {"wer": None, "cer": None, "ref_words": 0, "hyp_words": len(hyp)}
    d   = _edit_distance(ref, hyp)
    # CER on characters
    rc  = list(reference.lower())
    hc  = list(hypothesis.lower())
    dc  = _edit_distance(rc, hc)
    return {
        "wer":       round(d / len(ref) * 100, 1),
        "cer":       round(dc / max(len(rc), 1) * 100, 1),
        "ref_words": len(ref),
        "hyp_words": len(hyp),
        "edit_ops":  d,
    }


# ── OCR Ground Truth 提取 ────────────────────────────────────────────────────
def ocr_image(path: str) -> str:
    r = subprocess.run([OCR_BIN, path], capture_output=True, text=True, timeout=10)
    return r.stdout.strip()

def extract_gt_from_video(video_path: str) -> list:
    """
    每 2 秒抽一帧，裁切 YouTube 烧录字幕区域（视频内容下方 20%），
    用 OCR 读取，去重相邻重复 → [(sec, text), ...]
    """
    cap    = cv2.VideoCapture(video_path)
    total  = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps    = cap.get(cv2.CAP_PROP_FPS) or FPS
    step   = max(1, int(fps * 2))   # 每 2 秒抽一帧
    events = []
    prev   = ""

    # 裁切整个视频内容区（去掉 Chrome tab 栏约 35px，保留到 y=640）
    cap_y1 = 35
    cap_y2 = 640

    # 要过滤掉的 YouTube / Chrome UI 关键词
    UI_NOISE = re.compile(
        r"youtube|bbc|posted on|@|http|\.com|shorts|subscribe|"
        r"^\s*[a-z]\s*$|whatsapp|share|follow|views|likes",
        re.IGNORECASE)

    for fn in range(0, total, step):
        cap.set(cv2.CAP_PROP_POS_FRAMES, fn)
        ok, frame = cap.read()
        if not ok:
            continue
        crop = frame[cap_y1:cap_y2, :]
        ts   = fn / fps

        tmp = f"/tmp/_ocr_{fn}.png"
        cv2.imwrite(tmp, crop)
        text = ocr_image(tmp)
        os.remove(tmp)

        # 按行过滤：保留看起来像语音内容的行
        kept = []
        for line in text.split("\n"):
            line = line.strip().strip("'\"")
            words = re.sub(r"[^a-zA-Z0-9'\s]", "", line).split()
            if len(words) < 2:
                continue
            if UI_NOISE.search(line):
                continue
            kept.append(" ".join(words))

        clean = " ".join(kept).strip()
        if len(clean.split()) < 2:
            continue
        if clean != prev:
            events.append((ts, clean))
            prev = clean

    cap.release()
    return events


# ── 主流程 ────────────────────────────────────────────────────────────────────
def main():
    global _rec_t0
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # 1. 启动字幕识别器
    print(f"[1/5] 启动字幕识别器（{LANG}）...")
    sub_proc = subprocess.Popen(
        [SUBTITLE_BIN, LANG],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1)
    threading.Thread(target=_read_subtitles, args=(sub_proc,), daemon=True).start()
    ready = threading.Event()
    def _se():
        for l in sub_proc.stderr:
            if "READY" in l: ready.set()
            elif "ERROR" in l: print(f"  ✗ {l.strip()}"); ready.set()
    threading.Thread(target=_se, daemon=True).start()
    if not ready.wait(8):
        print("  ✗ 字幕识别器启动超时")
        sub_proc.kill()
        return
    print("  字幕识别器就绪 ✓")

    # 2. 录制视频帧
    tmp_video = f"/tmp/eval_video_{ts}.mp4"
    fourcc    = cv2.VideoWriter_fourcc(*"mp4v")
    writer    = cv2.VideoWriter(tmp_video, fourcc, FPS, (OUT_W, OUT_H))

    print(f"[2/5] 开始录制 {DURATION}s → {OUT_W}×{OUT_H} @ {FPS}fps")
    import mss
    sct         = mss.MSS()
    frame_int   = 1.0 / FPS
    next_t      = time.time()
    _rec_t0     = time.time()
    t0          = _rec_t0
    frame_count = 0

    while time.time() - t0 < DURATION:
        now = time.time()
        if now >= next_t:
            shot  = sct.grab(VIDEO_REGION)
            raw   = np.frombuffer(shot.raw, dtype=np.uint8).reshape(
                        (shot.height, shot.width, 4))
            bgr   = cv2.resize(cv2.cvtColor(raw, cv2.COLOR_BGRA2BGR),
                                (OUT_W, OUT_H), interpolation=cv2.INTER_AREA)
            writer.write(bgr)
            frame_count += 1
            next_t += frame_int
            elapsed = int(time.time() - t0)
            if frame_count % (FPS * 10) == 0:
                print(f"  [{elapsed:>3}s] {frame_count} 帧")
        else:
            time.sleep(0.001)

    writer.release()
    sct.close()
    sub_proc.terminate()
    try: sub_proc.wait(timeout=4)
    except: sub_proc.kill()
    print(f"  录制完成：{frame_count} 帧")

    # 3. 提取 Ground Truth（YouTube 烧录字幕 OCR）
    print("[3/5] 提取 Ground Truth（帧 OCR）...")
    gt_events = extract_gt_from_video(tmp_video)
    print(f"  提取到 {len(gt_events)} 条字幕片段")
    for t, txt in gt_events:
        print(f"    [{t:5.1f}s] {txt}")

    # 4. 整理 ASR 结果（去重相邻重复）
    print("[4/5] 整理 ASR 结果...")
    with _asr_lock:
        raw_asr = list(_asr_events)
    asr_dedup = []
    for t, txt in raw_asr:
        if not asr_dedup or txt != asr_dedup[-1][1]:
            asr_dedup.append((t, txt))
    print(f"  ASR 共 {len(asr_dedup)} 条（原始 {len(raw_asr)} 条）")
    for t, txt in asr_dedup:
        print(f"    [{t:5.1f}s] {txt}")

    # 5. 计算 WER / CER
    print("[5/5] 计算准确率指标...")
    gt_text  = " ".join(txt for _, txt in gt_events)
    asr_text = " ".join(txt for _, txt in asr_dedup)

    if not gt_text:
        print("\n  ⚠ 未提取到 Ground Truth（视频帧中未找到字幕文字）")
        print("  → OCR 可能受 Chrome UI 干扰，或字幕不在预期位置")
        metrics = None
    elif not asr_text:
        print("\n  ⚠ ASR 未产生任何输出（视频是否在播放/有声音？）")
        metrics = None
    else:
        metrics = wer(gt_text, asr_text)

    # 生成报告
    report_path = os.path.join(SCRIPT_DIR, f"eval_report_{ts}.txt")
    lines = [
        f"Shadow Recorder — 字幕准确率评测报告",
        f"{'='*50}",
        f"时间:      {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"视频时长:  {DURATION}s",
        f"识别语言:  {LANG}",
        f"",
        f"── Ground Truth（视频 OCR）──",
    ]
    for t, txt in gt_events:
        lines.append(f"  [{t:5.1f}s] {txt}")
    if not gt_events:
        lines.append("  (未提取到)")
    lines += ["", "── ASR 识别结果 ──"]
    for t, txt in asr_dedup:
        lines.append(f"  [{t:5.1f}s] {txt}")
    if not asr_dedup:
        lines.append("  (无输出)")

    if metrics:
        lines += [
            "",
            "── 量化指标 ──",
            f"  WER  (词错率):   {metrics['wer']}%   ← 越低越好，< 20% 为良好",
            f"  CER  (字符错率): {metrics['cer']}%",
            f"  参考词数:        {metrics['ref_words']}",
            f"  识别词数:        {metrics['hyp_words']}",
            f"  编辑距离:        {metrics['edit_ops']} 步",
            "",
            f"  准确率估算: {max(0, 100 - metrics['wer']):.1f}%",
            "",
            "── 全文对比 ──",
            f"  GT : {gt_text}",
            f"  ASR: {asr_text}",
        ]
    else:
        lines += ["", "── 量化指标 ──", "  无法计算（缺少 GT 或 ASR 输出）"]

    report = "\n".join(lines)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)

    # 转换视频为 H.264
    out_video = os.path.join(SCRIPT_DIR, f"eval_720p_{ts}.mp4")
    subprocess.run(["ffmpeg", "-y", "-i", tmp_video,
                    "-c:v", "libx264", "-preset", "fast", "-crf", "22",
                    "-pix_fmt", "yuv420p", out_video],
                   capture_output=True)
    os.remove(tmp_video)

    print(f"\n{'='*50}")
    if metrics:
        print(f"WER: {metrics['wer']}%  |  CER: {metrics['cer']}%  |  "
              f"准确率约 {max(0, 100-metrics['wer']):.1f}%")
    print(f"报告: {os.path.basename(report_path)}")
    print(f"视频: {os.path.basename(out_video)}")

if __name__ == "__main__":
    main()
