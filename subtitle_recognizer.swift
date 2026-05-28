// subtitle_recognizer.swift — 实时字幕识别
// ScreenCaptureKit 捕获系统音频 → SFSpeechRecognizer 识别
// 识别结果逐行输出到 stdout，供 Python 读取
// 用法: subtitle_recognizer [语言代码，默认 en-US]
// 示例: subtitle_recognizer zh-CN

import ScreenCaptureKit
import Speech
import AVFoundation
import Foundation

let langCode  = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "en-US"
let audioQ    = DispatchQueue(label: "sub.audio", qos: .userInteractive)
let recognQ   = DispatchQueue(label: "sub.recogn", qos: .userInteractive)

// ── CMSampleBuffer → AVAudioPCMBuffer 转换 ───────────────────────────────────
extension CMSampleBuffer {
    func toPCMBuffer() -> AVAudioPCMBuffer? {
        guard let desc = CMSampleBufferGetFormatDescription(self),
              let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(desc) else { return nil }
        var fmt = asbd.pointee
        guard let avFmt = AVAudioFormat(streamDescription: &fmt) else { return nil }
        let frames = AVAudioFrameCount(CMSampleBufferGetNumSamples(self))
        guard frames > 0,
              let pcm = AVAudioPCMBuffer(pcmFormat: avFmt, frameCapacity: frames) else { return nil }
        pcm.frameLength = frames
        guard CMSampleBufferCopyPCMDataIntoAudioBufferList(
            self, at: 0, frameCount: Int32(frames), into: pcm.mutableAudioBufferList) == noErr
        else { return nil }
        return pcm
    }
}

// ── 识别引擎（支持自动重启，绕过 60s 上限） ──────────────────────────────────
final class Recognizer: @unchecked Sendable {
    private let sfRecognizer:  SFSpeechRecognizer
    private var request:       SFSpeechAudioBufferRecognitionRequest?
    private var task:          SFSpeechRecognitionTask?
    private var lastPrinted    = ""
    private let lock           = NSLock()
    var active                 = false

    init?(locale: Locale) {
        guard let r = SFSpeechRecognizer(locale: locale), r.isAvailable else { return nil }
        sfRecognizer = r
        sfRecognizer.defaultTaskHint = .dictation
    }

    func start() {
        lock.lock(); defer { lock.unlock() }
        _startLocked()
    }

    private func _startLocked() {
        let req = SFSpeechAudioBufferRecognitionRequest()
        req.shouldReportPartialResults   = true
        req.requiresOnDeviceRecognition  = true   // 不限时，离线运行
        request = req
        active  = true

        task = sfRecognizer.recognitionTask(with: req) { [weak self] result, error in
            guard let self else { return }
            if let result {
                let text = result.bestTranscription.formattedString
                self.lock.lock()
                let prev = self.lastPrinted
                self.lock.unlock()

                if text != prev && !text.isEmpty {
                    print(text)
                    fflush(stdout)
                    self.lock.lock()
                    self.lastPrinted = text
                    self.lock.unlock()
                }
                if result.isFinal {
                    self.lock.lock()
                    self.lastPrinted = ""
                    self.lock.unlock()
                    // 最终结果后短暂延迟再重启，避免丢帧
                    recognQ.asyncAfter(deadline: .now() + 0.3) { [weak self] in
                        self?.lock.lock()
                        if self?.active == true { self?._startLocked() }
                        self?.lock.unlock()
                    }
                }
            }
            if let error = error {
                let nsErr = error as NSError
                // 超时或结束时自动重启
                if nsErr.code != 1110 && nsErr.code != 216 {
                    fputs("[recognizer] \(error.localizedDescription)\n", stderr)
                }
                recognQ.asyncAfter(deadline: .now() + 0.5) { [weak self] in
                    self?.lock.lock()
                    if self?.active == true { self?._startLocked() }
                    self?.lock.unlock()
                }
            }
        }

        // 45 秒后主动结束任务触发重启（Apple 服务端识别有 60s 上限）
        recognQ.asyncAfter(deadline: .now() + 45) { [weak self] in
            self?.lock.lock()
            if self?.active == true { self?.request?.endAudio() }
            self?.lock.unlock()
        }
    }

    func append(_ buf: AVAudioPCMBuffer) {
        lock.lock()
        request?.append(buf)
        lock.unlock()
    }

    func stop() {
        lock.lock(); defer { lock.unlock() }
        active = false
        request?.endAudio()
        task?.cancel()
        request = nil; task = nil
    }
}

// ── ScreenCaptureKit 音频输出 ─────────────────────────────────────────────────
final class AudioCapture: NSObject, SCStreamOutput, SCStreamDelegate, @unchecked Sendable {
    let recognizer: Recognizer
    init(_ r: Recognizer) { recognizer = r }

    func stream(_ stream: SCStream, didOutputSampleBuffer buf: CMSampleBuffer,
                of type: SCStreamOutputType) {
        guard type == .audio, let pcm = buf.toPCMBuffer() else { return }
        recognizer.append(pcm)
    }
    func stream(_ stream: SCStream, didStopWithError error: any Error) {
        fputs("[stream] \(error)\n", stderr)
    }
}

// ── 权限请求 ──────────────────────────────────────────────────────────────────
let speechSema = DispatchSemaphore(value: 0)
SFSpeechRecognizer.requestAuthorization { status in
    if status != .authorized {
        fputs("ERROR: 语音识别未授权。请在「系统设置 → 隐私 → 语音识别」中开启。\n", stderr)
        exit(1)
    }
    speechSema.signal()
}
speechSema.wait()

guard let recognizer = Recognizer(locale: Locale(identifier: langCode)) else {
    fputs("ERROR: 无法创建识别器（语言：\(langCode)）\n", stderr)
    exit(1)
}

// ── 启动 ScreenCaptureKit ─────────────────────────────────────────────────────
var scStream:    SCStream?
let capture    = AudioCapture(recognizer)

func installStopHandlers(_ stop: @escaping () -> Void) {
    for sig in [SIGTERM, SIGINT] {
        signal(sig, SIG_IGN)
        let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
        src.setEventHandler { stop() }
        src.resume()
    }
}
installStopHandlers {
    recognizer.stop()
    scStream?.stopCapture { _ in exit(0) }
    DispatchQueue.main.asyncAfter(deadline: .now() + 3) { exit(0) }
}

SCShareableContent.getWithCompletionHandler { content, error in
    guard let display = content?.displays.first else {
        fputs("ERROR: 无法获取屏幕内容（\(error?.localizedDescription ?? "unknown")）\n", stderr)
        exit(1)
    }

    let cfg = SCStreamConfiguration()
    cfg.capturesAudio               = true
    cfg.excludesCurrentProcessAudio = true
    cfg.width  = 2
    cfg.height = 2

    let filter = SCContentFilter(display: display,
                                 excludingApplications: [], exceptingWindows: [])
    let stream = SCStream(filter: filter, configuration: cfg, delegate: capture)
    scStream = stream

    do {
        try stream.addStreamOutput(capture, type: .audio, sampleHandlerQueue: audioQ)
        stream.startCapture { err in
            if let err {
                fputs("ERROR: \(err)\n", stderr)
                exit(1)
            }
            recognizer.start()
            fputs("READY\n", stderr)
        }
    } catch {
        fputs("ERROR: \(error)\n", stderr)
        exit(1)
    }
}

RunLoop.main.run()
