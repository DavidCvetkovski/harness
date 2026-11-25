import SwiftUI

/// Block-level Markdown for replies. SwiftUI's `AttributedString(markdown:)`
/// only does inline styling here, so headings came out as literal "## Plan"
/// and lists as raw "- item" lines. This handles the blocks models actually
/// write — headings, bullet and numbered lists, quotes, tables, rules — and
/// leaves inline styling (bold, code, links) to AttributedString.
enum ProseBlock: Equatable {
    case heading(level: Int, text: String)
    case listItem(marker: String, text: String, depth: Int)
    case paragraph(String)
    case quote(String)
    case table([[String]])
    case rule

    static func parse(_ text: String) -> [ProseBlock] {
        var blocks: [ProseBlock] = []
        var paragraph: [String] = []
        var quote: [String] = []
        var table: [[String]] = []

        func flushParagraph() {
            if !paragraph.isEmpty { blocks.append(.paragraph(paragraph.joined(separator: "\n"))) }
            paragraph.removeAll()
        }
        func flushQuote() {
            if !quote.isEmpty { blocks.append(.quote(quote.joined(separator: "\n"))) }
            quote.removeAll()
        }
        func flushTable() {
            if !table.isEmpty { blocks.append(.table(table)) }
            table.removeAll()
        }
        func flushAll() { flushParagraph(); flushQuote(); flushTable() }

        for raw in text.split(separator: "\n", omittingEmptySubsequences: false) {
            let line = String(raw)
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            let indent = line.prefix(while: { $0 == " " || $0 == "\t" }).count

            if trimmed.isEmpty {
                flushAll()
                continue
            }
            if trimmed.hasPrefix("|") && trimmed.hasSuffix("|") && trimmed.count > 1 {
                flushParagraph(); flushQuote()
                let cells = trimmed.dropFirst().dropLast()
                    .split(separator: "|", omittingEmptySubsequences: false)
                    .map { $0.trimmingCharacters(in: .whitespaces) }
                // The |---|:--:| line under the header carries no content.
                if !cells.allSatisfy({ $0.allSatisfy { "-: ".contains($0) } }) {
                    table.append(cells)
                }
                continue
            }
            flushTable()
            if trimmed.hasPrefix(">") {
                flushParagraph()
                quote.append(String(trimmed.dropFirst()).trimmingCharacters(in: .whitespaces))
                continue
            }
            flushQuote()
            if let heading = headingLevel(trimmed) {
                flushParagraph()
                blocks.append(.heading(level: heading.level, text: heading.text))
            } else if trimmed.count >= 3, Set(trimmed.replacingOccurrences(of: " ", with: "")).count == 1,
                      let mark = trimmed.first, "-*_".contains(mark) {
                flushParagraph()
                blocks.append(.rule)
            } else if let item = listItem(trimmed) {
                flushParagraph()
                blocks.append(.listItem(marker: item.marker, text: item.text, depth: min(indent / 2, 3)))
            } else {
                paragraph.append(trimmed)
            }
        }
        flushAll()
        return blocks
    }

    private static func headingLevel(_ line: String) -> (level: Int, text: String)? {
        let hashes = line.prefix(while: { $0 == "#" }).count
        guard (1...6).contains(hashes), line.dropFirst(hashes).first == " " else { return nil }
        return (hashes, String(line.dropFirst(hashes + 1)).trimmingCharacters(in: .whitespaces))
    }

    private static func listItem(_ line: String) -> (marker: String, text: String)? {
        for bullet in ["- ", "* ", "+ ", "• "] where line.hasPrefix(bullet) {
            var text = String(line.dropFirst(bullet.count))
            if text.hasPrefix("[ ] ") { return ("☐", String(text.dropFirst(4))) }
            if text.hasPrefix("[x] ") || text.hasPrefix("[X] ") {
                text = String(text.dropFirst(4))
                return ("☑", text)
            }
            return ("•", text)
        }
        let digits = line.prefix(while: \.isNumber)
        if !digits.isEmpty, digits.count <= 3 {
            let rest = line.dropFirst(digits.count)
            if rest.hasPrefix(". ") || rest.hasPrefix(") ") {
                return ("\(digits).", String(rest.dropFirst(2)))
            }
        }
        return nil
    }
}

