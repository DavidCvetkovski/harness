import SwiftUI
import UIKit

// MARK: - Authenticated Thumbnail & Zoom Viewer

struct AuthenticatedImageView: View {
    let store: AppStore
    let urlPath: String
    let localData: Data?
    var height: CGFloat = 110

    @State private var loaded: UIImage?
    @State private var showFullScreen = false

    var body: some View {
        Group {
            if let img = displayImage {
                Image(uiImage: img)
                    .resizable()
                    .scaledToFill()
                    .frame(width: height, height: height)
                    .clipped()
                    .overlay(RoundedRectangle(cornerRadius: 4).stroke(HarnessTheme.lineHot, lineWidth: 1))
                    .clipShape(RoundedRectangle(cornerRadius: 4))
                    .onTapGesture { showFullScreen = true }
            } else {
                ZStack {
                    HarnessTheme.panel2
                    ProgressView().tint(HarnessTheme.amber)
                }
                .frame(width: height, height: height)
                .clipShape(RoundedRectangle(cornerRadius: 4))
            }
        }
        .task(id: urlPath) {
            if localData == nil && loaded == nil {
                loaded = await store.loadImage(path: urlPath)
            }
        }
        .fullScreenCover(isPresented: $showFullScreen) {
            if let img = displayImage {
                ZStack(alignment: .topTrailing) {
                    Color.black.ignoresSafeArea()
                    Image(uiImage: img)
                        .resizable()
                        .scaledToFit()
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                    Button {
                        showFullScreen = false
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                            .font(.title)
                            .foregroundStyle(.white.opacity(0.85))
                            .padding(20)
                    }
                }
            }
        }
    }

    private var displayImage: UIImage? {
        if let localData, let img = UIImage(data: localData) { return img }
        return loaded
    }
}

// MARK: - User Turn

struct UserTurnView: View {
    let store: AppStore
    let text: String
    let imageUrls: [String]
    let localImages: [Data]

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("YOU")
                .font(HarnessTheme.label)
                .tracking(1.1)
                .foregroundStyle(HarnessTheme.amberDim)

            // Same treatment as the web app: a thin amber rule down the left
            // edge, so your turns read apart from Harness's at a glance.
            VStack(alignment: .leading, spacing: 8) {
                if !imageUrls.isEmpty || !localImages.isEmpty {
                    let count = max(imageUrls.count, localImages.count)
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 8) {
                            ForEach(0..<count, id: \.self) { i in
                                AuthenticatedImageView(
                                    store: store,
                                    urlPath: i < imageUrls.count ? imageUrls[i] : "",
                                    localData: i < localImages.count ? localImages[i] : nil,
                                    height: 108
                                )
                            }
                        }
                    }
                }

                if !text.isEmpty {
                    Text(text)
                        .font(.body)
                        .foregroundStyle(HarnessTheme.text)
                        .textSelection(.enabled)
                }
            }
            .padding(.leading, 12)
            .overlay(alignment: .leading) {
                Rectangle()
                    .fill(HarnessTheme.amberDim)
                    .frame(width: 2)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.top, 10)
        .padding(.bottom, 2)
    }
}

// MARK: - Thinking Card

struct ThinkingCardView: View {
    let text: String
    let startedAt: Date
    let finishedAt: Date?
    let isExpanded: Bool
    let onToggle: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button(action: onToggle) {
                HStack(spacing: 8) {
                    Circle()
                        .fill(finishedAt == nil ? HarnessTheme.amber : HarnessTheme.dim)
                        .frame(width: 6, height: 6)
                    if finishedAt == nil {
                        TimelineView(.periodic(from: startedAt, by: 1)) { ctx in
                            let secs = max(1, Int(ctx.date.timeIntervalSince(startedAt)))
                            Text("Thinking · \(secs)s · \(text.count) chars")
                                .font(HarnessTheme.monoSmall)
                                .foregroundStyle(HarnessTheme.amber)
                        }
                    } else {
                        let secs = max(1, Int((finishedAt ?? startedAt).timeIntervalSince(startedAt)))
                        Text("Thought for \(secs)s · \(text.count) chars")
                            .font(HarnessTheme.monoSmall)
                            .foregroundStyle(HarnessTheme.dim)
                    }
                    Spacer()
                    Image(systemName: isExpanded ? "chevron.up" : "chevron.down")
                        .font(.caption2)
                        .foregroundStyle(HarnessTheme.faint)
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 9)
            }
            .buttonStyle(.plain)

