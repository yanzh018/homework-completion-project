import Foundation
import Vision
import AppKit

// Local only: Apple's accurate boxes have word precision, never pretend they are character boxes.
func box(_ r: CGRect) -> [Double] { [Double(r.minX), Double(1-r.maxY), Double(r.maxX), Double(1-r.minY)] }
do {
    guard CommandLine.arguments.count > 1 else { throw NSError(domain:"usage: vision_ocr image",code:1) }
    let url = URL(fileURLWithPath:CommandLine.arguments[1])
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["en-US"]
    request.usesLanguageCorrection = false
    request.minimumTextHeight = 0.003
    try VNImageRequestHandler(url:url, options:[:]).perform([request])
    let result: [[String:Any]] = (request.results ?? []).compactMap { observation in
        guard let text = observation.topCandidates(1).first else { return nil }
        let s = text.string
        let regex = try! NSRegularExpression(pattern:"\\S+")
        let words: [[String:Any]] = regex.matches(in:s, range:NSRange(s.startIndex...,in:s)).compactMap { m in
            guard let range = Range(m.range,in:s), let rectangle = try? text.boundingBox(for:range) else { return nil }
            return ["text":String(s[range]),"bbox":box(rectangle.boundingBox),"confidence":Double(text.confidence)]
        }
        return ["text":s,"bbox":box(observation.boundingBox),"confidence":Double(text.confidence),"words":words]
    }
    let data = try JSONSerialization.data(withJSONObject:result,options:[.sortedKeys])
    print(String(data:data,encoding:.utf8)!)
} catch {
    fputs("Local Vision OCR failed: \(error)\n",stderr)
    exit(1)
}
