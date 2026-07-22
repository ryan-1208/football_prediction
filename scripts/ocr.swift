import Foundation
import Vision
import ImageIO

struct OCRLine: Codable {
    let text: String
    let confidence: Float
}

struct OCRResponse: Codable {
    let lines: [OCRLine]
}

guard CommandLine.arguments.count == 2 else {
    FileHandle.standardError.write(Data("Usage: ocr.swift <image-path>\n".utf8))
    exit(2)
}

let imageURL = URL(fileURLWithPath: CommandLine.arguments[1])
guard let source = CGImageSourceCreateWithURL(imageURL as CFURL, nil),
      let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
    FileHandle.standardError.write(Data("无法读取图片\n".utf8))
    exit(2)
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true
request.recognitionLanguages = ["zh-Hans", "en-US"]

let handler = VNImageRequestHandler(cgImage: image, options: [:])
do {
    try handler.perform([request])
    let lines = (request.results ?? []).compactMap { observation -> OCRLine? in
        guard let candidate = observation.topCandidates(1).first else { return nil }
        return OCRLine(text: candidate.string, confidence: candidate.confidence)
    }
    let data = try JSONEncoder().encode(OCRResponse(lines: lines))
    FileHandle.standardOutput.write(data)
} catch {
    FileHandle.standardError.write(Data("OCR 识别失败：\(error.localizedDescription)\n".utf8))
    exit(1)
}
