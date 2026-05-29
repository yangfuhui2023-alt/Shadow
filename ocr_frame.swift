// ocr_frame.swift — 用 Apple Vision 识别图像中的文字
// 用法: ocr_frame <image_path>
import Vision
import AppKit
import Foundation

guard CommandLine.arguments.count > 1,
      let nsImg  = NSImage(contentsOfFile: CommandLine.arguments[1]),
      let cgImg  = nsImg.cgImage(forProposedRect: nil, context: nil, hints: nil)
else { exit(1) }

let req = VNRecognizeTextRequest()
req.recognitionLevel       = .accurate
req.recognitionLanguages   = ["en-US"]
req.usesLanguageCorrection = true

try VNImageRequestHandler(cgImage: cgImg).perform([req])

let lines = (req.results ?? [])
    .compactMap { $0.topCandidates(1).first?.string }
    .filter { !$0.isEmpty }
print(lines.joined(separator: " "))
