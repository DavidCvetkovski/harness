import PhotosUI
import SwiftUI
import UIKit

struct MainChatView: View {
    @Bindable var store: AppStore
    @State private var selectedPhotoItems: [PhotosPickerItem] = []
    @State private var showPhotoPicker = false
    @State private var showCamera = false
    @State private var exportText: String?
    @State private var showExportSheet = false
    @FocusState private var composerFocused: Bool

    private let starterPrompts: [(title: String, prompt: String)] = [
        ("Mac health", "Check my Mac's memory, CPU load, and free disk space."),
        ("Recent files", "List the most recent files in my Downloads folder."),
        ("Try approval", "Write a small Python script rename_by_date.py to preview date-prefixed filenames."),
    ]

    var body: some View {
        VStack(spacing: 0) {
            headerBar
            Divider().background(HarnessTheme.line)

            if let banner = store.bannerMessage {
                HStack(spacing: 8) {
                    Text(banner)
                        .font(.caption)
                        .foregroundStyle(HarnessTheme.amber)
                    Spacer()
                    Button {
                        store.bannerMessage = nil
                    } label: {
                        Image(systemName: "xmark")
                            .font(.caption2)
                            .foregroundStyle(HarnessTheme.dim)
                    }
                    .buttonStyle(.plain)
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 8)
                .background(HarnessTheme.amber.opacity(0.1))
                Divider().background(HarnessTheme.line)
            }

            transcriptArea
            Divider().background(HarnessTheme.line)
            composerArea
        }
        .background(HarnessTheme.ink)
        .sheet(isPresented: $store.showSessionsSheet) {
            SessionsSheet(store: store)
        }
        .sheet(isPresented: $store.showSettingsSheet) {
            SettingsSheet(store: store)
        }
        .photosPicker(
            isPresented: $showPhotoPicker,
            selection: $selectedPhotoItems,
            maxSelectionCount: max(1, store.maxImagesPerMessage - store.pendingAttachments.count),
            matching: .images
        )
        .onChange(of: selectedPhotoItems) { _, items in
            guard !items.isEmpty else { return }
            let picked = items
            selectedPhotoItems = []
            Task {
                for item in picked {
                    if let data = try? await item.loadTransferable(type: Data.self) {
                        await store.attachImageData(data)
                    }
                }
            }
        }
        .fullScreenCover(isPresented: $showCamera) {
            CameraPickerView { image in
                showCamera = false
                if let jpeg = image.jpegData(compressionQuality: 0.85) {
                    Task { await store.attachImageData(jpeg) }
                }
            } onCancel: {
                showCamera = false
            }
            .ignoresSafeArea()
        }
        .sheet(isPresented: $showExportSheet) {
            if let exportText {
                NavigationStack {
                    ScrollView {
                        Text(exportText)
                            .font(HarnessTheme.monoSmall)
                            .foregroundStyle(HarnessTheme.text)
                            .textSelection(.enabled)
                            .padding()
                    }
                    .background(HarnessTheme.ink)
                    .navigationTitle("Markdown Export")
                    .navigationBarTitleDisplayMode(.inline)
                    .toolbar {
                        ToolbarItem(placement: .cancellationAction) {
                            Button("Done") { showExportSheet = false }
                        }
                        ToolbarItem(placement: .primaryAction) {
                            ShareLink(item: exportText) {
                                Label("Share", systemImage: "square.and.arrow.up")
                            }
                        }
                    }
                }
            }
        }
    }

    // MARK: - Top Instrument Bar

