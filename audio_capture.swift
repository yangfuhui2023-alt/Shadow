// audio_capture — 录制系统音频（ScreenCaptureKit，macOS 12.3+）
// 常驻 daemon 模式：从 stdin 读取指令，复用同一进程，
// 屏幕录制授权每个进程生命周期内最多触发一次（避免每次重录都弹授权）。
//
// 指令（stdin，每行一条）:
//   START <output.caf>   开始录制到指定文件；就绪后 stderr 输出 READY
//   STOP                 停止当前录制并写盘；完成后 stderr 输出 SAVED
//   QUIT                 停止并退出进程
// stdin EOF（父进程退出）或 SIGTERM/SIGINT 同样优雅退出。

import ScreenCaptureKit
import AVFoundation
import Foundation

// ── 音频写入器（每次录制新建一个实例）────────────────────────────────────────
final class AudioWriter: @unchecked Sendable {
    private let writer:    AVAssetWriter
    private let input:     AVAssetWriterInput
    private let writeQ  =  DispatchQueue(label: "aw.write")
    private var started    = false

    init(url: URL) throws {
        try? FileManager.default.removeItem(at: url)
        writer = try AVAssetWriter(outputURL: url, fileType: .caf)
        let fmt: [String: Any] = [
            AVFormatIDKey:                  Int(kAudioFormatLinearPCM),
            AVSampleRateKey:                44100,
            AVNumberOfChannelsKey:          2,
            AVLinearPCMBitDepthKey:         16,
            AVLinearPCMIsFloatKey:          false,
            AVLinearPCMIsBigEndianKey:      false,
            AVLinearPCMIsNonInterleaved:    false
        ]
        input = AVAssetWriterInput(mediaType: .audio, outputSettings: fmt)
        input.expectsMediaDataInRealTime = true
        writer.add(input)
    }

    func append(_ buf: CMSampleBuffer) {
        writeQ.async { [self] in
            if !started {
                writer.startWriting()
                // 以首帧 PTS 作为会话起点：跨多次录制复用进程时，时间戳更稳，
                // 输出文件从 0 开始，避免前导静音/偏移。
                writer.startSession(atSourceTime: CMSampleBufferGetPresentationTimeStamp(buf))
                started = true
            }
            if input.isReadyForMoreMediaData { input.append(buf) }
        }
    }

    func finalize(_ completion: @Sendable @escaping () -> Void) {
        writeQ.async { [self] in
            guard started else { completion(); return }
            input.markAsFinished()
            writer.finishWriting { completion() }
        }
    }
}

// ── SCStream 输出代理 ─────────────────────────────────────────────────────────
final class Output: NSObject, SCStreamOutput, SCStreamDelegate, @unchecked Sendable {
    let audioWriter: AudioWriter
    init(_ aw: AudioWriter) { audioWriter = aw }

    func stream(_ stream: SCStream,
                didOutputSampleBuffer buf: CMSampleBuffer,
                of type: SCStreamOutputType) {
        guard type == .audio else { return }
        audioWriter.append(buf)
    }
    func stream(_ stream: SCStream, didStopWithError error: any Error) {
        fputs("stream stopped: \(error)\n", stderr)
    }
}

// ── 录制控制器（常驻，串行队列保护状态）──────────────────────────────────────
final class Recorder: @unchecked Sendable {
    private let q = DispatchQueue(label: "recorder.control")
    private var scStream: SCStream?
    private var writer:   AudioWriter?
    private var outObj:   Output?