struct ProseBlocksView: View {
    let text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(Array(ProseBlock.parse(text).enumerated()), id: \.offset) { _, block in
                view(for: block)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder
    private func view(for block: ProseBlock) -> some View {
        switch block {
        case .heading(let level, let text):
            Text(Self.inline(text))
                .font(.system(size: level <= 1 ? 20 : level == 2 ? 18 : 16.5,
                              weight: .semibold, design: .serif))
                .foregroundStyle(HarnessTheme.text)
                .padding(.top, 4)
                .textSelection(.enabled)
        case .listItem(let marker, let text, let depth):
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(marker)
                    .font(marker.count > 1 ? HarnessTheme.monoSmall : HarnessTheme.reading)
                    .foregroundStyle(HarnessTheme.amberDim)
                    .frame(minWidth: 14, alignment: .trailing)
                Text(Self.inline(text))
                    .font(HarnessTheme.reading)
                    .foregroundStyle(HarnessTheme.text)
                    .lineSpacing(3)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .padding(.leading, CGFloat(depth) * 16)
        case .paragraph(let text):
            Text(Self.inline(text))
                .font(HarnessTheme.reading)
                .foregroundStyle(HarnessTheme.text)
                .lineSpacing(4)
                .textSelection(.enabled)
        case .quote(let text):
            Text(Self.inline(text))
                .font(HarnessTheme.reading.italic())
                .foregroundStyle(HarnessTheme.dim)
                .lineSpacing(3)
                .padding(.leading, 12)
                .overlay(alignment: .leading) {
                    Rectangle().fill(HarnessTheme.lineHot).frame(width: 2)
                }
                .textSelection(.enabled)
        case .table(let rows):
            ScrollView(.horizontal, showsIndicators: false) {
                Text(Self.alignedTable(rows))
                    .font(HarnessTheme.monoSmall)
                    .foregroundStyle(HarnessTheme.text)
                    .textSelection(.enabled)
                    .padding(10)
            }
            .harnessCard(border: HarnessTheme.line, background: HarnessTheme.panel)
        case .rule:
            Rectangle().fill(HarnessTheme.line).frame(height: 1).padding(.vertical, 4)
        }
    }

    static func inline(_ text: String) -> AttributedString {
        let options = AttributedString.MarkdownParsingOptions(
            interpretedSyntax: .inlineOnlyPreservingWhitespace)
        return (try? AttributedString(markdown: text, options: options)) ?? AttributedString(text)
    }

    /// Pads every column to its widest cell, header underlined with ─.
    static func alignedTable(_ rows: [[String]]) -> String {
        let plain = rows.map { $0.map { $0.replacingOccurrences(of: "**", with: "")
            .replacingOccurrences(of: "`", with: "") } }
        let columns = plain.map(\.count).max() ?? 0
        var widths = Array(repeating: 0, count: columns)
        for row in plain {
            for (i, cell) in row.enumerated() { widths[i] = max(widths[i], cell.count) }
        }
        var lines: [String] = []
        for (index, row) in plain.enumerated() {
            let padded = (0..<columns).map { i -> String in
                let cell = i < row.count ? row[i] : ""
                return cell.padding(toLength: widths[i], withPad: " ", startingAt: 0)
            }
            lines.append(padded.joined(separator: "  "))
            if index == 0 && plain.count > 1 {
                lines.append(widths.map { String(repeating: "─", count: $0) }.joined(separator: "  "))
            }
        }
        return lines.joined(separator: "\n")
    }
}
