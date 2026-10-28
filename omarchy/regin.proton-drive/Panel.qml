import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// Proton Drive: status of the pdfs FUSE mount (proton-drive-nautilus). The
// daemon writes $XDG_RUNTIME_DIR/proton-drive-nautilus/status.json with its
// pending uploads and whether the CLI's last call was authenticated; this
// widget watches that file, polls the systemd unit and mount point, and runs
// `proton-drive auth login` for sign-in.
Panel {
  id: root
  moduleName: "regin.proton-drive"
  ipcTarget: "regin.proton-drive"

  readonly property string home: Quickshell.env("HOME")
  readonly property string mountPath: home + "/ProtonDrive"
  readonly property string statusPath: Quickshell.env("XDG_RUNTIME_DIR") + "/proton-drive-nautilus/status.json"
  readonly property string unit: "proton-drive-fs.service"
  readonly property string cliInstallCommand: "omarchy pkg aur add proton-drive-cli-git"

  // ----------------------------------------------------------------- system

  property var system: ({ checked: false, cli: true, installed: true, active: false, mounted: false })

  Process {
    id: systemProc
    command: ["bash", "-c",
      "b() { \"$@\" >/dev/null 2>&1 && echo true || echo false; }; " +
      "printf '{\"cli\":%s,\"installed\":%s,\"active\":%s,\"mounted\":%s}' " +
      "\"$(b command -v proton-drive)\" \"$(b systemctl --user cat " + root.unit + ")\" " +
      "\"$(b systemctl --user is-active " + root.unit + ")\" \"$(b mountpoint -q \"$HOME/ProtonDrive\")\""]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        try {
          var data = JSON.parse(text)
          data.checked = true
          var cliAppeared = data.cli && !root.system.cli
          root.system = data
          if (cliAppeared || root.cliLoggedIn === null) root.checkLogin()
        } catch (e) {}
      }
    }
  }

  function refreshSystem() {
    if (!systemProc.running) systemProc.running = true
  }

  Timer { interval: 15000; running: true; repeat: true; triggeredOnStart: true; onTriggered: root.refreshSystem() }

  // ---------------------------------------------------------- daemon status

  // null when the daemon isn't running (it removes the file on exit).
  property var daemon: null

  FileView {
    id: statusFile
    path: root.statusPath
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: {
      try { root.daemon = JSON.parse(text()) } catch (e) { root.daemon = null }
    }
    onLoadFailed: root.daemon = null
  }

  // The daemon replaces the file atomically, which can drop the inotify
  // watch; a cheap re-read keeps the icon honest either way.
  Timer {
    interval: root.transfers.length > 0 ? 1000 : 3000
    running: true
    repeat: true
    onTriggered: statusFile.reload()
  }

  // Files moving right now: [{path, direction: "up"|"down", done, total}] in bytes.
  readonly property var transfers: root.daemon && root.daemon.transfers ? root.daemon.transfers : []
  readonly property bool uploading: transfers.some(function(t) { return t.direction === "up" })
  readonly property bool downloading: transfers.some(function(t) { return t.direction === "down" })
  readonly property real progress: {
    var done = 0, total = 0
    for (var i = 0; i < transfers.length; i++) { done += transfers[i].done; total += transfers[i].total }
    return total > 0 ? done / total : 0
  }

  function percent(fraction) { return Math.floor(fraction * 100) + "%" }
  function baseName(path) { return path.split("/").pop() }
  function formatSize(bytes) {
    if (bytes < 1024) return bytes + " B"
    if (bytes < 1048576) return (bytes / 1024).toFixed(0) + " KB"
    if (bytes < 1073741824) return (bytes / 1048576).toFixed(1) + " MB"
    return (bytes / 1073741824).toFixed(2) + " GB"
  }

  readonly property var pending: {
    var status = root.daemon && root.daemon.status ? root.daemon.status : {}
    return Object.keys(status).sort().map(function(path) {
      var state = status[path]
      var isError = state.indexOf("error") === 0
      return { path: path, state: isError ? "error" : state, message: isError ? state.slice(7) : "" }
    })
  }
  // Pending files that aren't moving yet; those are listed under transfers.
  readonly property var waiting: pending.filter(function(p) { return p.state !== "uploading" })
  readonly property int errorCount: pending.filter(function(p) { return p.state === "error" }).length
  readonly property int uploadCount: pending.length - errorCount

  // --------------------------------------------------------------- sign-in

  // Result of `proton-drive filesystem list /`: the CLI checks the session
  // before running any command, and listing the root needs no network.
  property var cliLoggedIn: null

  Process {
    id: loginCheckProc
    command: ["proton-drive", "filesystem", "list", "--json", "/"]
    stderr: StdioCollector { id: loginCheckErr; waitForEnd: true }
    onExited: function(exitCode) {
      if (exitCode === 0) root.cliLoggedIn = true
      else if (loginCheckErr.text.toLowerCase().indexOf("login") >= 0) root.cliLoggedIn = false
    }
  }

  function checkLogin() {
    if (root.system.cli && !loginCheckProc.running) loginCheckProc.running = true
  }

  Timer { interval: 300000; running: true; repeat: true; onTriggered: root.checkLogin() }

  // The daemon's view wins when it has seen an auth failure: that's what is
  // actually blocking the mount.
  readonly property bool loggedIn: (root.daemon && root.daemon.loggedIn === false) ? false : root.cliLoggedIn === true
  readonly property bool loginKnown: root.cliLoggedIn !== null || (root.daemon && root.daemon.loggedIn !== null)

  property bool loginRunning: false
  property bool loginCancelled: false
  property string loginUrl: ""
  property string loginError: ""
  property string loginOutput: ""

  function startLogin() {
    if (!root.system.cli || root.loginRunning) return
    root.loginRunning = true
    root.loginCancelled = false
    root.loginUrl = ""
    root.loginError = ""
    root.loginOutput = ""
    loginProc.running = true
  }

  function cancelLogin() {
    if (!root.loginRunning) return
    root.loginCancelled = true
    loginProc.running = false
  }

  function openLoginUrl() {
    if (root.loginUrl !== "") Quickshell.execDetached(["xdg-open", root.loginUrl])
  }

  Process {
    id: loginProc
    // `auth login` opens the browser itself and, with --json, prints
    // {"signInUrl": ...} so the link can be reopened from the panel.
    command: ["proton-drive", "auth", "login", "--json"]
    stdout: SplitParser {
      onRead: function(line) {
        root.loginOutput += line + "\n"
        try {
          var data = JSON.parse(line)
          if (data.signInUrl) root.loginUrl = data.signInUrl
        } catch (e) {}
      }
    }
    stderr: SplitParser { onRead: function(line) { root.loginOutput += line + "\n" } }
    onExited: function(exitCode) {
      root.loginRunning = false
      root.loginUrl = ""
      if (exitCode === 0) {
        root.cliLoggedIn = true
        // The daemon still remembers the failed calls; a restart starts it clean.
        if (root.system.installed) root.restartMount()
      } else if (!root.loginCancelled) {
        root.loginError = root.loginOutput.trim().split("\n").pop() || "Sign-in failed."
      }
    }
  }

  Process {
    id: logoutProc
    command: ["proton-drive", "auth", "logout"]
    onExited: { root.cliLoggedIn = false; root.restartMount() }
  }

  // ---------------------------------------------------------------- actions

  Process {
    id: unitProc
    onExited: root.refreshSystem()
  }

  function restartMount() {
    unitProc.command = ["systemctl", "--user", "restart", root.unit]
    unitProc.running = true
  }

  function stopMount() {
    unitProc.command = ["systemctl", "--user", "stop", root.unit]
    unitProc.running = true
  }

  function openFolder() {
    Quickshell.execDetached(["xdg-open", root.mountPath])
    root.close()
  }

  // Space used by downloaded copies of Drive files, kept for fast reopening.
  readonly property real cacheBytes: root.daemon && root.daemon.cacheBytes ? root.daemon.cacheBytes : 0
  readonly property real cacheLimit: root.daemon && root.daemon.cacheLimit ? root.daemon.cacheLimit : 0

  function clearCache() {
    Quickshell.execDetached(["python3", "-c",
      "import os, sys\ntry: os.setxattr(sys.argv[1], 'user.protondrive.clear-cache', b'1')\nexcept OSError: pass",
      root.mountPath])
  }

  function retryFailed() {
    var paths = root.pending.filter(function(p) { return p.state === "error" })
      .map(function(p) { return root.mountPath + "/" + p.path })
    Quickshell.execDetached(["python3", "-c",
      "import os, sys\nfor p in sys.argv[1:]:\n    try: os.setxattr(p, 'user.protondrive.retry', b'1')\n    except OSError: pass",
      ].concat(paths))
  }

  function copyInstallCommand() {
    Quickshell.execDetached(["wl-copy", "-n", root.cliInstallCommand])
  }

  // ------------------------------------------------------------ bar button

  readonly property string driveState: {
    if (!root.system.checked) return "checking"
    if (!root.system.cli) return "no-cli"
    if (root.loginKnown && !root.loggedIn) return "logged-out"
    if (!root.system.mounted) return "unmounted"
    if (root.errorCount > 0) return "error"
    if (root.uploading || root.uploadCount > 0) return "uploading"
    if (root.downloading) return "downloading"
    return "ok"
  }

  readonly property string summary: {
    switch (root.driveState) {
      case "checking": return "Checking…"
      case "no-cli": return "proton-drive CLI isn't installed"
      case "logged-out": return "Not signed in"
      case "unmounted": return root.system.installed ? "Not mounted" : "Mount service isn't installed"
      case "error": return root.errorCount + (root.errorCount === 1 ? " upload failed" : " uploads failed")
      case "uploading":
      case "downloading": return root.transferSummary
      default: return "Mounted at ~/ProtonDrive"
    }
  }

  readonly property string transferSummary: {
    var verb = root.driveState === "downloading" ? "Downloading " : "Uploading "
    var moving = root.transfers.filter(function(t) { return t.direction === (root.driveState === "downloading" ? "down" : "up") })
    var count = Math.max(moving.length, root.driveState === "uploading" ? root.uploadCount : 0)
    var what = moving.length === 1 && count === 1 ? root.baseName(moving[0].path)
      : count + (count === 1 ? " file" : " files")
    return verb + what + (root.transfers.length > 0 ? " — " + root.percent(root.progress) : "")
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onOpenedChanged: {
    if (opened) {
      refreshSystem()
      checkLogin()
      statusFile.reload()
    }
  }

  // How the cloud is filled:
  //   "progress": blue rises (upload) or falls (download) with the percentage
  //   "sweep":    a moving band while a transfer has no progress yet
  //   "pulse":    files are waiting to upload
  readonly property string fillMode: {
    if (root.transfers.length > 0) return root.progress > 0 ? "progress" : "sweep"
    if (root.uploadCount > 0) return "pulse"
    return ""
  }
  readonly property bool fillUp: root.uploading || !root.downloading

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    foreground: root.driveState === "error" ? Color.urgent : root.barForeground
    opacity: ["ok", "uploading", "downloading", "error"].indexOf(root.driveState) >= 0 ? 1.0 : 0.5
    tooltipText: "Proton Drive — " + root.summary
    onPressed: function(b) { root.toggle() }

    iconComponent: Component {
      Item {
        id: icon
        readonly property string glyph: "\uf0c2" // fa-cloud
        property real sweep: 0
        property real pulse: 0.3

        // The painted cloud only covers part of the icon slot; fill that part.
        readonly property real glyphTop: base.baselineY + metrics.tightBoundingRect.y
        readonly property real glyphHeight: metrics.tightBoundingRect.height

        TextMetrics {
          id: metrics
          text: icon.glyph
          font.family: button.fontFamily
          font.pixelSize: base.renderedFontSize
        }

        OpticalGlyph {
          id: base
          anchors.fill: parent
          text: icon.glyph
          fontFamily: button.fontFamily
          fontSize: button.fontSize
          color: button.foreground
          opacity: root.fillMode === "" ? 1.0 : 0.45
          Behavior on opacity { NumberAnimation { duration: 250 } }
        }

        // A window onto a blue copy of the glyph; moving and sizing the
        // window is what fills the cloud.
        Item {
          id: fillWindow
          visible: root.fillMode !== ""
          clip: true
          width: icon.width

          readonly property real fillTop: icon.glyphTop
          readonly property real fillSpan: icon.glyphHeight
          readonly property real band: fillSpan * 0.45
          height: root.fillMode === "progress" ? fillSpan * root.progress
            : root.fillMode === "sweep" ? band
            : icon.height
          y: root.fillMode === "progress" ? (root.fillUp ? fillTop + fillSpan - height : fillTop)
            : root.fillMode === "sweep"
              ? (root.fillUp ? fillTop + fillSpan - icon.sweep * (fillSpan + band) : fillTop - band + icon.sweep * (fillSpan + band))
              : 0

          Behavior on height { enabled: root.fillMode === "progress"; NumberAnimation { duration: 400; easing.type: Easing.OutCubic } }
          Behavior on y { enabled: root.fillMode === "progress"; NumberAnimation { duration: 400; easing.type: Easing.OutCubic } }

          OpticalGlyph {
            x: 0
            y: -fillWindow.y
            width: icon.width
            height: icon.height
            text: icon.glyph
            fontFamily: button.fontFamily
            fontSize: button.fontSize
            // The theme's accent, so it always matches and follows theme changes.
            color: Color.accent
            opacity: root.fillMode === "pulse" ? icon.pulse : 1.0
          }
        }

        NumberAnimation on sweep {
          running: root.fillMode === "sweep"
          from: 0
          to: 1
          duration: 1100
          loops: Animation.Infinite
        }

        SequentialAnimation on pulse {
          running: root.fillMode === "pulse"
          loops: Animation.Infinite
          NumberAnimation { to: 0.85; duration: 900; easing.type: Easing.InOutSine }
          NumberAnimation { to: 0.2; duration: 900; easing.type: Easing.InOutSine }
        }
      }
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(380))
    contentHeight: panel.fittedContentHeight(content.implicitHeight, Style.space(560))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Column {
        id: content
        anchors.left: parent.left
        anchors.right: parent.right
        spacing: Style.space(10)

        PanelHero {
          width: parent.width
          title: "Proton Drive"
          meta: root.summary
          foreground: root.bar.foreground
          fontFamily: root.bar.fontFamily
          iconComponent: Component {
            Text {
              textFormat: Text.PlainText
              text: ""
              color: root.driveState === "error" ? Color.urgent : root.bar.foreground
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.display
            }
          }
          trailingControl: Component {
            PanelActionButton {
              iconText: "⟳"
              tooltipText: "Refresh status"
              foreground: root.bar.foreground
              onClicked: { root.refreshSystem(); root.checkLogin(); statusFile.reload() }
            }
          }
        }

        // ---------- CLI missing ----------
        Column {
          visible: root.driveState === "no-cli"
          width: parent.width
          spacing: Style.space(4)

          Text {
            width: parent.width
            textFormat: Text.PlainText
            text: "It's an AUR package — install it with:"
            color: root.bar.foreground
            opacity: 0.65
            wrapMode: Text.WordWrap
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
          }
          Row {
            spacing: Style.space(6)
            Text {
              anchors.verticalCenter: parent.verticalCenter
              textFormat: Text.PlainText
              text: root.cliInstallCommand
              color: root.bar.foreground
              font.family: "monospace"
              font.pixelSize: Style.font.bodySmall
            }
            PanelActionButton {
              anchors.verticalCenter: parent.verticalCenter
              iconText: "⧉"
              tooltipText: "Copy install command"
              foreground: root.bar.foreground
              onClicked: root.copyInstallCommand()
            }
          }
        }

        // ---------- Sign-in ----------
        Column {
          visible: root.system.cli && root.loginKnown && !root.loggedIn
          width: parent.width
          spacing: Style.space(6)

          Text {
            visible: root.loginError !== ""
            width: parent.width
            textFormat: Text.PlainText
            text: root.loginError
            color: Color.urgent
            wrapMode: Text.WordWrap
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          Row {
            spacing: Style.space(8)

            Button {
              text: root.loginRunning ? "Signing in…" : "Log in to Proton Drive"
              enabled: !root.loginRunning
              foreground: root.bar.foreground
              onClicked: root.startLogin()
            }
            PanelActionButton {
              visible: root.loginUrl !== ""
              anchors.verticalCenter: parent.verticalCenter
              iconText: "⇗"
              tooltipText: "Open sign-in link in browser"
              foreground: root.bar.foreground
              onClicked: root.openLoginUrl()
            }
            PanelActionButton {
              visible: root.loginRunning
              anchors.verticalCenter: parent.verticalCenter
              iconText: "✕"
              tooltipText: "Cancel sign-in"
              foreground: root.bar.foreground
              onClicked: root.cancelLogin()
            }
          }

          Text {
            visible: root.loginRunning
            width: parent.width
            textFormat: Text.PlainText
            text: root.loginUrl !== "" ? "Waiting for browser authentication…" : "Starting sign-in…"
            color: root.bar.foreground
            opacity: 0.6
            wrapMode: Text.WordWrap
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
          }
        }

        // ---------- Mount ----------
        Column {
          visible: root.system.cli
          width: parent.width
          spacing: Style.space(6)

          Text {
            visible: !root.system.installed
            width: parent.width
            textFormat: Text.PlainText
            text: "Run install.sh from proton-drive-nautilus to set up the ~/ProtonDrive mount."
            color: root.bar.foreground
            opacity: 0.65
            wrapMode: Text.WordWrap
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
          }

          Row {
            visible: root.system.installed
            spacing: Style.space(8)

            Button {
              visible: root.system.mounted
              text: "Open in Files"
              foreground: root.bar.foreground
              onClicked: root.openFolder()
            }
            Button {
              text: root.system.active ? "Restart mount" : "Start mount"
              enabled: !unitProc.running
              foreground: root.bar.foreground
              onClicked: root.restartMount()
            }
            Button {
              visible: root.system.active
              text: "Stop"
              enabled: !unitProc.running
              foreground: root.bar.foreground
              onClicked: root.stopMount()
            }
          }

          Item {
            visible: root.system.mounted && root.daemon !== null
            width: parent.width
            height: freeButton.implicitHeight

            Text {
              anchors.left: parent.left
              anchors.right: freeButton.left
              anchors.rightMargin: Style.space(8)
              anchors.verticalCenter: parent.verticalCenter
              textFormat: Text.PlainText
              text: "Downloaded files: " + root.formatSize(root.cacheBytes)
                + (root.cacheLimit > 0 ? " of " + root.formatSize(root.cacheLimit) : "")
              color: root.bar.foreground
              opacity: 0.65
              elide: Text.ElideRight
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.caption
            }
            Button {
              id: freeButton
              anchors.right: parent.right
              text: "Free up space"
              tooltipText: "Remove local copies of downloaded files. They stay in Proton Drive and download again when opened. Files waiting to upload are kept."
              enabled: root.cacheBytes > 0
              foreground: root.bar.foreground
              onClicked: root.clearCache()
            }
          }
        }

        // ---------- Transfers ----------
        PanelSeparator { visible: root.transfers.length > 0; foreground: root.bar.foreground }

        Text {
          visible: root.transfers.length > 0
          textFormat: Text.PlainText
          text: "TRANSFERS"
          color: root.bar.foreground
          opacity: 0.6
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.caption
        }

        Repeater {
          model: root.transfers

          delegate: Column {
            required property var modelData
            readonly property real fraction: modelData.total > 0 ? modelData.done / modelData.total : 0
            width: content.width
            spacing: Style.space(3)

            Item {
              width: parent.width
              height: nameText.implicitHeight

              Text {
                id: nameText
                anchors.left: parent.left
                anchors.right: sizeText.left
                anchors.rightMargin: Style.space(8)
                textFormat: Text.PlainText
                text: (modelData.direction === "up" ? "⇡ " : "⇣ ") + modelData.path
                color: root.bar.foreground
                elide: Text.ElideMiddle
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.bodySmall
              }
              Text {
                id: sizeText
                anchors.right: parent.right
                textFormat: Text.PlainText
                text: root.percent(fraction) + " of " + root.formatSize(modelData.total)
                color: root.bar.foreground
                opacity: 0.6
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.caption
              }
            }

            Rectangle {
              width: parent.width
              height: Math.max(3, Style.space(3))
              radius: height / 2
              color: Qt.rgba(root.bar.foreground.r, root.bar.foreground.g, root.bar.foreground.b, 0.15)

              Rectangle {
                width: parent.width * fraction
                height: parent.height
                radius: parent.radius
                color: Color.accent
                Behavior on width { NumberAnimation { duration: 400 } }
              }
            }
          }
        }

        // ---------- Uploads ----------
        PanelSeparator { visible: root.waiting.length > 0; foreground: root.bar.foreground }

        Row {
          visible: root.waiting.length > 0
          width: parent.width
          spacing: Style.space(8)

          Text {
            anchors.verticalCenter: parent.verticalCenter
            textFormat: Text.PlainText
            text: "WAITING TO UPLOAD"
            color: root.bar.foreground
            opacity: 0.6
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
          }
          PanelActionButton {
            visible: root.errorCount > 0
            anchors.verticalCenter: parent.verticalCenter
            iconText: "⟳"
            tooltipText: "Retry failed uploads"
            foreground: root.bar.foreground
            onClicked: root.retryFailed()
          }
        }

        Repeater {
          model: root.waiting.slice(0, 12)

          delegate: Column {
            required property var modelData
            width: content.width
            spacing: Style.space(2)

            Text {
              width: parent.width
              textFormat: Text.PlainText
              text: (modelData.state === "error" ? "✕ " : modelData.state === "uploading" ? "⇡ " : "… ") + modelData.path
              color: modelData.state === "error" ? Color.urgent : root.bar.foreground
              elide: Text.ElideMiddle
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.bodySmall
            }
            Text {
              visible: modelData.message !== ""
              width: parent.width
              textFormat: Text.PlainText
              text: modelData.message
              color: root.bar.foreground
              opacity: 0.6
              wrapMode: Text.WordWrap
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.caption
            }
          }
        }

        Text {
          visible: root.waiting.length > 12
          textFormat: Text.PlainText
          text: "and " + (root.waiting.length - 12) + " more"
          color: root.bar.foreground
          opacity: 0.6
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.caption
        }

        // ---------- Account ----------
        Row {
          visible: root.loggedIn
          spacing: Style.space(8)

          Button {
            text: "Log out"
            enabled: !logoutProc.running
            foreground: root.bar.foreground
            onClicked: logoutProc.running = true
          }
        }
      }
    }
  }
}