    func start(_ path: String) {
        q.async { [self] in
            guard scStream == nil else {
                fputs("ERROR: already recording\n", stderr); return
            }
            let aw: AudioWriter
            do { aw = try AudioWriter(url: URL(fileURLWithPath: path)) }
            catch { fputs("ERROR: cannot create output file\n", stderr); return }
            let obj = Output(aw)
            writer = aw
            outObj = obj

            SCShareableContent.getWithCompletionHandler { content, error in
                self.q.async {
                    guard let content, let display = content.displays.first else {
                        fputs("ERROR: SCShareableContent: \(error?.localizedDescription ?? "unknown")\n", stderr)
                        fputs("TIP: grant Screen Recording permission in System Settings → Privacy\n", stderr)
                        self.writer = nil; self.outObj = nil
                        return
                    }

                    let cfg = SCStreamConfiguration()
                    cfg.capturesAudio               = true
                    cfg.excludesCurrentProcessAudio = true
                    cfg.width  = 2   // 只需音频，最小视频分辨率省资源
                    cfg.height = 2

                    let filter = SCContentFilter(display: display,
                                                 excludingApplications: [],
                                                 exceptingWindows: [])
                    let stream = SCStream(filter: filter, configuration: cfg, delegate: obj)
                    do {
                        try stream.addStreamOutput(obj, type: .audio,
                                                   sampleHandlerQueue: DispatchQueue(label: "sc.audio"))
                    } catch {
                        fputs("ERROR: addStreamOutput: \(error)\n", stderr)
                        self.writer = nil; self.outObj = nil
                        return
                    }
                    self.scStream = stream
                    stream.startCapture { error in
                        if let error {
                            fputs("ERROR: startCapture: \(error)\n", stderr)
                            self.q.async { self.scStream = nil; self.writer = nil; self.outObj = nil }
                            return
                        }
                        fputs("READY\n", stderr)
                    }
                }
            }
        }
    }

    func stop() {
        q.async { [self] in
            guard let stream = scStream, let aw = writer else {
                fputs("SAVED\n", stderr); return   // 无录制可停，直接确认
            }
            scStream = nil   // 先清，防止并发 stop
            stream.stopCapture { _ in
                aw.finalize {
                    fputs("SAVED\n", stderr)
                    self.q.async { self.writer = nil; self.outObj = nil }
                }
            }
        }
    }
}

// ── 屏幕画面录制器（ScreenCaptureKit 视频）──────────────────────────────────────
// 抓取指定矩形的屏幕画面写 H.264。content filter 排除本进程的全部窗口，
// 因此 Shadow 自己的 UI（ghost 框、雾遮罩、按钮）和系统录屏红框都不会进画面，
// 从根本上避免「截图式抓屏把系统叠加层一起录进去」的问题。
final class VideoRecorder: NSObject, SCStreamOutput, SCStreamDelegate, @unchecked Sendable {
    private let q = DispatchQueue(label: "vrecorder")   // 控制 + 帧写入串行同队列
    private var scStream: SCStream?
    private var writer:   AVAssetWriter?
    private var input:    AVAssetWriterInput?
    private var adaptor:  AVAssetWriterInputPixelBufferAdaptor?
    private var started = false
    private var cfg:      SCStreamConfiguration?   // 留存以便录制中动态改 sourceRect
    private var dispOrigin = CGPoint.zero          // 当前显示器原点（点坐标）

