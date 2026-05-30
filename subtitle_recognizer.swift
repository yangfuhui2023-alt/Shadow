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

    // 双路 fallback 状态
    private var useOnDevice    = false   // 当前会话用端上识别
    private var sessionStarted = Date()
    private var hasResult      = false   // 本次会话是否拿到过结果

    init?(locale: Locale) {
        guard let r = SFSpeechRecognizer(locale: locale) else { return nil }
        sfRecognizer = r
        sfRecognizer.defaultTaskHint = .dictation
        // 初始策略：服务端不可用就直接走端上（不再因 !isAvailable 失败退出）
        if !r.isAvailable && r.supportsOnDeviceRecognition {
            useOnDevice = true
            fputs("[recognizer] 服务端不可用，初始即用端上识别\n", stderr)
        } else if !r.isAvailable {
            fputs("[recognizer] 服务端不可用且端上不支持当前语言 — 仍尝试启动\n", stderr)
        }
    }

    func start() {
        lock.lock(); defer { lock.unlock() }
        _startLocked()
    }

    private func _startLocked() {
        let req = SFSpeechAudioBufferRecognitionRequest()
        req.shouldReportPartialResults   = true
        req.requiresOnDeviceRecognition  = useOnDevice
        request = req
        active  = true
        sessionStarted = Date()
        hasResult      = false

        fputs("MODE: \(useOnDevice ? "local" : "server")\n", stderr)

        task = sfRecognizer.recognitionTask(with: req) { [weak self] result, error in
            guard let self else { return }
            if let result {
                self.lock.lock(); self.hasResult = true; self.lock.unlock()
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
                    recognQ.asyncAfter(deadline: .now() + 0.3) { [weak self] in
                        self?.lock.lock()
                        if self?.active == true { self?._startLocked() }
                        self?.lock.unlock()
                    }
                }
            }
            if let error = error {
                let nsErr = error as NSError
                // Fallback 判断：当前在服务端、整个会话没拿到过结果、且耗时短 → 切端上
                self.lock.lock()
                let elapsed       = Date().timeIntervalSince(self.sessionStarted)
                let shouldFallback = !self.useOnDevice && !self.hasResult && elapsed < 6
                                     && self.sfRecognizer.supportsOnDeviceRecognition
                self.lock.unlock()
                if shouldFallback {
                    fputs("[recognizer] 服务端识别启动失败 (\(error.localizedDescription))，降级到端上识别\n", stderr)
                    self.lock.lock(); self.useOnDevice = true; self.lock.unlock()
                }
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

        // Watchdog：服务端模式下 4 秒还没拿到任何结果 → 主动取消触发降级
        let initiallyOnDevice = useOnDevice
        recognQ.asyncAfter(deadline: .now() + 4) { [weak self] in
            guard let self else { return }
            self.lock.lock()
            let stuck = self.active && !self.hasResult
                        && !initiallyOnDevice && !self.useOnDevice
                        && self.sfRecognizer.supportsOnDeviceRecognition
            let currentTask = self.task
            self.lock.unlock()
            if stuck {
                fputs("[recognizer] 服务端 4 秒无响应，降级到端上识别\n", stderr)
                self.lock.lock(); self.useOnDevice = true; self.lock.unlock()
                currentTask?.cancel()
            }
        }

        // 45 秒后主动结束任务触发重启
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
