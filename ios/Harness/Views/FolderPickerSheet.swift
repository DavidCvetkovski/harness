import SwiftUI

/// Picks the project folder: the folder Harness treats as "here". Walks the
/// Mac's folders one level at a time through `/api/folders`, the same way the
/// web app's picker does.
struct FolderPickerSheet: View {
    @Bindable var store: AppStore
    @Environment(\.dismiss) private var dismiss

    @State private var listing: FoldersResponse?
    @State private var loadError: String?
    @State private var isLoading = false
    @State private var isSaving = false

    private let shortcuts: [(label: String, path: String, icon: String)] = [
        ("Home", "~", "house"),
        ("Desktop", "~/Desktop", "menubar.dock.rectangle"),
        ("Documents", "~/Documents", "doc"),
        ("Downloads", "~/Downloads", "arrow.down.circle"),
    ]

    var body: some View {
        NavigationStack {
            List {
                Section {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("CURRENT PROJECT")
                            .font(HarnessTheme.label)
                            .tracking(1.0)
                            .foregroundStyle(HarnessTheme.dim)
                        Text(store.projectDir.isEmpty ? "None — paths must be absolute" : store.projectDir)
                            .font(HarnessTheme.monoSmall)
                            .foregroundStyle(store.projectDir.isEmpty ? HarnessTheme.faint : HarnessTheme.amber)
                            .textSelection(.enabled)
                        Text("Harness treats this folder as \u{201C}here\u{201D}: relative paths and \u{201C}this folder\u{201D} in your messages point to it.")
                            .font(.caption)
                            .foregroundStyle(HarnessTheme.dim)
                    }
                    .padding(.vertical, 4)

                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 8) {
                            ForEach(shortcuts, id: \.path) { shortcut in
                                Button {
                                    Task { await load(shortcut.path) }
                                } label: {
                                    Label(shortcut.label, systemImage: shortcut.icon)
                                        .font(.caption.weight(.medium))
                                        .foregroundStyle(HarnessTheme.text)
                                        .padding(.horizontal, 10)
                                        .padding(.vertical, 6)
                                        .harnessCard(border: HarnessTheme.lineHot, background: HarnessTheme.panel2)
                                }
                                .buttonStyle(.plain)
                            }
                        }
                    }
                    .listRowInsets(EdgeInsets(top: 4, leading: 16, bottom: 8, trailing: 16))
                }
                .listRowBackground(HarnessTheme.panel)

                Section {
                    if let listing {
                        if listing.path != "/" {
                            Button {
                                Task { await load(listing.parent) }
                            } label: {
                                Label("Up one level", systemImage: "arrow.turn.left.up")
                                    .font(.subheadline)
                                    .foregroundStyle(HarnessTheme.dim)
                            }
                        }
                        if listing.folders.isEmpty {
                            Text("No folders inside.")
                                .font(.subheadline)
                                .foregroundStyle(HarnessTheme.faint)
                        }
                        ForEach(listing.folders, id: \.self) { name in
                            Button {
                                Task { await load(join(listing.path, name)) }
                            } label: {
                                HStack(spacing: 10) {
                                    Image(systemName: "folder.fill")
                                        .foregroundStyle(HarnessTheme.amberDim)
                                    Text(name)
                                        .font(.subheadline)
                                        .foregroundStyle(HarnessTheme.text)
                                        .lineLimit(1)
                                    Spacer()
                                    Image(systemName: "chevron.right")
                                        .font(.caption2)
                                        .foregroundStyle(HarnessTheme.faint)
                                }
                            }
                        }
                    } else if let loadError {
                        Text(loadError)
                            .font(.subheadline)
                            .foregroundStyle(HarnessTheme.rust)
                    } else {
                        HStack {
                            Spacer()
                            ProgressView().tint(HarnessTheme.amber)
                            Spacer()
                        }
                    }
                } header: {
                    Text(listing?.path ?? "Loading…")
                        .font(HarnessTheme.monoSmall)
                        .foregroundStyle(HarnessTheme.dim)
                        .textCase(nil)
                        .lineLimit(1)
                        .truncationMode(.head)
                }
                .listRowBackground(HarnessTheme.panel)
            }
            .scrollContentBackground(.hidden)
            .background(HarnessTheme.ink)
            .overlay(alignment: .top) {
                if isLoading && listing != nil {
                    ProgressView().tint(HarnessTheme.amber).padding(.top, 4)
                }
            }
            .safeAreaInset(edge: .bottom) { footer }
            .navigationTitle("Project Folder")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                if !store.projectDir.isEmpty {
                    ToolbarItem(placement: .primaryAction) {
                        Button("Clear", role: .destructive) {
                            Task { await choose("") }
                        }
                    }
                }
            }
            .task {
                await load(store.projectDir.isEmpty ? "~" : store.projectDir)
            }
        }
    }

    private var footer: some View {
        Button {
            if let path = listing?.path { Task { await choose(path) } }
        } label: {
            HStack(spacing: 8) {
                if isSaving {
                    ProgressView().tint(HarnessTheme.ink)
                } else {
                    Image(systemName: "checkmark")
                }
                Text(listing.map { "Use \u{201C}\(lastComponent($0.path))\u{201D}" } ?? "Use This Folder")
                    .lineLimit(1)
            }
            .font(.headline)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 13)
            .background(listing == nil ? HarnessTheme.panel2 : HarnessTheme.amber)
            .foregroundStyle(listing == nil ? HarnessTheme.faint : HarnessTheme.ink)
            .clipShape(RoundedRectangle(cornerRadius: 8))
        }
        .buttonStyle(.plain)
        .disabled(listing == nil || isSaving)
        .padding(.horizontal, 16)
        .padding(.top, 10)
        .padding(.bottom, 6)
        .background(HarnessTheme.ink)
    }

    private func load(_ path: String) async {
        isLoading = true
        defer { isLoading = false }
        do {
            listing = try await store.browseFolders(path: path)
            loadError = nil
        } catch {
            if listing == nil { loadError = error.localizedDescription }
        }
    }

    private func choose(_ path: String) async {
        isSaving = true
        await store.setProjectDir(path)
        isSaving = false
        if store.projectDir == path { dismiss() }
    }

    private func join(_ base: String, _ name: String) -> String {
        base.hasSuffix("/") ? base + name : base + "/" + name
    }

    private func lastComponent(_ path: String) -> String {
        path == "/" ? "/" : (path.split(separator: "/").last.map(String.init) ?? path)
    }
}