    private var headerBar: some View {
        HStack(spacing: 10) {
            Button {
                store.showSessionsSheet = true
                Task { await store.refreshSessionsList() }
            } label: {
                Image(systemName: "sidebar.left")
                    .font(.body)
                    .foregroundStyle(HarnessTheme.dim)
                    .frame(width: 34, height: 34)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Sessions")

            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Circle()
                        .fill(statusLEDColor)
                        .frame(width: 7, height: 7)
                    Text(store.activeModelName)
                        .font(.system(size: 13, weight: .semibold, design: .monospaced))
                        .foregroundStyle(HarnessTheme.text)
                        .lineLimit(1)
                        .truncationMode(.tail)
                }
                HStack(spacing: 6) {
                    if store.isDemoMode {
                        pill("DEMO", color: HarnessTheme.sage)
                    } else if store.capabilities?.tls == true {
                        pill("HTTPS", color: HarnessTheme.sage)
                    } else {
                        pill((store.capabilities?.mode ?? "remote").uppercased(), color: HarnessTheme.dim)
                    }
                    if store.isVisionAvailable {
                        pill("VISION", color: HarnessTheme.amber)
                    }
                    if let status = store.statusText {
                        Text(status)
                            .font(HarnessTheme.monoSmall)
                            .foregroundStyle(HarnessTheme.amber)
                            .lineLimit(1)
                    }
                }
            }

            Spacer()

            if store.hasUserTurns {
                Button {
                    Task {
                        exportText = await store.exportCurrentMarkdown()
                        showExportSheet = true
                    }
                } label: {
                    Image(systemName: "square.and.arrow.up")
                        .font(.subheadline)
                        .foregroundStyle(HarnessTheme.dim)
                        .frame(width: 32, height: 34)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Export transcript")
            }

            Button {
                store.newChat()
            } label: {
                Image(systemName: "square.and.pencil")
                    .font(.body)
                    .foregroundStyle(HarnessTheme.dim)
                    .frame(width: 34, height: 34)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("New chat")

            Button {
                store.showSettingsSheet = true
                Task { await store.refreshAll() }
            } label: {
                Image(systemName: "slider.horizontal.3")
                    .font(.body)
                    .foregroundStyle(HarnessTheme.dim)
                    .frame(width: 34, height: 34)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Settings")
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(HarnessTheme.panel)
    }

    private var statusLEDColor: Color {
        if store.isStreaming || store.isSwitchingModel { return HarnessTheme.amber }
        return store.isModelLoaded ? HarnessTheme.sage : HarnessTheme.rust
    }

    private func pill(_ text: String, color: Color) -> some View {
        Text(text)
            .font(.system(size: 9.5, weight: .semibold, design: .monospaced))
            .foregroundStyle(color)
            .padding(.horizontal, 5)
            .padding(.vertical, 1.5)
            .background(color.opacity(0.12))
            .overlay(RoundedRectangle(cornerRadius: 3).stroke(color.opacity(0.35), lineWidth: 0.75))
    }

    // MARK: - Transcript

    private var transcriptArea: some View {
        ScrollViewReader { proxy in
            ScrollView {
                if store.transcript.isEmpty {
                    emptyStateView
                } else {
                    LazyVStack(alignment: .leading, spacing: 12) {
                        let lastAssistantId = store.transcript.last(where: {
                            if case .assistant = $0 { return true }
                            return false
                        })?.id

                        ForEach(store.transcript) { item in
                            rowView(for: item, lastAssistantId: lastAssistantId)
                                .id(item.id)
                        }
                        Color.clear.frame(height: 8).id("bottom-anchor")
                    }
                    .padding(.horizontal, 16)
                    .padding(.vertical, 14)
                }
            }
            .scrollDismissesKeyboard(.interactively)
            .onChange(of: store.transcript.count) { _, _ in
                withAnimation(.easeOut(duration: 0.15)) {
                    proxy.scrollTo("bottom-anchor", anchor: .bottom)
                }
            }
            .onChange(of: store.transcript.last) { _, _ in
                proxy.scrollTo("bottom-anchor", anchor: .bottom)
            }
        }
    }

    @ViewBuilder
    private func rowView(for item: TranscriptItem, lastAssistantId: String?) -> some View {
        switch item {
        case .user(_, let text, let urls, let local):
            UserTurnView(store: store, text: text, imageUrls: urls, localImages: local)
        case .thinking(_, let text, let started, let finished, let expanded):
            ThinkingCardView(text: text, startedAt: started, finishedAt: finished, isExpanded: expanded) {
                store.toggleExpanded(itemId: item.id)
            }
        case .tool(_, let name, let args, let out, let isErr, let isRun, let expanded):
            ToolCardView(name: name, args: args, output: out, isError: isErr, isRunning: isRun, isExpanded: expanded) {
                store.toggleExpanded(itemId: item.id)
            }
        case .approval(let apId, let apItem, let status):
            ApprovalCardView(item: apItem, status: status) { allow, always in
                Task { await store.respondToApproval(id: apId, allow: allow, always: always) }
            }
        case .assistant(_, let text, let streaming):
            AssistantTurnView(
                itemId: item.id,
                text: text,
                isStreaming: streaming,
                isLastAssistant: item.id == lastAssistantId,
                store: store
            )
        case .error(_, let text):
            ErrorCardView(text: text)
        case .cost(_, let seconds, let tokens, let steps):
            CostFooterView(seconds: seconds, tokens: tokens, steps: steps)
        }
    }

    private var emptyStateView: some View {
        VStack(alignment: .leading, spacing: 18) {
            Spacer().frame(height: 36)
            HStack(spacing: 12) {
                HarnessMarkView(size: 40)
                VStack(alignment: .leading, spacing: 2) {
                    Text(store.isDemoMode ? "Offline Demo Mode" : "Ready on your Mac")
                        .font(.headline)
                        .foregroundStyle(HarnessTheme.text)
                    Text(store.isVisionAvailable
                         ? "Ask anything, run local tools, or attach a photo."
                         : "Ask anything or run local tools on your Mac.")
                        .font(.caption)
                        .foregroundStyle(HarnessTheme.dim)
                }
            }

            VStack(spacing: 8) {
                ForEach(starterPrompts, id: \.title) { starter in
                    Button {
                        store.composerText = starter.prompt
                        Task { await store.sendMessage() }
                    } label: {
                        HStack {
                            VStack(alignment: .leading, spacing: 3) {
                                Text(starter.title.uppercased())
                                    .font(HarnessTheme.label)
                                    .tracking(1.0)
                                    .foregroundStyle(HarnessTheme.amber)
                                Text(starter.prompt)
                                    .font(.subheadline)
                                    .foregroundStyle(HarnessTheme.text)
                                    .multilineTextAlignment(.leading)
                            }
                            Spacer()
                            Image(systemName: "arrow.up.right")
                                .font(.caption)
                                .foregroundStyle(HarnessTheme.faint)
                        }
                        .padding(12)
                        .harnessCard(border: HarnessTheme.line, background: HarnessTheme.panel)
                    }
                    .buttonStyle(.plain)
                }
            }
        }
        .padding(20)
    }

    // MARK: - Composer

    private var composerArea: some View {
        VStack(spacing: 8) {
            if !store.pendingAttachments.isEmpty || store.isUploadingImage {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 8) {
                        ForEach(store.pendingAttachments) { att in
                            ZStack(alignment: .topTrailing) {
                                if let uiImg = UIImage(data: att.previewData) {
                                    Image(uiImage: uiImg)
                                        .resizable()
                                        .scaledToFill()
                                        .frame(width: 60, height: 60)
                                        .clipped()
                                        .clipShape(RoundedRectangle(cornerRadius: 4))
                                        .overlay(RoundedRectangle(cornerRadius: 4).stroke(HarnessTheme.lineHot, lineWidth: 1))
                                }
                                Button {
                                    store.removeAttachment(id: att.id)
                                } label: {
                                    Image(systemName: "xmark.circle.fill")
                                        .font(.caption)
                                        .foregroundStyle(HarnessTheme.text)
                                        .background(Circle().fill(HarnessTheme.ink))
                                }
                                .offset(x: 5, y: -5)
                            }
                        }
                        if store.isUploadingImage {
                            ZStack {
                                HarnessTheme.panel2
                                ProgressView().tint(HarnessTheme.amber)
                            }
                            .frame(width: 60, height: 60)
                            .clipShape(RoundedRectangle(cornerRadius: 4))
                        }
                    }
                    .padding(.horizontal, 14)
                    .padding(.top, 6)
                }
            }

            HStack(alignment: .bottom, spacing: 8) {
                if store.isVisionAvailable {
                    Menu {
                        Button {
                            showPhotoPicker = true
                        } label: {
                            Label("Photo Library", systemImage: "photo.on.rectangle")
                        }
                        if UIImagePickerController.isSourceTypeAvailable(.camera) {
                            Button {
                                showCamera = true
                            } label: {
                                Label("Take Photo", systemImage: "camera")
                            }
                        }
                    } label: {
                        Image(systemName: "photo.badge.plus")
                            .font(.body)
                            .foregroundStyle(HarnessTheme.dim)
                            .frame(width: 36, height: 36)
                            .harnessCard(border: HarnessTheme.lineHot, background: HarnessTheme.panel2)
                    }
                    .disabled(store.isStreaming || store.isUploadingImage)
                    .accessibilityLabel("Attach image")
                }

                TextField("Ask Harness…", text: $store.composerText, axis: .vertical)
                    .lineLimit(1...6)
                    .font(.body)
                    .foregroundStyle(HarnessTheme.text)
                    .focused($composerFocused)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 8)
                    .background(HarnessTheme.panel2)
                    .overlay(RoundedRectangle(cornerRadius: 6).stroke(HarnessTheme.lineHot, lineWidth: 1))

                if store.isStreaming {
                    Button {
                        Task { await store.stopTurn() }
                    } label: {
                        Image(systemName: "stop.fill")
                            .font(.subheadline.weight(.semibold))
                            .frame(width: 36, height: 36)
                            .background(HarnessTheme.rust)
                            .foregroundStyle(.white)
                            .clipShape(RoundedRectangle(cornerRadius: 6))
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Stop generation")
                } else {
                    let canSend = !store.composerText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !store.pendingAttachments.isEmpty
                    Button {
                        composerFocused = false
                        Task { await store.sendMessage() }
                    } label: {
                        Image(systemName: "arrow.up")
                            .font(.subheadline.weight(.bold))
                            .frame(width: 36, height: 36)
                            .background(canSend ? HarnessTheme.amber : HarnessTheme.panel2)
                            .foregroundStyle(canSend ? HarnessTheme.ink : HarnessTheme.faint)
                            .clipShape(RoundedRectangle(cornerRadius: 6))
                    }
                    .buttonStyle(.plain)
                    .disabled(!canSend || store.isUploadingImage)
                    .accessibilityLabel("Send message")
                }
            }
            .padding(.horizontal, 12)

            HStack(spacing: 10) {
                Button {
                    store.thinkFirst.toggle()
                } label: {
                    HStack(spacing: 5) {
                        Image(systemName: store.thinkFirst ? "brain.head.profile.fill" : "brain.head.profile")
                            .font(.caption2)
                        Text(store.thinkFirst ? "Think first: ON" : "Think first: OFF")
                            .font(HarnessTheme.monoSmall)
                    }
                    .foregroundStyle(store.thinkFirst ? HarnessTheme.amber : HarnessTheme.dim)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 4)
                    .background(store.thinkFirst ? HarnessTheme.amber.opacity(0.12) : HarnessTheme.panel2)
                    .overlay(RoundedRectangle(cornerRadius: 4).stroke(store.thinkFirst ? HarnessTheme.amber.opacity(0.5) : HarnessTheme.line, lineWidth: 1))
                }
                .buttonStyle(.plain)

                Spacer()

                if let ctx = store.capabilities?.nCtx, ctx > 0 {
                    Text("\(ctx / 1024)K ctx")
                        .font(HarnessTheme.monoSmall)
                        .foregroundStyle(HarnessTheme.faint)
                }
            }
            .padding(.horizontal, 12)
            .padding(.bottom, 8)
        }
        .padding(.top, 8)
        .background(HarnessTheme.panel)
    }
}

