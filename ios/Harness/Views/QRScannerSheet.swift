import AVFoundation
import SwiftUI

struct QRScannerSheet: View {
    @Environment(\.dismiss) private var dismiss
    var onScan: (String) -> Void
    @State private var unavailableReason: String?

    var body: some View {
        NavigationStack {
            ZStack {
                HarnessTheme.ink.ignoresSafeArea()
                if let reason = unavailableReason {
                    VStack(spacing: 14) {
                        Image(systemName: "camera.fill")
                            .font(.largeTitle)
                            .foregroundStyle(HarnessTheme.dim)
                        Text(reason)
                            .font(.subheadline)
                            .foregroundStyle(HarnessTheme.dim)
                            .multilineTextAlignment(.center)
                            .padding(.horizontal, 32)
                    }
                } else {
                    QRScannerRepresentable(onScan: onScan, onUnavailable: { unavailableReason = $0 })
                        .ignoresSafeArea()
                    VStack {
                        Spacer()
                        RoundedRectangle(cornerRadius: 12)
                            .stroke(HarnessTheme.amber, lineWidth: 2)
                            .frame(width: 240, height: 240)
                        Spacer()
                        Text("Point at the QR code shown by `harness --remote` or `harness --lan`")
                            .font(.caption)
                            .foregroundStyle(HarnessTheme.text)
                            .padding(.horizontal, 16)
                            .padding(.vertical, 10)
                            .background(HarnessTheme.ink.opacity(0.85))
                            .clipShape(RoundedRectangle(cornerRadius: 6))
                            .padding(.bottom, 32)
                    }
                }
            }
            .navigationTitle("Scan Harness QR")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
            }
        }
    }
}

private struct QRScannerRepresentable: UIViewControllerRepresentable {
    var onScan: (String) -> Void
    var onUnavailable: (String) -> Void

    func makeUIViewController(context: Context) -> QRScannerViewController {
        let vc = QRScannerViewController()
        vc.onScan = onScan
        vc.onUnavailable = onUnavailable
        return vc
    }

    func updateUIViewController(_ uiViewController: QRScannerViewController, context: Context) {}
}

private final class QRScannerViewController: UIViewController, AVCaptureMetadataOutputObjectsDelegate {
    var onScan: ((String) -> Void)?
    var onUnavailable: ((String) -> Void)?

    private let session = AVCaptureSession()
    private var previewLayer: AVCaptureVideoPreviewLayer?
    private var hasReported = false

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .black

        guard let device = AVCaptureDevice.default(for: .video) else {
            onUnavailable?("Camera is unavailable on this device. Paste the link manually instead.")
            return
        }

        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized:
            configureSession(device: device)
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .video) { [weak self] granted in
                DispatchQueue.main.async {
                    if granted {
                        self?.configureSession(device: device)
                    } else {
                        self?.onUnavailable?("Camera permission was denied. Enable camera access in Settings or paste the link manually.")
                    }
                }
            }
        default:
            onUnavailable?("Camera permission is required to scan QR codes. Enable it in iOS Settings or paste the link manually.")
        }
    }

    private func configureSession(device: AVCaptureDevice) {
        do {
            let input = try AVCaptureDeviceInput(device: device)
            if session.canAddInput(input) { session.addInput(input) }
            let output = AVCaptureMetadataOutput()
            if session.canAddOutput(output) {
                session.addOutput(output)
                output.setMetadataObjectsDelegate(self, queue: .main)
                output.metadataObjectTypes = [.qr]
            }
            let preview = AVCaptureVideoPreviewLayer(session: session)
            preview.videoGravity = .resizeAspectFill
            preview.frame = view.bounds
            view.layer.addSublayer(preview)
            self.previewLayer = preview

            DispatchQueue.global(qos: .userInitiated).async { [weak self] in
                self?.session.startRunning()
            }
        } catch {
            onUnavailable?("Could not start camera: \(error.localizedDescription)")
        }
    }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        previewLayer?.frame = view.bounds
    }

    override func viewWillDisappear(_ animated: Bool) {
        super.viewWillDisappear(animated)
        if session.isRunning {
            DispatchQueue.global(qos: .userInitiated).async { [weak self] in
                self?.session.stopRunning()
            }
        }
    }

    func metadataOutput(_ output: AVCaptureMetadataOutput,
                        didOutput metadataObjects: [AVMetadataObject],
                        from connection: AVCaptureConnection) {
        guard !hasReported,
              let obj = metadataObjects.first as? AVMetadataMachineReadableCodeObject,
              let value = obj.stringValue, !value.isEmpty else {
            return
        }
        hasReported = true
        session.stopRunning()
        onScan?(value)
    }
}
