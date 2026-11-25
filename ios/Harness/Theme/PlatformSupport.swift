import Foundation
import ImageIO
import SwiftUI
import UniformTypeIdentifiers

#if canImport(UIKit)
import UIKit
typealias PlatformImage = UIImage
#elseif canImport(AppKit)
import AppKit
typealias PlatformImage = NSImage
#endif

extension Image {
    init(platformImage: PlatformImage) {
        #if canImport(UIKit)
        self.init(uiImage: platformImage)
        #elseif canImport(AppKit)
        self.init(nsImage: platformImage)
        #endif
    }
}

enum PlatformImageCodec {
    /// Decodes image bytes into the native image type on iOS or macOS.
    static func image(from data: Data) -> PlatformImage? {
        #if canImport(UIKit)
        return UIImage(data: data)
        #elseif canImport(AppKit)
        return NSImage(data: data)
        #endif
    }

    /// Resizes an image to fit `maxEdge` and encodes as JPEG using CoreGraphics/ImageIO,
    /// working identically on both iOS and macOS with zero platform-specific UIKit code.
    static func prepareJPEG(from data: Data, maxEdge: CGFloat, quality: CGFloat) -> Data? {
        #if canImport(UIKit)
        guard let img = UIImage(data: data) else { return nil }
        let size = img.size
        let longest = max(size.width, size.height)
        let target: CGSize
        if longest > maxEdge && longest > 0 {
            let scale = maxEdge / longest
            target = CGSize(width: floor(size.width * scale), height: floor(size.height * scale))
        } else {
            target = size
        }
        let renderer = UIGraphicsImageRenderer(size: target)
        let resized = renderer.image { _ in
            img.draw(in: CGRect(origin: .zero, size: target))
        }
        return resized.jpegData(compressionQuality: quality)
        #elseif canImport(AppKit)
        guard let src = CGImageSourceCreateWithData(data as CFData, nil) else { return nil }
        let options: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceThumbnailMaxPixelSize: Int(maxEdge),
        ]
        guard let cgImage = CGImageSourceCreateThumbnailAtIndex(src, 0, options as CFDictionary)
                ?? CGImageSourceCreateImageAtIndex(src, 0, nil) else {
            return nil
        }
        let out = NSMutableData()
        guard let dest = CGImageDestinationCreateWithData(out, UTType.jpeg.identifier as CFString, 1, nil) else {
            return nil
        }
        CGImageDestinationAddImage(dest, cgImage, [kCGImageDestinationLossyCompressionQuality: quality] as CFDictionary)
        guard CGImageDestinationFinalize(dest) else { return nil }
        return out as Data
        #endif
    }
}

enum PlatformClipboard {
    static func copy(_ text: String) {
        #if canImport(UIKit)
        UIPasteboard.general.string = text
        #elseif canImport(AppKit)
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        #endif
    }

    static func pasteString() -> String? {
        #if canImport(UIKit)
        return UIPasteboard.general.string
        #elseif canImport(AppKit)
        return NSPasteboard.general.string(forType: .string)
        #endif
    }

    #if canImport(AppKit)
    static func pasteImageData() -> Data? {
        let pb = NSPasteboard.general
        if let png = pb.data(forType: .png) { return png }
        if let tiff = pb.data(forType: .tiff) { return tiff }
        if let urls = pb.readObjects(forClasses: [NSURL.self]) as? [URL] {
            for url in urls where url.isFileURL {
                if let type = try? url.resourceValues(forKeys: [.contentTypeKey]).contentType,
                   type.conforms(to: .image),
                   let data = try? Data(contentsOf: url) {
                    return data
                }
            }
        }
        return nil
    }
    #endif
}

/// Vector recreation of `harness/icon.svg` usable on both iOS and macOS.
struct HarnessBrandMark: View {
    var size: CGFloat = 32

    var body: some View {
        Canvas { ctx, sz in
            let s = sz.width / 64.0
            let bg = Path(roundedRect: CGRect(origin: .zero, size: sz), cornerRadius: sz.width * 0.22)
            ctx.fill(bg, with: .linearGradient(
                Gradient(colors: [Color(red: 27/255, green: 34/255, blue: 32/255), HarnessTheme.ink]),
                startPoint: .zero,
                endPoint: CGPoint(x: 0, y: sz.height)
            ))
            var stem = Path()
            stem.move(to: CGPoint(x: 32 * s, y: 11 * s))
            stem.addLine(to: CGPoint(x: 32 * s, y: 19 * s))
            ctx.stroke(stem, with: .color(Color(red: 58/255, green: 71/255, blue: 65/255)),
                       style: StrokeStyle(lineWidth: 2.5 * s, lineCap: .round))
            ctx.fill(Path(ellipseIn: CGRect(x: 28.8 * s, y: 5.8 * s, width: 6.4 * s, height: 6.4 * s)),
                     with: .color(HarnessTheme.sage))
            ctx.fill(Path(ellipseIn: CGRect(x: 14 * s, y: 27 * s, width: 16 * s, height: 16 * s)),
                     with: .color(HarnessTheme.amber))
            ctx.fill(Path(ellipseIn: CGRect(x: 38.4 * s, y: 29.4 * s, width: 11.2 * s, height: 11.2 * s)),
                     with: .color(HarnessTheme.amber))
            ctx.fill(Path(ellipseIn: CGRect(x: 17.1 * s, y: 30.1 * s, width: 5 * s, height: 5 * s)),
                     with: .color(HarnessTheme.ink.opacity(0.55)))
            ctx.fill(Path(ellipseIn: CGRect(x: 40.5 * s, y: 31.5 * s, width: 3.6 * s, height: 3.6 * s)),
                     with: .color(HarnessTheme.ink.opacity(0.55)))
            ctx.fill(Path(roundedRect: CGRect(x: 22 * s, y: 50 * s, width: 20 * s, height: 3.4 * s),
                          cornerRadius: 1.7 * s),
                     with: .color(HarnessTheme.sage.opacity(0.85)))
            ctx.fill(Path(roundedRect: CGRect(x: 45 * s, y: 50 * s, width: 4.4 * s, height: 3.4 * s),
                          cornerRadius: 1.7 * s),
                     with: .color(Color(red: 58/255, green: 71/255, blue: 65/255)))
        }
        .frame(width: size, height: size)
        .overlay(
            RoundedRectangle(cornerRadius: size * 0.22)
                .stroke(HarnessTheme.lineHot, lineWidth: 1)
        )
    }
}
