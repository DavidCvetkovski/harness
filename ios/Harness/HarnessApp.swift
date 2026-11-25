import SwiftUI

@main
struct HarnessApp: App {
    @State private var store = AppStore()
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            RootView(store: store)
                .preferredColorScheme(.dark)
                .tint(HarnessTheme.amber)
                .task {
                    await store.restoreConnectionIfSaved()
                }
                .onChange(of: scenePhase) { _, newPhase in
                    if newPhase == .active {
                        Task { await store.handleScenePhaseActive() }
                    }
                }
        }
    }
}
