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

  // Where the watcher publishes visibility. Only an ABSOLUTE runtime dir is
  // accepted: an empty or relative value must never become a path at "/".
  readonly property string stateFile: {
    const rt = Quickshell.env("XDG_RUNTIME_DIR") || ""
    return rt.startsWith("/") ? rt + "/dropdown-terminal.state" : ""
  }

  // Event-driven state: the watcher rewrites this file whenever the dropdown is
  // shown or hidden, so the icon tracks it with zero idle wakeups. A missing,
  // oversized or malformed file keeps the previous state, and only the
  // `visible` boolean is ever read - it is never interpolated into a command.
  FileView {
    id: stateFileView
    path: root.stateFile
    watchChanges: true
    printErrors: false
    onLoaded: root.readState()
    // text() is stale inside the change signal, so reload() first.
    onFileChanged: reload()
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
  // the honest place to stop.
  readonly property var cliEnvPassthrough: [
    "XDG_RUNTIME_DIR", "HYPRLAND_INSTANCE_SIGNATURE", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS"
  ]

  function cliArgv(args) {
    const argv = ["/usr/bin/env", "-i",
                  "HOME=" + (Quickshell.env("HOME") || ""),
                  "PATH=/usr/bin:/bin:/usr/local/bin"]
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

  // Fallback only: with no absolute XDG_RUNTIME_DIR - or while the state file has
  // never loaded, e.g. because the watcher validated a different runtime dir -
  // there is nothing to watch, so reconcile against the CLI slowly instead of
  // showing a permanently stale icon. In a normal session this timer is inert.
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
