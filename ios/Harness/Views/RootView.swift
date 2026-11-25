import SwiftUI

struct RootView: View {
    @Bindable var store: AppStore

    var body: some View {
        ZStack {
            HarnessTheme.ink.ignoresSafeArea()
            switch store.connectionState {
            case .connected:
                MainChatView(store: store)
            case .disconnected, .connecting:
                ConnectView(store: store)
            }
        }
        .animation(.easeInOut(duration: 0.2), value: store.connectionState)
    }
}