            if isExpanded && !text.isEmpty {
                Divider().background(HarnessTheme.line)
                Text(text)
                    .font(HarnessTheme.monoSmall)
                    .foregroundStyle(HarnessTheme.dim)
                    .textSelection(.enabled)
                    .padding(12)
            }
        }
        .harnessCard(border: HarnessTheme.line, background: HarnessTheme.panel)
    }
}

// MARK: - Tool Call Card

struct ToolCardView: View {
    let name: String
    let args: [String: String]
    let output: String?
    let isError: Bool
    let isRunning: Bool
    let isExpanded: Bool
    let onToggle: () -> Void

    private var argSummary: String {
        if let path = args["path"] ?? args["query"] ?? args["command"] ?? args["url"] {
            return path
        }
        return args.sorted(by: { $0.key < $1.key }).map { "\($0.key)=\($0.value)" }.joined(separator: " ")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button(action: onToggle) {
                HStack(spacing: 8) {
                    Circle()
                        .fill(statusColor)
                        .frame(width: 7, height: 7)
                    Text(name)
                        .font(HarnessTheme.monoSmall.weight(.semibold))
                        .foregroundStyle(HarnessTheme.text)
                    if !argSummary.isEmpty {
                        Text(argSummary)
                            .font(HarnessTheme.monoSmall)
                            .foregroundStyle(HarnessTheme.dim)
                            .lineLimit(1)
                            .truncationMode(.middle)
                    }
                    Spacer()
                    if isRunning {
                        ProgressView()
                            .scaleEffect(0.65)
                            .tint(HarnessTheme.amber)
                    } else {
                        Image(systemName: isExpanded ? "chevron.up" : "chevron.down")
                            .font(.caption2)
                            .foregroundStyle(HarnessTheme.faint)
                    }
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 9)
            }
            .buttonStyle(.plain)

            if isExpanded {
                Divider().background(HarnessTheme.line)
                VStack(alignment: .leading, spacing: 8) {
                    if !args.isEmpty {
                        Text(args.sorted(by: { $0.key < $1.key }).map { "\($0.key): \($0.value)" }.joined(separator: "\n"))
                            .font(HarnessTheme.monoSmall)
                            .foregroundStyle(HarnessTheme.amber.opacity(0.9))
                            .textSelection(.enabled)
                    }
                    if let output, !output.isEmpty {
                        // Both axes: long output used to be clipped at the
                        // frame's height with no way to reach the rest.
                        ScrollView([.horizontal, .vertical], showsIndicators: false) {
                            Text(output)
                                .font(HarnessTheme.monoSmall)
                                .foregroundStyle(isError ? HarnessTheme.rust : HarnessTheme.dim)
                                .textSelection(.enabled)
                        }
                        .frame(maxHeight: 260)
                    }
                }
                .padding(12)
            }
        }
        .harnessCard(
            border: isError ? HarnessTheme.rust.opacity(0.55) : HarnessTheme.line,
            background: HarnessTheme.panel
        )
    }

    private var statusColor: Color {
        if isRunning { return HarnessTheme.amber }
        return isError ? HarnessTheme.rust : HarnessTheme.sage
    }
}

// MARK: - Interactive Approval Card (Diffs & Commands)

