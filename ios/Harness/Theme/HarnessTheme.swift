import SwiftUI

/// Visual tokens matching Harness's dark instrument-panel aesthetic (`harness/ui.html`).
enum HarnessTheme {
    static let ink = Color(red: 11/255, green: 13/255, blue: 12/255)         // #0b0d0c
    static let panel = Color(red: 17/255, green: 21/255, blue: 20/255)      // #111514
    static let panel2 = Color(red: 22/255, green: 27/255, blue: 25/255)     // #161b19
    static let line = Color(red: 35/255, green: 42/255, blue: 39/255)       // #232a27
    static let lineHot = Color(red: 47/255, green: 58/255, blue: 53/255)    // #2f3a35

    static let text = Color(red: 216/255, green: 222/255, blue: 217/255)    // #d8ded9
    static let dim = Color(red: 125/255, green: 138/255, blue: 131/255)     // #7d8a83
    static let faint = Color(red: 77/255, green: 87/255, blue: 81/255)      // #4d5751

    static let amber = Color(red: 232/255, green: 163/255, blue: 61/255)    // #e8a33d
    static let amberDim = Color(red: 140/255, green: 100/255, blue: 39/255) // #8c6427
    static let sage = Color(red: 143/255, green: 191/255, blue: 127/255)    // #8fbf7f
    static let rust = Color(red: 212/255, green: 97/255, blue: 74/255)      // #d4614a

    static let mono = Font.system(.footnote, design: .monospaced)
    static let monoSmall = Font.system(size: 11, design: .monospaced)
    static let label = Font.system(size: 10.5, weight: .semibold, design: .default)
    static let reading = Font.system(size: 16.5, weight: .regular, design: .serif)
}

struct PanelCardModifier: ViewModifier {
    var border: Color = HarnessTheme.line
    var background: Color = HarnessTheme.panel

    func body(content: Content) -> some View {
        content
            .background(background)
            .overlay(
                RoundedRectangle(cornerRadius: 4)
                    .stroke(border, lineWidth: 1)
            )
            .clipShape(RoundedRectangle(cornerRadius: 4))
    }
}

extension View {
    func harnessCard(border: Color = HarnessTheme.line,
                     background: Color = HarnessTheme.panel) -> some View {
        modifier(PanelCardModifier(border: border, background: background))
    }
}
