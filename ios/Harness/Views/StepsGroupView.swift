import SwiftUI

/// What the transcript actually draws: most items stand alone, but a run of
/// tool calls and the thinking between them folds into one "steps" card, so a
/// five-command search reads as one line instead of five full-width cards.
enum TranscriptRow: Identifiable {
    case single(TranscriptItem)
    case steps(id: String, items: [TranscriptItem])

    var id: String {
        switch self {
        case .single(let item): return item.id
        case .steps(let id, _): return id
        }
    }

    /// Groups each maximal run of `.thinking` / `.tool` items that contains at
    /// least one tool call and more than one item. Approvals, errors and replies
    /// break a run, so anything that needs the user stays in plain view. The
    /// group's id comes from its first item, so it stays put as the run grows.
    static func build(from transcript: [TranscriptItem]) -> [TranscriptRow] {
        var rows: [TranscriptRow] = []
        var run: [TranscriptItem] = []

        func flush() {
            let toolCount = run.filter(\.isTool).count
            if toolCount >= 1 && run.count >= 2, let first = run.first {
                rows.append(.steps(id: "steps-\(first.id)", items: run))
            } else {
                rows.append(contentsOf: run.map(TranscriptRow.single))
            }
            run.removeAll()
        }

        for item in transcript {
            if item.isTool || item.isThinking {
                run.append(item)
            } else {
                flush()
                rows.append(.single(item))
            }
        }
        flush()
        return rows
    }
}

extension TranscriptItem {
    var isTool: Bool {
        if case .tool = self { return true }
        return false
    }

    var isThinking: Bool {
        if case .thinking = self { return true }
        return false
    }
}

struct StepsGroupView<Content: View>: View {
    let items: [TranscriptItem]
    /// The turn is still streaming and this is the newest group.
    let isLive: Bool
    let isExpanded: Bool
    let onToggle: () -> Void
    @ViewBuilder let content: (TranscriptItem) -> Content

    private var tools: [(name: String, args: [String: String], isError: Bool, isRunning: Bool)] {
        items.compactMap {
            if case .tool(_, let name, let args, _, let isError, let isRunning, _) = $0 {
                return (name, args, isError, isRunning)
            }
            return nil
        }
    }

    private var thoughtCount: Int { items.filter(\.isThinking).count }
    private var failedCount: Int { tools.filter(\.isError).count }

    private var title: String {
        if isLive {
            if let running = tools.last(where: \.isRunning) {
                return "Running \(running.name)"
            }
            if case .thinking = items.last { return "Thinking" }
            return "Working"
        }
        var parts = ["\(tools.count) \(tools.count == 1 ? "step" : "steps")"]
        if thoughtCount > 0 {
            parts.append("\(thoughtCount) \(thoughtCount == 1 ? "thought" : "thoughts")")
        }
        return parts.joined(separator: " · ")
    }

    /// "run_command ×4 · read_file" — which tools, in the order first used.
    private var toolSummary: String {
        if isLive, let last = tools.last {
            let detail = Self.argSummary(last.args)
            return detail.isEmpty ? last.name : detail
        }
        var order: [String] = []
        var counts: [String: Int] = [:]
        for tool in tools {
            if counts[tool.name] == nil { order.append(tool.name) }
            counts[tool.name, default: 0] += 1
        }
        return order.map { counts[$0, default: 0] > 1 ? "\($0) ×\(counts[$0]!)" : $0 }
            .joined(separator: " · ")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Button(action: onToggle) {
                HStack(spacing: 9) {
                    statusMark
                    VStack(alignment: .leading, spacing: 2) {
                        HStack(spacing: 6) {
                            Text(title)
                                .font(HarnessTheme.monoSmall.weight(.semibold))
                                .foregroundStyle(isLive ? HarnessTheme.amber : HarnessTheme.text)
                            if failedCount > 0 {
                                Text("\(failedCount) failed")
                                    .font(HarnessTheme.monoSmall)
                                    .foregroundStyle(HarnessTheme.rust)
                            }
                        }
                        if !toolSummary.isEmpty {
                            Text(toolSummary)
                                .font(HarnessTheme.monoSmall)
                                .foregroundStyle(HarnessTheme.dim)
                                .lineLimit(1)
                                .truncationMode(.middle)
                        }
                    }
                    Spacer(minLength: 6)
                    Image(systemName: isExpanded ? "chevron.up" : "chevron.down")
                        .font(.caption2)
                        .foregroundStyle(HarnessTheme.faint)
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 9)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .harnessCard(border: isLive ? HarnessTheme.amberDim : HarnessTheme.line,
                         background: HarnessTheme.panel)
            .accessibilityLabel(title)
            .accessibilityHint(isExpanded ? "Hides the steps" : "Shows each step")

            if isExpanded {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(items) { item in
                        content(item)
                    }
                }
                .padding(.leading, 12)
                .overlay(alignment: .leading) {
                    Rectangle().fill(HarnessTheme.line).frame(width: 1).padding(.leading, 4)
                }
            } else if isLive, let last = items.last {
                // Collapsed but still working: keep the step in progress in
                // view, so the run is never a silent spinner.
                content(last)
                    .padding(.leading, 12)
                    .transition(.opacity)
            }
        }
        .animation(.easeOut(duration: 0.15), value: isExpanded)
    }

    @ViewBuilder
    private var statusMark: some View {
        if isLive {
            ProgressView()
                .scaleEffect(0.65)
                .tint(HarnessTheme.amber)
                .frame(width: 14, height: 14)
        } else {
            Image(systemName: failedCount > 0 ? "exclamationmark.circle" : "checkmark.circle")
                .font(.caption)
                .foregroundStyle(failedCount > 0 ? HarnessTheme.rust : HarnessTheme.sage)
                .frame(width: 14, height: 14)
        }
    }

    static func argSummary(_ args: [String: String]) -> String {
        if let value = args["command"] ?? args["path"] ?? args["query"] ?? args["url"] {
            return value
        }
        return args.sorted(by: { $0.key < $1.key }).map { "\($0.key)=\($0.value)" }
            .joined(separator: " ")
    }
}
