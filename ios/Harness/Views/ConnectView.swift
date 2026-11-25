import SwiftUI
import UIKit

struct ConnectView: View {
    @Bindable var store: AppStore
    @State private var showTokenField = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                // Brand mark & header
                VStack(alignment: .leading, spacing: 10) {
                    HStack(spacing: 12) {
                        HarnessMarkView(size: 44)
                        VStack(alignment: .leading, spacing: 2) {
                            Text("HARNESS")
                                .font(.system(size: 16, weight: .semibold, design: .monospaced))
                                .tracking(2.2)
                                .foregroundStyle(HarnessTheme.text)
                            Text("Local macOS AI companion")
                                .font(.caption)
                                .foregroundStyle(HarnessTheme.dim)
                        }
                    }
                    Text("Connect directly to Harness running on your Mac over Tailscale or your local network. Everything stays on your hardware.")
                        .font(.subheadline)
                        .foregroundStyle(HarnessTheme.dim)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(.top, 20)

                // Connection card
                VStack(alignment: .leading, spacing: 14) {
                    Text("MAC SERVER ADDRESS")
                        .font(HarnessTheme.label)
                        .tracking(1.1)
                        .foregroundStyle(HarnessTheme.dim)

                    TextField("my-mac.ts.net:8420 or 100.x.x.x:8420", text: $store.serverURLInput)
                        .font(HarnessTheme.mono)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled(true)
                        .keyboardType(.URL)
                        .submitLabel(.go)
                        .onSubmit {
                            Task { await store.connect(rawURL: store.serverURLInput, explicitToken: store.tokenInput) }
                        }
                        .padding(12)
                        .background(HarnessTheme.panel2)
                        .overlay(RoundedRectangle(cornerRadius: 4).stroke(HarnessTheme.lineHot, lineWidth: 1))
                        .foregroundStyle(HarnessTheme.text)

                    HStack(spacing: 10) {
                        Button {
                            store.showQRScanner = true
                        } label: {
                            Label("Scan QR", systemImage: "qrcode.viewfinder")
                                .font(.footnote.weight(.medium))
                                .frame(maxWidth: .infinity)
                                .padding(.vertical, 10)
                        }
                        .buttonStyle(.plain)
                        .foregroundStyle(HarnessTheme.text)
                        .harnessCard(border: HarnessTheme.lineHot, background: HarnessTheme.panel2)

                        Button {
                            if let pasted = UIPasteboard.general.string {
                                store.serverURLInput = pasted.trimmingCharacters(in: .whitespacesAndNewlines)
                                if let parsed = ConnectionParser.parse(pasted), let tok = parsed.token {
                                    store.tokenInput = tok
                                    showTokenField = true
                                }
                            }
                        } label: {
                            Label("Paste Link", systemImage: "doc.on.clipboard")
                                .font(.footnote.weight(.medium))
                                .frame(maxWidth: .infinity)
                                .padding(.vertical, 10)
                        }
                        .buttonStyle(.plain)
                        .foregroundStyle(HarnessTheme.text)
                        .harnessCard(border: HarnessTheme.lineHot, background: HarnessTheme.panel2)
                    }

                    DisclosureGroup(isExpanded: $showTokenField) {
                        SecureField("Optional bearer token (for --lan mode)", text: $store.tokenInput)
                            .font(HarnessTheme.mono)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled(true)
                            .padding(10)
                            .background(HarnessTheme.panel2)
                            .overlay(RoundedRectangle(cornerRadius: 4).stroke(HarnessTheme.line, lineWidth: 1))
                            .foregroundStyle(HarnessTheme.text)
                            .padding(.top, 6)
                    } label: {
                        Text("Access token (optional over Tailscale)")
                            .font(.caption)
                            .foregroundStyle(HarnessTheme.dim)
                    }
                    .tint(HarnessTheme.dim)

                    if let error = store.connectionError {
                        HStack(alignment: .top, spacing: 8) {
                            Image(systemName: "exclamationmark.triangle.fill")
                                .foregroundStyle(HarnessTheme.rust)
                                .font(.caption)
                            Text(error)
                                .font(.caption)
                                .foregroundStyle(HarnessTheme.rust)
                        }
                        .padding(10)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(HarnessTheme.rust.opacity(0.08))
                        .overlay(RoundedRectangle(cornerRadius: 4).stroke(HarnessTheme.rust.opacity(0.4), lineWidth: 1))
                    }

                    Button {
                        Task { await store.connect(rawURL: store.serverURLInput, explicitToken: store.tokenInput) }
                    } label: {
                        HStack {
                            if store.connectionState == .connecting && !store.isDemoMode {
                                ProgressView()
                                    .tint(HarnessTheme.ink)
                                    .scaleEffect(0.85)
                            }
                            Text(store.connectionState == .connecting && !store.isDemoMode ? "Connecting…" : "Connect to Mac")
                                .font(.subheadline.weight(.semibold))
                        }
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 12)
                        .background(HarnessTheme.amber)
                        .foregroundStyle(HarnessTheme.ink)
                        .clipShape(RoundedRectangle(cornerRadius: 4))
                    }
                    .disabled(store.connectionState == .connecting || store.serverURLInput.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    .opacity(store.serverURLInput.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? 0.5 : 1)
                }
                .padding(16)
                .harnessCard()

                // Offline Demo Mode (for first-time exploration & App Store Review)
                VStack(alignment: .leading, spacing: 10) {
                    Text("NO MAC NEARBY?")
                        .font(HarnessTheme.label)
                        .tracking(1.1)
                        .foregroundStyle(HarnessTheme.dim)
                    Text("Explore streaming replies, tool calls, interactive file-edit approvals, and image attachments offline.")
                        .font(.caption)
                        .foregroundStyle(HarnessTheme.dim)
                    Button {
                        Task { await store.enterDemoMode() }
                    } label: {
                        HStack {
                            Image(systemName: "sparkles")
                            Text("Try Offline Demo Mode")
                                .font(.subheadline.weight(.medium))
                        }
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 11)
                        .foregroundStyle(HarnessTheme.sage)
                        .harnessCard(border: HarnessTheme.sage.opacity(0.45), background: HarnessTheme.sage.opacity(0.08))
                    }
                    .buttonStyle(.plain)
                }
                .padding(16)
                .harnessCard()

