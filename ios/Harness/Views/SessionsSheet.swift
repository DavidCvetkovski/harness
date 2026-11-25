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

    var body: some View {
        NavigationStack {
            List {
                if filtered.isEmpty {
                    ContentUnavailableView(
                        "No Conversations",
                        systemImage: "bubble.left.and.bubble.right",
                        description: Text("Start a new chat and it will be saved on your Mac.")
                    )
                    .listRowBackground(HarnessTheme.ink)
                } else {
                    ForEach(filtered) { session in
                        Button {
                            dismiss()
                            Task { await store.openSession(id: session.id) }
                        } label: {
                            HStack(spacing: 10) {
                                Circle()
                                    .fill(session.id == store.currentSessionId ? HarnessTheme.amber : HarnessTheme.lineHot)
                                    .frame(width: 7, height: 7)
                                VStack(alignment: .leading, spacing: 3) {
                                    Text(session.title.isEmpty ? "Untitled chat" : session.title)
                                        .font(.subheadline.weight(.medium))
                                        .foregroundStyle(HarnessTheme.text)
                                        .lineLimit(1)
                                    Text("\(session.turns) turn\(session.turns == 1 ? "" : "s") · \(shortDate(session.updated))")
                                        .font(HarnessTheme.monoSmall)
                                        .foregroundStyle(HarnessTheme.dim)
                                }
                                Spacer()
                            }
                            .padding(.vertical, 4)
                        }
                        .listRowBackground(HarnessTheme.panel)
                    }
                    .onDelete { indices in
                        let ids = indices.map { filtered[$0].id }
                        Task {
                            for id in ids {
                                await store.deleteSession(id: id)
                            }
                        }
                    }
                }
            }
            .scrollContentBackground(.hidden)
            .background(HarnessTheme.ink)
            .searchable(text: $query, prompt: "Filter conversations")
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

    private func shortDate(_ iso: String) -> String {
        if iso.count >= 16 {
            return String(iso.prefix(16)).replacingOccurrences(of: "T", with: " ")
        }
        return iso
    }
}