struct ApprovalCardView: View {
    let item: ApprovalItem
    let status: ApprovalStatus
    let onDecide: (_ allow: Bool, _ always: Bool) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Image(systemName: "hand.raised.fill")
                    .font(.caption)
                    .foregroundStyle(HarnessTheme.amber)
                Text(item.question)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(HarnessTheme.text)
                Spacer()
                if status != .pending {
                    Text(statusLabel)
                        .font(HarnessTheme.monoSmall.weight(.semibold))
                        .foregroundStyle(status == .allowed ? HarnessTheme.sage : HarnessTheme.rust)
                }
            }

            if !item.detail.isEmpty {
                ScrollView([.horizontal, .vertical], showsIndicators: true) {
                    VStack(alignment: .leading, spacing: 2) {
                        ForEach(Array(item.detail.split(separator: "\n", omittingEmptySubsequences: false).enumerated()), id: \.offset) { _, line in
                            let str = String(line)
                            let trimmed = str.trimmingCharacters(in: .whitespaces)
                            Text(str)
                                .font(HarnessTheme.monoSmall)
                                .foregroundStyle(diffLineColor(trimmed))
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                    .padding(10)
                }
                .frame(maxHeight: 220)
                .background(HarnessTheme.ink)
                .overlay(RoundedRectangle(cornerRadius: 4).stroke(HarnessTheme.line, lineWidth: 1))
            }

            if status == .pending {
                HStack(spacing: 8) {
                    Button {
                        onDecide(true, false)
                    } label: {
                        Text("Allow")
                            .font(.footnote.weight(.semibold))
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 9)
                            .background(HarnessTheme.amber)
                            .foregroundStyle(HarnessTheme.ink)
                            .clipShape(RoundedRectangle(cornerRadius: 4))
                    }
                    .buttonStyle(.plain)

                    if !item.kind.isEmpty {
                        Button {
                            onDecide(true, true)
                        } label: {
                            Text("Always")
                                .font(.footnote.weight(.medium))
                                .padding(.horizontal, 12)
                                .padding(.vertical, 9)
                                .foregroundStyle(HarnessTheme.text)
                                .harnessCard(border: HarnessTheme.lineHot, background: HarnessTheme.panel2)
                        }
                        .buttonStyle(.plain)
                    }

                    Button {
                        onDecide(false, false)
                    } label: {
                        Text("Deny")
                            .font(.footnote.weight(.medium))
                            .padding(.horizontal, 14)
                            .padding(.vertical, 9)
                            .foregroundStyle(HarnessTheme.rust)
                            .harnessCard(border: HarnessTheme.rust.opacity(0.45), background: HarnessTheme.rust.opacity(0.08))
                    }
                    .buttonStyle(.plain)
                }
            }
        }
        .padding(12)
        .harnessCard(
            border: status == .pending ? HarnessTheme.amber.opacity(0.65) : HarnessTheme.line,
            background: HarnessTheme.panel2
        )
    }

    private var statusLabel: String {
        switch status {
        case .allowed: return "ALLOWED"
        case .denied: return "DENIED"
        case .timedOut: return "EXPIRED"
        case .pending: return ""
        }
    }

    private func diffLineColor(_ trimmed: String) -> Color {
        if trimmed.hasPrefix("+") { return HarnessTheme.sage }
        if trimmed.hasPrefix("-") { return HarnessTheme.rust }
        return HarnessTheme.dim
    }
}

// MARK: - Assistant Markdown + Code Block Renderer

struct AssistantTurnView: View {
    let itemId: String
    let text: String
    let isStreaming: Bool
    let isLastAssistant: Bool
    let store: AppStore
    @State private var copied = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 6) {
                Text("HARNESS")
                    .font(HarnessTheme.label)
                    .tracking(1.1)
                    .foregroundStyle(HarnessTheme.amber)
                if isStreaming {
                    ProgressView()
                        .scaleEffect(0.55)
                        .tint(HarnessTheme.amber)
                }
            }

            ForEach(Array(MarkdownSplitter.split(text).enumerated()), id: \.offset) { _, segment in
                switch segment {
                case .prose(let prose):
                    ProseBlocksView(text: prose)
                case .code(let lang, let code):
                    CodeBlockCardView(language: lang, code: code)
                }
            }

            if !isStreaming && !text.isEmpty {
                HStack(spacing: 14) {
                    Button {
                        UIPasteboard.general.string = text
                        copied = true
                        Task {
                            try? await Task.sleep(nanoseconds: 1_400_000_000)
                            copied = false
                        }
                    } label: {
                        Label(copied ? "Copied" : "Copy", systemImage: copied ? "checkmark" : "doc.on.doc")
                            .font(.caption)
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(copied ? HarnessTheme.sage : HarnessTheme.dim)

                    if store.isVoiceAvailable {
                        let isSpeaking = store.speakingItemId == itemId
                        Button {
                            Task { await store.toggleSpeak(itemId: itemId, text: text) }
                        } label: {
                            Label(isSpeaking ? "Stop" : "Speak", systemImage: isSpeaking ? "stop.fill" : "speaker.wave.2")
                                .font(.caption)
                        }
                        .buttonStyle(.plain)
                        .foregroundStyle(isSpeaking ? HarnessTheme.amber : HarnessTheme.dim)
                    }

                    if isLastAssistant && !store.isStreaming && store.currentSessionId != nil {
                        Button {
                            Task { await store.rewindAndRetry() }
                        } label: {
                            Label("Retry", systemImage: "arrow.clockwise")
                                .font(.caption)
                        }
                        .buttonStyle(.plain)
                        .foregroundStyle(HarnessTheme.dim)

                        Button {
                            Task { await store.rewindAndEdit() }
                        } label: {
                            Label("Edit", systemImage: "pencil")
                                .font(.caption)
                        }
                        .buttonStyle(.plain)
                        .foregroundStyle(HarnessTheme.dim)
                    }
                }
                .padding(.top, 2)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, 6)
    }

}

