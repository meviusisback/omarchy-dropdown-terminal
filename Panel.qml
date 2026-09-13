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

  // Live state. `visible` comes from the state file the focus watcher publishes
  // (event-driven); the rest is filled from the CLI's JSON status, which now
  // runs only on demand - at load, after an action, or when IPC asks.
  property var ddState: ({
    server: "unknown",
    window: null,
    workspace: null,
    visible: false,
    keybind: "SUPER + U"
  })
  property bool busy: false

  // Set once the watcher's state file has been parsed at least once; until then
  // the slow reconcile timer below stays active (see the fallback Timer).
  property bool stateFileSeen: false

  // Where the watcher publishes visibility. The path is ASKED FOR, not derived from
  // a raw environment variable: the CLI prints the path the backend validated (the
  // runtime-dir rules live in backend/focus_watcher.py and all three consumers use
  // them), so the widget cannot end up watching a directory the watcher refused -
  // say a world-writable one where any local user could plant the file.
  property string stateFile: ""

  Process {
    id: statePathProc
    command: root.cliArgv(["state-path"])
    running: true
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        const path = text.trim()
        if (path.startsWith("/")) root.stateFile = path
      }
    }
  }

  // Event-driven state: the watcher rewrites this file whenever the dropdown is
  // shown or hidden, so the icon tracks it and nothing polls on a schedule. A
  // missing, oversized or malformed file keeps the previous state, and only the
  // `visible` boolean is ever read - it is never interpolated into a command.
  FileView {
    id: stateFileView
    path: root.stateFile
    watchChanges: true
    printErrors: false
    onLoaded: root.readState()
    // text() is stale inside the change signal, so reload() first.
    onFileChanged: reload()
    onLoadFailed: { /* no state file yet: keep the previous value */ }
  }

  function readState() {
    try {
      const parsed = JSON.parse(stateFileView.text() || "{}")
      if (parsed && typeof parsed === "object" && typeof parsed.visible === "boolean") {
        root.stateFileSeen = true
        root.ddState = Object.assign({}, root.ddState, { visible: parsed.visible })
      }
    } catch (e) { /* malformed or truncated: keep the previous state */ }
  }

  function scriptPath() {
    // bin/ is a sibling of Panel.qml inside the plugin folder.
    return Qt.resolvedUrl("bin/omarchy-dropdown-terminal").toString().replace(/^file:\/\//, "")
  }

  // Every automatic process is started with a CLEARED environment: /usr/bin/env -i
  // drops the inherited environment (PATH included) and only the variables the CLI
  // needs are passed back in explicitly. The tools themselves are then resolved to
  // validated absolute paths inside the CLI. /usr/bin/env is the one hard-coded
  // path here: something must be the first exec, and a root-owned system path is
  // the honest place to stop. The CLI's own stdout is what the collectors below
  // read, and it is fixed-shape: the backend caps every compositor-supplied string
  // (Quickshell 0.3.1's StdioCollector has no maxBufferSize to set).
  readonly property var cliEnvPassthrough: [
    "XDG_RUNTIME_DIR", "HYPRLAND_INSTANCE_SIGNATURE", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS"
  ]

  function cliArgv(args) {
    // No PATH at all: the CLI resolves every tool to a validated absolute path, so
    // there is nothing for a hostile PATH entry to win. (proc.build_env() sets a
    // filtered PATH for the Python side's children, which is a different layer.)
    const argv = ["/usr/bin/env", "-i", "HOME=" + (Quickshell.env("HOME") || "")]
    for (const name of root.cliEnvPassthrough) {
      const value = Quickshell.env(name) || ""
      if (value !== "") argv.push(name + "=" + value)
    }
    return argv.concat([root.scriptPath()], args)
  }

  function refresh() {
    if (statusProc.running || busy) return
    statusProc.running = true
  }

  function runAction(action) {
    if (busy) return
    busy = true
    actionProc.command = root.cliArgv([action])
    actionProc.running = true
  }

  // ---------------- processes (argv arrays only) ----------------
  Process {
    id: statusProc
    command: root.cliArgv(["status"])
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

  // Fallback only: while there is no state path to watch (the backend refused every
  // candidate runtime dir) or the file has never loaded, reconcile against the CLI
  // slowly instead of showing a permanently stale icon - and never treat an
  // unvalidated path as authoritative. In a normal session this timer is inert.
  Timer {
    id: fallbackRefreshTimer
    interval: 30000
    repeat: true
    running: root.stateFile === "" || !root.stateFileSeen
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

  // ---------------- dropdown window background ----------------
  Rectangle {
    anchors.fill: parent
    color: Color.background
    radius: 8
    border.width: 1
    border.color: Qt.rgba(Color.foreground.r, Color.foreground.g, Color.foreground.b, 0.15)
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

  // ---------------- focus watcher (auto-close special ws when focus leaves) ----------------
  // Run through the plugin CLI's internal `watcher` subcommand: the CLI resolves
  // the interpreter to a validated absolute path (never PATH) and execs it with
  // -I -E -S, so this process inherits neither the session environment (env -i,
  // above) nor PYTHON*/site-packages.
  Process {
    id: focusWatcher
    command: root.cliArgv(["watcher"])
    running: true
  }

  // Restart the watcher if it ever exits (crash, or a stale instance holding
  // the lock at load time). Without it a dead watcher silently disables
  // click-outside-to-close until the shell is restarted.
  Timer {
    interval: 10000
    repeat: true
    running: true
    onTriggered: if (!focusWatcher.running) focusWatcher.running = true
  }

  Component.onCompleted: root.refresh()
  Component.onDestruction: focusWatcher.running = false
}
