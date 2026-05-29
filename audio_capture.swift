// audio_capture — 录制系统音频（ScreenCaptureKit，macOS 12.3+）
// 用法: audio_capture <output.caf>
// stderr 输出 READY 表示就绪；发送 SIGTERM/SIGINT 停止并写入文件

import ScreenCaptureKit
import AVFoundation
import Foundation

// ── 参数 ─────────────────────────────────────────────────────────────────────
let outPath = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "/tmp/sys_audio.caf"

// ── 音频写入器 ────────────────────────────────────────────────────────────────
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
                writer.startSession(atSourceTime: .zero)
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

guard let aw = try? AudioWriter(url: URL(fileURLWithPath: outPath)) else {
    fputs("ERROR: cannot create output file\n", stderr)
    exit(1)
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

// ── 停止信号处理 ──────────────────────────────────────────────────────────────
var scStream:  SCStream?
let outputObj = Output(aw)

// DispatchSource 可以安全捕获上下文（Swift 6 兼容）
func installStopHandlers(_ stop: @escaping () -> Void) {
    for sig in [SIGTERM, SIGINT] {
        signal(sig, SIG_IGN)
        let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
        src.setEventHandler { stop() }
        src.resume()
    }
}
installStopHandlers {
    scStream?.stopCapture { _ in
        aw.finalize { exit(0) }
    }
    DispatchQueue.main.asyncAfter(deadline: .now() + 5) { exit(0) }
}

// ── 获取可录内容并启动 ────────────────────────────────────────────────────────
SCShareableContent.getWithCompletionHandler { content, error in
    guard let content else {
        fputs("ERROR: SCShareableContent: \(error?.localizedDescription ?? "unknown")\n", stderr)
        fputs("TIP: grant Screen Recording permission in System Settings → Privacy\n", stderr)
        exit(1)
    }
    guard let display = content.displays.first else {
        fputs("ERROR: no display found\n", stderr)
        exit(1)
    }

    let cfg = SCStreamConfiguration()
    cfg.capturesAudio               = true
    cfg.excludesCurrentProcessAudio = true
    // 最小视频分辨率以节省资源（只需要音频）
    cfg.width  = 2
    cfg.height = 2

    let filter = SCContentFilter(display: display,
                                 excludingApplications: [],
                                 exceptingWindows: [])
    let stream = SCStream(filter: filter, configuration: cfg, delegate: outputObj)
    scStream = stream

    do {
        try stream.addStreamOutput(outputObj, type: .audio,
                                   sampleHandlerQueue: DispatchQueue(label: "sc.audio"))
    } catch {
        fputs("ERROR: addStreamOutput: \(error)\n", stderr)
        exit(1)
    }

    stream.startCapture { error in
        if let error {
            fputs("ERROR: startCapture: \(error)\n", stderr)
            exit(1)
        }
        fputs("READY\n", stderr)
    }
}

RunLoop.main.run()
