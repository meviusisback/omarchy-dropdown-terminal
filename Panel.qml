import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

Panel {
  id: root

  moduleName: "meviusisback.dropdown-terminal"
  ipcTarget: "meviusisback.dropdown-terminal"
  manageIpc: false

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color accent: Color.accent
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  // Right-click menu open state (drives the PopupCard `open` property).
  property bool menuOpen: false

  // Live state (filled from the CLI's JSON status).
  property var ddState: ({
    server: "unknown",
    window: null,
    workspace: null,
    visible: false,
    keybind: "SUPER + U"
  })
  property bool busy: false

  function scriptPath() {
    // bin/ is a sibling of Panel.qml inside the plugin folder.
    return Qt.resolvedUrl("bin/omarchy-dropdown-terminal").toString().replace(/^file:\/\//, "")
  }

  function refresh() {
    if (statusProc.running || busy) return
    statusProc.running = true
  }

  function runAction(action) {
    if (busy) return
    busy = true
    actionProc.command = [root.scriptPath(), action]
    actionProc.running = true
  }

  // ---------------- processes (argv arrays only) ----------------
  Process {
    id: statusProc
    command: [root.scriptPath(), "status"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        try {
          const parsed = JSON.parse(text || "{}")
          if (parsed && typeof parsed === "object") root.ddState = parsed
        } catch (e) { /* keep previous state on malformed output */ }
        root.busy = false
      }
    }
  }

  Process {
    id: actionProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        root.busy = false
        root.refresh()
      }
    }
  }

  Timer {
    id: refreshTimer
    interval: 5000
    repeat: true
    running: true
    onTriggered: root.refresh()
  }

  // ---------------- IPC ----------------
  IpcHandler {
    target: "meviusisback.dropdown-terminal"

    function toggle(): void { root.runAction("toggle") }
    function open(): void { root.runAction("open") }
    function close(): void { root.runAction("close") }
    function kill(): void { root.runAction("kill") }
    function status(): void { root.refresh() }
  }

  // ---------------- bar button ----------------
  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.ddState.visible ? "\u2b81" : "\u21e5"
    tooltipText: root.ddState.visible
      ? "Drop-down terminal: shown (" + root.ddState.keybind + ") - right-click for menu"
      : "Drop-down terminal: hidden (" + root.ddState.keybind + ") - right-click for menu"
    active: root.ddState.visible
    useActiveColor: true
    activeColor: root.accent

    onPressed: function(buttonCode) {
      if (buttonCode === Qt.LeftButton) {
        root.runAction("toggle")
      } else if (buttonCode === Qt.RightButton) {
        root.menuOpen = !root.menuOpen
      }
    }
  }

  // ---------------- right-click menu ----------------
  PopupCard {
    id: contextMenu
    anchorItem: button
    bar: root.bar
    owner: root
    open: root.menuOpen
    contentWidth: Style.space(280)
    contentHeight: column.implicitHeight + Style.space(12)

    onOpenChanged: if (!open) root.menuOpen = false

    ColumnLayout {
      id: column
      anchors.margins: 0
      anchors.fill: parent
      spacing: Style.space(4)

      Button {
        Layout.fillWidth: true
        leftAlign: true
        text: root.ddState.visible ? "Hide terminal" : "Show terminal"
        foreground: root.foreground
        onClicked: {
          root.menuOpen = false
          root.runAction("toggle")
        }
      }

      Button {
        Layout.fillWidth: true
        leftAlign: true
        text: "Kill server (ends session)"
        foreground: root.foreground
        onClicked: {
          root.menuOpen = false
          root.runAction("kill")
        }
      }
    }
  }

  Component.onCompleted: root.refresh()
}
