import Foundation
import Observation
import SwiftUI

enum MacSettingsPane: String, CaseIterable, Identifiable {
    case overview = "Mac & Status"
    case model = "Model"
    case behaviour = "Behaviour"
    case permissions = "Permissions"
    case automations = "Automations"
    case voice = "Voice"
    case remote = "iPhone & Remote"
    case logs = "Logs"

    var id: String { rawValue }

    var icon: String {
        switch self {
        case .overview: return "macmini"
        case .model: return "cpu"
        case .behaviour: return "sparkles"
        case .permissions: return "hand.raised"
        case .automations: return "clock.badge.checkmark"
        case .voice: return "waveform"
        case .remote: return "iphone"
        case .logs: return "doc.text.magnifyingglass"
        }
    }

    var tint: Color {
        switch self {
        case .overview: return HarnessTheme.sage
        case .model: return HarnessTheme.amber
        case .behaviour: return HarnessTheme.amber
        case .permissions: return HarnessTheme.rust
        case .automations: return HarnessTheme.sage
        case .voice: return HarnessTheme.amberDim
        case .remote: return HarnessTheme.sage
        case .logs: return HarnessTheme.dim
        }
    }
}

/// Shared UI coordination state between the main window, menu bar, and Settings window.
@MainActor
@Observable
final class MacUIState {
    var selectedSettingsPane: MacSettingsPane = .overview
    var showSidebar: Bool = true
}

// MARK: - Reusable Crafted macOS Primitives

/// Subtle dark card matching Harness's instrument-panel aesthetic (`#111514` on `#0b0d0c`).
struct MacPanelCard<Content: View>: View {
    var border: Color = HarnessTheme.line
    var background: Color = HarnessTheme.panel
    var padding: CGFloat = 16
    @ViewBuilder let content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            content()
        }
        .padding(padding)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(background)
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(border, lineWidth: 1))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }
}

struct MacSectionTitle: View {
    let title: String
    var subtitle: String? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(title.uppercased())
                .font(.system(size: 10.5, weight: .semibold))
                .tracking(1.3)
                .foregroundStyle(HarnessTheme.dim)
            if let subtitle {
                Text(subtitle)
                    .font(.caption)
                    .foregroundStyle(HarnessTheme.faint)
            }
        }
    }
}

struct MacBadge: View {
    let text: String
    let color: Color

    var body: some View {
        Text(text)
            .font(.system(size: 9.5, weight: .semibold, design: .monospaced))
            .foregroundStyle(color)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(color.opacity(0.13))
            .overlay(RoundedRectangle(cornerRadius: 4).stroke(color.opacity(0.35), lineWidth: 0.75))
            .clipShape(RoundedRectangle(cornerRadius: 4))
    }
}

struct MacIconTile: View {
    let systemName: String
    let tint: Color
    var size: CGFloat = 26

    var body: some View {
        Image(systemName: systemName)
            .font(.system(size: size * 0.48, weight: .semibold))
            .foregroundStyle(tint)
            .frame(width: size, height: size)
            .background(RoundedRectangle(cornerRadius: 6).fill(tint.opacity(0.15)))
            .overlay(RoundedRectangle(cornerRadius: 6).stroke(tint.opacity(0.25), lineWidth: 0.75))
    }
}

struct HoverHighlightModifier: ViewModifier {
    var cornerRadius: CGFloat = 6
    var active: Bool = false
    @State private var hovered = false

    func body(content: Content) -> some View {
        content
            .background(
                RoundedRectangle(cornerRadius: cornerRadius)
                    .fill(active ? HarnessTheme.panel2 : (hovered ? HarnessTheme.panel2.opacity(0.75) : Color.clear))
            )
            .overlay(
                RoundedRectangle(cornerRadius: cornerRadius)
                    .stroke(active ? HarnessTheme.amberDim : (hovered ? HarnessTheme.lineHot : Color.clear), lineWidth: 1)
            )
            .onHover { hovered = $0 }
            .animation(.easeOut(duration: 0.12), value: hovered)
    }
}

extension View {
    func macHoverRow(cornerRadius: CGFloat = 6, active: Bool = false) -> some View {
        modifier(HoverHighlightModifier(cornerRadius: cornerRadius, active: active))
    }
}

enum MacFormatting {
    static func readableSize(_ raw: String) -> String {
        let units = ["Ki": "KB", "Mi": "MB", "Gi": "GB", "Ti": "TB", "K": "KB", "M": "MB", "G": "GB", "T": "TB"]
        for (suffix, unit) in units.sorted(by: { $0.key.count > $1.key.count }) where raw.hasSuffix(suffix) {
            return "\(raw.dropLast(suffix.count)) \(unit)"
        }
        return raw.isEmpty ? "—" : raw
    }

    static func memoryTint(_ raw: String) -> Color {
        guard let pct = Int(raw.filter(\.isNumber)) else { return HarnessTheme.text }
        if pct < 15 { return HarnessTheme.rust }
        if pct < 30 { return HarnessTheme.amber }
        return HarnessTheme.sage
    }

    static var appVersion: String {
        let info = Bundle.main.infoDictionary
        let v = info?["CFBundleShortVersionString"] as? String ?? "1.0.0"
        let b = info?["CFBundleVersion"] as? String ?? "1"
        return "\(v) (\(b))"
    }
}
