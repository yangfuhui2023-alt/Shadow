// subtitle_file.swift — 文件级字幕识别（编辑器用）
// 对一个音频文件跑 SFSpeechRecognizer（优先端上识别，绕开服务端 60s 限制），
// 识别完把整段文本打到 stdout 后退出。
// 用法: subtitle_file <音频文件路径> [语言代码，默认 en-US]

import Speech
import Foundation

let args = CommandLine.arguments
guard args.count > 1 else { fputs("ERROR: 缺少音频文件参数\n", stderr); exit(2) }
let path = args[1]
let lang = args.count > 2 ? args[2] : "en-US"

guard FileManager.default.fileExists(atPath: path) else {
    fputs("ERROR: 文件不存在: \(path)\n", stderr); exit(2)
}

// 语音识别授权
let sema = DispatchSemaphore(value: 0)
SFSpeechRecognizer.requestAuthorization { status in
    if status != .authorized {
        fputs("ERROR: 语音识别未授权（系统设置 → 隐私 → 语音识别）\n", stderr)
        exit(1)
    }
    sema.signal()
}
sema.wait()

guard let rec = SFSpeechRecognizer(locale: Locale(identifier: lang)) else {
    fputs("ERROR: 无法创建识别器（语言：\(lang)）\n", stderr); exit(1)
}

let req = SFSpeechURLRecognitionRequest(url: URL(fileURLWithPath: path))
req.shouldReportPartialResults = false
if rec.supportsOnDeviceRecognition {
    req.requiresOnDeviceRecognition = true     // 端上：无 60s 上限、长音频更稳
}

rec.recognitionTask(with: req) { result, error in
    if let error = error {
        fputs("ERROR: \(error.localizedDescription)\n", stderr)
        exit(1)
    }
    guard let result = result else { return }
    if result.isFinal {
        let tr = result.bestTranscription
        // 逐词/段输出：S<TAB>起始秒<TAB>时长秒<TAB>文本（供 Python 按节奏分句 + 随播放同步）
        for seg in tr.segments {
            let w = seg.substring.replacingOccurrences(of: "\t", with: " ")
            print("S\t\(seg.timestamp)\t\(seg.duration)\t\(w)")
        }
        if tr.segments.isEmpty {
            print("S\t0\t0\t\(tr.formattedString)")
        }
        fflush(stdout)
        exit(0)
    }
}

RunLoop.main.run()