struct CodeBlockCardView: View {
    let language: String
    let code: String
    @State private var copied = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text(language.isEmpty ? "CODE" : language.uppercased())
                    .font(HarnessTheme.monoSmall)
                    .foregroundStyle(HarnessTheme.dim)
                Spacer()
                Button {
                    UIPasteboard.general.string = code
                    copied = true
                    Task {
                        try? await Task.sleep(nanoseconds: 1_400_000_000)
                        copied = false
                    }
                } label: {
                    Label(copied ? "Copied" : "Copy", systemImage: copied ? "checkmark" : "doc.on.doc")
                        .font(.caption2)
                        .foregroundStyle(copied ? HarnessTheme.sage : HarnessTheme.dim)
                }
                .buttonStyle(.plain)
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 7)
            .background(HarnessTheme.panel2)

            Divider().background(HarnessTheme.line)

            ScrollView(.horizontal, showsIndicators: true) {
                Text(code)
                    .font(HarnessTheme.monoSmall)
                    .foregroundStyle(HarnessTheme.text)
                    .textSelection(.enabled)
                    .padding(10)
            }
        }
        .harnessCard(border: HarnessTheme.lineHot, background: HarnessTheme.ink)
    }
}

enum MarkdownSegment: Equatable {
    case prose(String)
    case code(language: String, content: String)
}

enum MarkdownSplitter {
    static func split(_ text: String) -> [MarkdownSegment] {
        var result: [MarkdownSegment] = []
        var proseLines: [String] = []
        var codeLines: [String] = []
        var inCode = false
        var currentLang = ""

        for rawLine in text.split(separator: "\n", omittingEmptySubsequences: false) {
            let line = String(rawLine)
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.hasPrefix("```") {
                if !inCode {
                    let prose = proseLines.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
                    if !prose.isEmpty { result.append(.prose(prose)) }
                    proseLines.removeAll()
                    currentLang = String(trimmed.dropFirst(3)).trimmingCharacters(in: .whitespaces)
                    inCode = true
                } else {
                    result.append(.code(language: currentLang, content: codeLines.joined(separator: "\n")))
                    codeLines.removeAll()
                    currentLang = ""
                    inCode = false
                }
            } else if inCode {
                codeLines.append(line)
            } else {
                proseLines.append(line)
            }
        }

        if inCode {
            result.append(.code(language: currentLang, content: codeLines.joined(separator: "\n")))
        } else {
            let prose = proseLines.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
            if !prose.isEmpty { result.append(.prose(prose)) }
        }
        return result.isEmpty ? [.prose(text)] : result
    }
}

// MARK: - Error & Cost Rows

struct ErrorCardView: View {
    let text: String

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: "exclamationmark.circle.fill")
                .font(.caption)
                .foregroundStyle(HarnessTheme.rust)
            Text(text)
                .font(HarnessTheme.monoSmall)
                .foregroundStyle(HarnessTheme.rust)
                .textSelection(.enabled)
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(HarnessTheme.rust.opacity(0.08))
        .overlay(RoundedRectangle(cornerRadius: 4).stroke(HarnessTheme.rust.opacity(0.4), lineWidth: 1))
    }
}

struct CostFooterView: View {
    let seconds: Double
    let tokens: Int
    let steps: Int

    var body: some View {
        let rate = seconds > 0.1 && tokens > 0 ? String(format: " · %.1f tok/s", Double(tokens) / seconds) : ""
        let stepLabel = steps == 1 ? "1 step" : "\(steps) steps"
        Text(String(format: "%.1fs · %d tokens%@ · %@", seconds, tokens, rate, stepLabel))
            .font(HarnessTheme.monoSmall)
            .foregroundStyle(HarnessTheme.faint)
            .padding(.top, 2)
    }
}
