import AppKit
import CoreImage
import CoreImage.CIFilterBuiltins
import SwiftUI

enum QRCodeGenerator {
    private static let context = CIContext()

    static func makeImage(from text: String, scale: CGFloat = 8) -> NSImage? {
        guard !text.isEmpty, let data = text.data(using: .utf8) else { return nil }
        let filter = CIFilter.qrCodeGenerator()
        filter.setValue(data, forKey: "inputMessage")
        filter.setValue("M", forKey: "inputCorrectionLevel")
        guard let output = filter.outputImage else { return nil }
        let scaled = output.transformed(by: CGAffineTransform(scaleX: scale, y: scale))
        guard let cg = context.createCGImage(scaled, from: scaled.extent) else { return nil }
        return NSImage(cgImage: cg, size: NSSize(width: scaled.extent.width, height: scaled.extent.height))
    }
}

struct QRCodeCardView: View {
    let urlString: String
    var size: CGFloat = 156

    var body: some View {
        VStack(spacing: 10) {
            if let img = QRCodeGenerator.makeImage(from: urlString) {
                Image(nsImage: img)
                    .interpolation(.none)
                    .resizable()
                    .scaledToFit()
                    .frame(width: size, height: size)
                    .padding(10)
                    .background(Color.white)
                    .clipShape(RoundedRectangle(cornerRadius: 8))
            }
            Text(urlString)
                .font(HarnessTheme.monoSmall)
                .foregroundStyle(HarnessTheme.dim)
                .textSelection(.enabled)
                .multilineTextAlignment(.center)
                .lineLimit(2)
        }
    }
}