// MARK: - Camera Sheet Wrapper

private struct CameraPickerView: UIViewControllerRepresentable {
    var onCapture: (UIImage) -> Void
    var onCancel: () -> Void

    func makeCoordinator() -> Coordinator { Coordinator(onCapture: onCapture, onCancel: onCancel) }

    func makeUIViewController(context: Context) -> UIImagePickerController {
        let picker = UIImagePickerController()
        picker.sourceType = .camera
        picker.delegate = context.coordinator
        return picker
    }

    func updateUIViewController(_ uiViewController: UIImagePickerController, context: Context) {}

    final class Coordinator: NSObject, UINavigationControllerDelegate, UIImagePickerControllerDelegate {
        let onCapture: (UIImage) -> Void
        let onCancel: () -> Void

        init(onCapture: @escaping (UIImage) -> Void, onCancel: @escaping () -> Void) {
            self.onCapture = onCapture
            self.onCancel = onCancel
        }

        func imagePickerController(_ picker: UIImagePickerController,
                                   didFinishPickingMediaWithInfo info: [UIImagePickerController.InfoKey: Any]) {
            if let img = info[.originalImage] as? UIImage {
                onCapture(img)
            } else {
                onCancel()
            }
        }

        func imagePickerControllerDidCancel(_ picker: UIImagePickerController) {
            onCancel()
        }
    }
}