    func start(path: String, x: Double, y: Double, w: Double, h: Double, outW: Int, outH: Int) {
        q.async { [self] in
            guard scStream == nil else { fputs("ERROR: video already recording\n", stderr); return }
            let url = URL(fileURLWithPath: path)
            try? FileManager.default.removeItem(at: url)
            let aw: AVAssetWriter
            do { aw = try AVAssetWriter(outputURL: url, fileType: .mp4) }
            catch { fputs("ERROR: cannot create video file\n", stderr); return }
            let settings: [String: Any] = [
                AVVideoCodecKey:  AVVideoCodecType.h264,
                AVVideoWidthKey:  outW,
                AVVideoHeightKey: outH,
                AVVideoCompressionPropertiesKey: [
                    AVVideoAverageBitRateKey:      outW * outH * 8,
                    AVVideoMaxKeyFrameIntervalKey: 60
                ]
            ]
            let vinput = AVAssetWriterInput(mediaType: .video, outputSettings: settings)
            vinput.expectsMediaDataInRealTime = true
            let adp = AVAssetWriterInputPixelBufferAdaptor(
                assetWriterInput: vinput,
                sourcePixelBufferAttributes: [
                    kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
                    kCVPixelBufferWidthKey  as String: outW,
                    kCVPixelBufferHeightKey as String: outH
                ])
            guard aw.canAdd(vinput) else { fputs("ERROR: cannot add video input\n", stderr); return }
            aw.add(vinput)
            writer = aw; input = vinput; adaptor = adp; started = false

            SCShareableContent.getWithCompletionHandler { content, error in
                self.q.async {
                    guard let content, !content.displays.isEmpty else {
                        fputs("ERROR: video SCShareableContent: \(error?.localizedDescription ?? "unknown")\n", stderr)
                        self.writer = nil; self.input = nil; self.adaptor = nil; return
                    }
                    // 选包含捕获矩形中心的显示器（多屏时不至于抓错屏），退化到首个
                    let cx = x + w / 2, cy = y + h / 2
                    let display = content.displays.first(where: { $0.frame.contains(CGPoint(x: cx, y: cy)) })
                                  ?? content.displays.first!
                    let myApps = content.applications.filter { $0.processID == getpid() }
                    let cfg = SCStreamConfiguration()
                    cfg.width  = outW
                    cfg.height = outH
                    cfg.sourceRect = CGRect(x: x - display.frame.origin.x,
                                            y: y - display.frame.origin.y, width: w, height: h)
                    cfg.scalesToFit          = true
                    cfg.showsCursor          = true
                    cfg.pixelFormat          = kCVPixelFormatType_32BGRA
                    cfg.minimumFrameInterval = CMTime(value: 1, timescale: 30)
                    cfg.queueDepth           = 6
                    self.cfg = cfg
                    self.dispOrigin = display.frame.origin
                    let filter = SCContentFilter(display: display,
                                                 excludingApplications: myApps, exceptingWindows: [])
                    let stream = SCStream(filter: filter, configuration: cfg, delegate: self)
                    do {
                        try stream.addStreamOutput(self, type: .screen, sampleHandlerQueue: self.q)
                    } catch {
                        fputs("ERROR: video addStreamOutput: \(error)\n", stderr)
                        self.writer = nil; self.input = nil; self.adaptor = nil; return
                    }
                    self.scStream = stream
                    stream.startCapture { error in
                        if let error {
                            fputs("ERROR: video startCapture: \(error)\n", stderr)
                            self.q.async { self.scStream = nil; self.writer = nil
                                           self.input = nil; self.adaptor = nil }
                            return
                        }
                        fputs("VREADY\n", stderr)
                    }
                }
            }
        }
    }

    // 回调运行在 q（指定的 sampleHandlerQueue）⇒ 与 start/stop 串行，像素缓冲在本作用域内有效
    func stream(_ stream: SCStream, didOutputSampleBuffer buf: CMSampleBuffer,
                of type: SCStreamOutputType) {
        guard type == .screen, let aw = writer, let vinput = input, let adp = adaptor else { return }
        guard CMSampleBufferIsValid(buf), CMSampleBufferGetNumSamples(buf) > 0 else { return }
        // 仅接受 complete 帧（跳过 idle/blank），避免无变化帧污染
        if let arr = CMSampleBufferGetSampleAttachmentsArray(buf, createIfNecessary: false)
                as? [[SCStreamFrameInfo: Any]],
           let raw = arr.first?[.status] as? Int,
           let st  = SCFrameStatus(rawValue: raw), st != .complete { return }
        guard let px = CMSampleBufferGetImageBuffer(buf) else { return }
        let pts = CMSampleBufferGetPresentationTimeStamp(buf)
        if !started {
            aw.startWriting(); aw.startSession(atSourceTime: pts); started = true
        }
        if vinput.isReadyForMoreMediaData { adp.append(px, withPresentationTime: pts) }
    }

