import SwiftUI

struct SessionsSheet: View {
    @Bindable var store: AppStore
    @Environment(\.dismiss) private var dismiss
    @State private var query = ""

    private var filtered: [SessionSummary] {
        let q = query.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !q.isEmpty else { return store.sessions }
        return store.sessions.filter { $0.title.lowercased().contains(q) || $0.id.lowercased().contains(q) }
    }

    /// Today / Yesterday / Previous 7 Days / Older, newest first within each —
    /// the server already sorts by `updated`, so order is kept as given.
    private var sections: [(title: String, sessions: [SessionSummary])] {
        let calendar = Calendar.current
        let now = Date()
        var buckets: [(String, [SessionSummary])] = [
            ("Today", []), ("Yesterday", []), ("Previous 7 Days", []), ("Older", []),
        ]
        for session in filtered {
            let index: Int
            if let date = SessionDates.parse(session.updated) {
                if calendar.isDateInToday(date) {
                    index = 0
                } else if calendar.isDateInYesterday(date) {
                    index = 1
                } else if let days = calendar.dateComponents([.day], from: date, to: now).day, days < 7 {
                    index = 2
                } else {
                    index = 3
                }
            } else {
                index = 3
            }
            buckets[index].1.append(session)
        }
        return buckets.filter { !$0.1.isEmpty }.map { (title: $0.0, sessions: $0.1) }
    }

    var body: some View {
        NavigationStack {
            List {
                if filtered.isEmpty {
                    ContentUnavailableView(
                        query.isEmpty ? "No Conversations" : "No Matches",
                        systemImage: query.isEmpty ? "bubble.left.and.bubble.right" : "magnifyingglass",
                        description: Text(query.isEmpty
                                          ? "Start a new chat and it will be saved on your Mac."
                                          : "Nothing in your history matches \u{201C}\(query)\u{201D}.")
                    )
                    .listRowBackground(HarnessTheme.ink)
                } else {
                    ForEach(sections, id: \.title) { section in
                        Section {
                            ForEach(section.sessions) { session in
                                row(for: session)
                                    .swipeActions(edge: .trailing, allowsFullSwipe: true) {
                                        Button(role: .destructive) {
                                            Task { await store.deleteSession(id: session.id) }
                                        } label: {
                                            Label("Delete", systemImage: "trash")
                                        }
                                    }
                            }
                        } header: {
                            Text(section.title.uppercased())
                                .font(HarnessTheme.label)
                                .tracking(1.0)
                                .foregroundStyle(HarnessTheme.dim)
                        }
                    }
                }
            }
            .scrollContentBackground(.hidden)
            .background(HarnessTheme.ink)
            .searchable(text: $query, prompt: "Search conversations")
            .refreshable { await store.refreshSessionsList() }
            .navigationTitle("History")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Done") { dismiss() }
                }
                ToolbarItem(placement: .primaryAction) {
                    Button {
                        store.newChat()
                        dismiss()
                    } label: {
                        Label("New Chat", systemImage: "square.and.pencil")
                    }
                }
            }
        }
    }

    private func row(for session: SessionSummary) -> some View {
        let isOpen = session.id == store.currentSessionId
        return Button {
            dismiss()
            if !isOpen {
                Task { await store.openSession(id: session.id) }
            }
        } label: {
            HStack(alignment: .top, spacing: 10) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(session.title.isEmpty ? "Untitled chat" : session.title)
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(HarnessTheme.text)
                        .lineLimit(2)
                        .multilineTextAlignment(.leading)
                    HStack(spacing: 6) {
                        Text(SessionDates.label(for: session.updated))
                        Text("·")
                        Text("\(session.turns) \(session.turns == 1 ? "message" : "messages")")
                    }
                    .font(HarnessTheme.monoSmall)
                    .foregroundStyle(HarnessTheme.dim)
                }
                Spacer(minLength: 6)
                if isOpen {
                    Text("OPEN")
                        .font(.system(size: 9.5, weight: .semibold, design: .monospaced))
                        .foregroundStyle(HarnessTheme.amber)
                        .padding(.horizontal, 5)
                        .padding(.vertical, 1.5)
                        .background(HarnessTheme.amber.opacity(0.12))
                        .overlay(RoundedRectangle(cornerRadius: 3)
                            .stroke(HarnessTheme.amber.opacity(0.35), lineWidth: 0.75))
                }
            }
            .padding(.vertical, 4)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .listRowBackground(isOpen ? HarnessTheme.panel2 : HarnessTheme.panel)
    }
}

/// Session timestamps come from the Mac as local ISO time without a zone
/// ("2026-11-25T14:53:12"), so they're read in the phone's current zone —
/// right whenever the phone and the Mac agree, which is the usual case.
enum SessionDates {
    private static let parser: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
        return f
    }()

    private static let parserNoSeconds: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd'T'HH:mm"
        return f
    }()

    static func parse(_ raw: String) -> Date? {
        parser.date(from: String(raw.prefix(19))) ?? parserNoSeconds.date(from: String(raw.prefix(16)))
    }

    /// "14:53" today, "Yesterday", "Monday" this week, "12 Nov" this year,
    /// "12 Nov 2025" before that.
    static func label(for raw: String, now: Date = Date(), calendar: Calendar = .current) -> String {
        guard let date = parse(raw) else { return raw }
        if calendar.isDateInToday(date) {
            return date.formatted(date: .omitted, time: .shortened)
        }
        if calendar.isDateInYesterday(date) {
            return "Yesterday"
        }
        if let days = calendar.dateComponents([.day], from: date, to: now).day, days < 7 {
            return date.formatted(.dateTime.weekday(.wide))
        }
        if calendar.isDate(date, equalTo: now, toGranularity: .year) {
            return date.formatted(.dateTime.day().month(.abbreviated))
        }
        return date.formatted(.dateTime.day().month(.abbreviated).year())
    }
}