                // Quick setup note
                VStack(alignment: .leading, spacing: 6) {
                    Text("ON YOUR MAC")
                        .font(HarnessTheme.label)
                        .tracking(1.1)
                        .foregroundStyle(HarnessTheme.faint)
                    Text("Run `harness --remote` (or `harness --remote --tls` with Tailscale HTTPS certificates) and scan the QR code shown in Terminal.")
                        .font(.caption)
                        .foregroundStyle(HarnessTheme.dim)
                }
                .padding(.horizontal, 4)
            }
            .padding(20)
        }
        .background(HarnessTheme.ink)
        .sheet(isPresented: $store.showQRScanner) {
            QRScannerSheet { scanned in
                store.showQRScanner = false
                store.serverURLInput = scanned
                Task { await store.connect(rawURL: scanned) }
            }
        }
    }
}

/// Vector recreation of `harness/icon.svg` for headers and connect screens.
struct HarnessMarkView: View {
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
            // Antenna
            var stem = Path()
            stem.move(to: CGPoint(x: 32 * s, y: 11 * s))
            stem.addLine(to: CGPoint(x: 32 * s, y: 19 * s))
            ctx.stroke(stem, with: .color(Color(red: 58/255, green: 71/255, blue: 65/255)), style: StrokeStyle(lineWidth: 2.5 * s, lineCap: .round))
            ctx.fill(Path(ellipseIn: CGRect(x: 28.8 * s, y: 5.8 * s, width: 6.4 * s, height: 6.4 * s)), with: .color(HarnessTheme.sage))
            // Eyes
            ctx.fill(Path(ellipseIn: CGRect(x: 14 * s, y: 27 * s, width: 16 * s, height: 16 * s)), with: .color(HarnessTheme.amber))
            ctx.fill(Path(ellipseIn: CGRect(x: 38.4 * s, y: 29.4 * s, width: 11.2 * s, height: 11.2 * s)), with: .color(HarnessTheme.amber))
            // Pupils
            ctx.fill(Path(ellipseIn: CGRect(x: 17.1 * s, y: 30.1 * s, width: 5 * s, height: 5 * s)), with: .color(HarnessTheme.ink.opacity(0.55)))
            ctx.fill(Path(ellipseIn: CGRect(x: 40.5 * s, y: 31.5 * s, width: 3.6 * s, height: 3.6 * s)), with: .color(HarnessTheme.ink.opacity(0.55)))
            // Mouth readout
            ctx.fill(Path(roundedRect: CGRect(x: 22 * s, y: 50 * s, width: 20 * s, height: 3.4 * s), cornerRadius: 1.7 * s), with: .color(HarnessTheme.sage.opacity(0.85)))
            ctx.fill(Path(roundedRect: CGRect(x: 45 * s, y: 50 * s, width: 4.4 * s, height: 3.4 * s), cornerRadius: 1.7 * s), with: .color(Color(red: 58/255, green: 71/255, blue: 65/255)))
        }
        .frame(width: size, height: size)
        .overlay(
            RoundedRectangle(cornerRadius: size * 0.22)
                .stroke(HarnessTheme.lineHot, lineWidth: 1)
        )
    }
}