    func stream(_ stream: SCStream, didStopWithError error: any Error) {
        fputs("video stream stopped: \(error)\n", stderr)
    }

    // 录制中动态更新捕获矩形（仅移动 x/y，w/h 维持启动值 ⇒ 输出尺寸不变）
    func region(x: Double, y: Double, w: Double, h: Double) {
        q.async { [self] in
            guard let stream = scStream, let cfg = cfg else { return }
            cfg.sourceRect = CGRect(x: x - dispOrigin.x, y: y - dispOrigin.y, width: w, height: h)
            stream.updateConfiguration(cfg) { _ in }
        }
    }

    func stop() {
        q.async { [self] in
            guard let stream = scStream else { fputs("VSAVED\n", stderr); return }
            scStream = nil
            stream.stopCapture { _ in
                self.q.async {
                    guard self.started, let aw = self.writer, let vinput = self.input else {
                        fputs("VSAVED\n", stderr)
                        self.writer = nil; self.input = nil; self.adaptor = nil; return
                    }
                    vinput.markAsFinished()
                    aw.finishWriting {
                        fputs("VSAVED\n", stderr)
                        self.q.async { self.writer = nil; self.input = nil; self.adaptor = nil }
                    }
                }
            }
        }
    }
}

let recorder = Recorder()
let videoRecorder = VideoRecorder()

// ── 信号处理：优雅退出 ────────────────────────────────────────────────────────
func installStopHandlers() {
    for sig in [SIGTERM, SIGINT] {
        signal(sig, SIG_IGN)
        let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
        src.setEventHandler {
            recorder.stop()
            videoRecorder.stop()
            DispatchQueue.main.asyncAfter(deadline: .now() + 2) { exit(0) }
        }
        src.resume()
    }
}
installStopHandlers()

// ── stdin 指令循环（后台线程阻塞读取）──────────────────────────────────────────
DispatchQueue.global().async {
    while let line = readLine(strippingNewline: true) {
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        if trimmed.isEmpty { continue }
        let parts = trimmed.split(separator: " ", maxSplits: 1).map(String.init)
        switch parts[0] {
        case "START":
            recorder.start(parts.count > 1 ? parts[1] : "/tmp/sys_audio.caf")
        case "STOP":
            recorder.stop()
        case "VSTART":
            // VSTART <x> <y> <w> <h> <outW> <outH> <path>（数值在前，路径放最后 ⇒ 允许含空格）
            let a = (parts.count > 1 ? parts[1] : "").split(separator: " ", maxSplits: 6).map(String.init)
            if a.count >= 7, let x = Double(a[0]), let y = Double(a[1]),
               let w = Double(a[2]), let h = Double(a[3]), let ow = Int(a[4]), let oh = Int(a[5]) {
                videoRecorder.start(path: a[6], x: x, y: y, w: w, h: h, outW: ow, outH: oh)
            } else { fputs("ERROR: bad VSTART args\n", stderr) }
        case "VREGION":
            // VREGION <x> <y> <w> <h>（录制中移动录屏区，实时改捕获矩形）
            let a = (parts.count > 1 ? parts[1] : "").split(separator: " ").map(String.init)
            if a.count >= 4, let x = Double(a[0]), let y = Double(a[1]),
               let w = Double(a[2]), let h = Double(a[3]) {
                videoRecorder.region(x: x, y: y, w: w, h: h)
            }
        case "VSTOP":
            videoRecorder.stop()
        case "QUIT":
            recorder.stop()
            videoRecorder.stop()
            DispatchQueue.main.asyncAfter(deadline: .now() + 2) { exit(0) }
        default:
            fputs("ERROR: unknown command \(parts[0])\n", stderr)
        }
    }
    // stdin EOF：父进程已退出，优雅收尾
    recorder.stop()
    videoRecorder.stop()
    DispatchQueue.main.asyncAfter(deadline: .now() + 2) { exit(0) }
}

RunLoop.main.run()
