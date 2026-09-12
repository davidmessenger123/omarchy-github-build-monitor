import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

// GitHub Build Monitor
//
// Real-time indicator in the bar for the state of your GitHub Actions
// CI/CD pipelines. Monitors every repository owned by the GitHub account
// that is currently logged in on this machine (via gh CLI or GITHUB_TOKEN)
// and renders one status pill; left-click opens a list of recent workflow
// runs, right-click refreshes immediately. If no account is logged in the
// pill asks to sign in and left-click launches `gh auth login` in a terminal.
//
// Optional configuration lives on the bar layout entry in shell.json:
//   {
//     "id": "davidjm.github-build-monitor",
//     "repos": "HANCORE-linux/Shibumi-Shell",  // extra repos beyond the account
//     "maxRepos": 30,
//     "token": "",
//     "host": "",
//     "interval": 60,
//     "perPage": 6
//   }
BarWidget {
  id: root
  moduleName: "davidjm.github-build-monitor"

  // ---- configuration ------------------------------------------------------

  // Settings are read fresh on every refresh (see refresh/onSettingsChanged)
  // because the shell updates this widget's `settings` in place when
  // shell.json changes; properties bound once at construction would go stale.

  // ---- state --------------------------------------------------------------

  property var repoResults: []          // [{repo, error?, runs?, runUrl?}]
  property string overall: Model.STATUS_UNKNOWN
  property string statusText: "Checking GitHub account…"
  property var derivedRows: []          // full popup list from the last fetch
  property string selectedRepo: ""      // repo whose notifications are shown
  property string rateStatus: ""
  property string updatedLabel: ""
  property bool fetchBusy: false
  property string account: ""           // login of the watched GitHub account
  property int repoCount: 0             // account repos monitored
  property bool notLoggedIn: false      // no usable credentials -> sign in flow
  property bool ghAvailable: false      // is the gh CLI installed?
  property var notifications: []        // unread notifications from the account
  property int unreadCount: 0           // total unread notifications shown
  property int actionableCount: 0       // ...that need your review/response

  // Clicking a repo row switches the list to that repo's notifications;
  // click it again (or the "close this view" row) to see everything again.
  readonly property var popupRows: root.selectedRepo === ""
    ? root.derivedRows
    : Model.repoNotificationRows(root.selectedRepo, root.notifications, root.repoResults)

  // The plain-G mark is drawn smaller than the solid bar glyphs at the same
  // pixel size, so we measure its painted height against a reference glyph
  // (the bell) and scale it up to optically match the other bar icons.
  TextMetrics {
    id: gRefMetrics
    font.family: root.bar ? root.bar.fontFamily : Style.font.family
    font.pixelSize: Style.bar.iconFont
    text: "\uf0f3"
  }
  TextMetrics {
    id: gGlyphMetrics
    font.family: root.bar ? root.bar.fontFamily : Style.font.family
    font.pixelSize: Style.bar.iconFont
    text: "\uDB82\uDEF4"
  }
  readonly property real gScale: {
    var ref = gRefMetrics.tightBoundingRect.height
    var glyph = gGlyphMetrics.tightBoundingRect.height
    return (ref > 0 && glyph > 0) ? ref / glyph : 1.3
  }
  // The letter G concentrates its ink in the upper half, so on top of the
  // height ratio it needs a little extra size and a nudge downward to sit at
  // the same optical height as the solid bar glyphs.
  readonly property real gExtra: 1.08
  readonly property real gDrop: Math.round(Style.bar.iconFont * 0.05)
  readonly property bool showG: root.overall === "success" ||
    root.overall === "neutral" || root.overall === "unknown"

  readonly property bool highlighted: overall === "running" || overall === "pending" ||
    overall === "failure" || overall === "action-required" || overall === "error" ||
    overall === Model.STATUS_LOGIN || overall === Model.STATUS_ATTENTION

  readonly property string activeIcon: Model.statusIcon(overall)
  readonly property color activeColor: Model.statusColor(overall, root.bar ? root.bar.foreground : Color.foreground)

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  // ---- polling ------------------------------------------------------------

  function refresh() {
    if (root.fetchBusy) return

    // Re-read every setting fresh: shell.json can be edited at any time and
    // the host only updates this widget's `settings` in place.
    var repos = Model.parseRepos(setting("repos", ""))
    var maxRepos = Model.clampInt(setting("maxRepos", 30), 1, 100)
    var apiHost = setting("host", "")
    var githubToken = setting("token", "")
    var perPage = Model.clampInt(setting("perPage", 6), 1, 30)

    // Account mode by default: the helper resolves the logged-in GitHub
    // account itself (gh CLI, GITHUB_TOKEN, or the `token` setting) and
    // monitors that account's repositories. Extra --repo flags add repos
    // that don't belong to the account (organization-owned, a coworker's…).
    var command = ["python3",
      Model.scriptPath(Qt.resolvedUrl("github-builds.py")),
      "--per-page", String(perPage),
      "--max-repos", String(maxRepos),
      "--timeout", "12"]
    if (apiHost !== "") { command.push("--host"); command.push(apiHost) }
    if (githubToken !== "") { command.push("--token"); command.push(githubToken) }
    for (var i = 0; i < repos.length; i++) {
      command.push("--repo"); command.push(repos[i])
    }

    fetcher.command = command
    root.fetchBusy = true
    fetcher.running = true
  }

  function ingest(raw) {
    root.fetchBusy = false
    var state = Model.deriveState(raw)
    root.repoResults = state.results
    root.overall = state.overall
    root.statusText = state.statusText
    root.derivedRows = state.rows
    root.rateStatus = state.rateStatus
    root.account = state.account
    root.repoCount = state.repoCount
    root.notLoggedIn = state.notLoggedIn
    root.ghAvailable = state.ghAvailable
    root.notifications = state.notifications
    root.unreadCount = state.unreadCount
    root.actionableCount = state.actionableCount
    root.updatedLabel = "Updated " + Model.clockTime(new Date())
  }

  onSettingsChanged: function() {
    pollTimer.interval = Model.clampInt(setting("interval", 60), 15, 3600) * 1000
    Qt.callLater(root.refresh)
  }

  Process {
    id: fetcher
    command: []
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.ingest(text)
    }
    onExited: function(exitCode) {
      // If the stream already parsed lines this is a no-op. Only a failure
      // with no usable stdout should flip the widget into its error state.
      if (root.fetchBusy) root.ingest("")
    }
  }

  Timer {
    id: pollTimer
    interval: Model.clampInt(setting("interval", 60), 15, 3600) * 1000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  // ---- IPC (refresh / open from hotkeys and omarchy-shell) ----------------

  IpcHandler {
    target: root.moduleName
    function refresh(): void { root.refresh() }
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.togglePanel() }
  }

  // Open/close contract the bar uses to route panel hotkeys and summon IPC.
  // Single source of truth that the KeyboardPanel below mirrors via its
  // `open` binding.
  property bool opened: false
  function open() {
    // Not signed in? The "popup" is the sign-in flow instead.
    if (root.notLoggedIn) { root.loginAction(); return }
    root.opened = true
  }
  function close() { root.opened = false }
  function togglePanel() {
    if (root.notLoggedIn) { root.loginAction(); return }
    root.opened = !root.opened
  }

  // Ask the user to log a GitHub account in. The gh CLI owns the credentials;
  // `omarchy-launch-terminal` runs it in the user's terminal so the device
  // flow can complete interactively. The next poll picks the account up
  // automatically.
  function loginAction() {
    if (root.ghAvailable) {
      Quickshell.execDetached(["omarchy-launch-terminal", "gh", "auth", "login"])
    } else {
      Quickshell.execDetached(["omarchy-notification-send",
        "GitHub Build Monitor",
        "The gh CLI is missing. Install it (sudo pacman -S github-cli), sign in, and the monitor will start tracking your account."])
    }
  }

  function shellOpen(url) {
    if (!url) return
    if (root.bar && typeof root.bar.run === "function" && typeof root.bar.shellQuote === "function")
      root.bar.run("xdg-open " + root.bar.shellQuote(url))
    else if (root.bar && typeof root.bar.run === "function")
      root.bar.run("xdg-open " + url)
  }

  // ---- bar pill -----------------------------------------------------------

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.activeIcon
    active: root.highlighted
    activeColor: root.activeColor
    useActiveColor: root.highlighted
    slotSize: Style.bar.iconSlot
    // The G is rendered by a custom icon so we can size it up and bias it
    // downward; every other state uses the plain text glyph at bar size.
    iconComponent: root.showG ? gPillIcon : null
    fontSize: Style.bar.iconFont
    tooltipText: root.statusText
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) root.refresh()
      else if (buttonCode === Qt.MiddleButton) {
        // Unread feedback waiting? Straight to the notifications inbox.
        if (root.actionableCount > 0) root.shellOpen(Model.githubNotificationsUrl())
        else if (root.account !== "") root.shellOpen(Model.githubAccountReposUrl(root.account))
        else if (root.repoResults.length > 0) root.shellOpen(root.repoResults[0].repoUrl)
      }
      else root.togglePanel()
    }
  }

  // ---- popup: recent workflow runs ----------------------------------------

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    padding: Style.spacing.popupPadding
    contentWidth: panel.fittedContentWidth(Style.space(460), Style.space(560))
    contentHeight: panel.fittedContentHeight(root.popupImplicitHeight, Style.space(640))

    PanelKeyCatcher {
      anchors.fill: parent
      onCloseRequested: root.close()

      Column {
        id: popupRoot
        width: parent.width
        spacing: Style.space(6)

        Row {
          id: headerRow
          width: parent.width
          spacing: Style.space(8)

          OpticalGlyph {
            anchors.verticalCenter: parent.verticalCenter
            width: Style.space(22)
            height: headerRow.height
            text: root.activeIcon
            fontFamily: root.bar ? root.bar.fontFamily : Style.font.resolvedFamily
            fontSize: root.showG ? Style.font.icon * root.gScale : Style.font.icon
            color: root.activeColor
          }

          Column {
            id: headerText
            width: parent.width - Style.space(22) - headerRow.spacing
            spacing: Style.space(2)
            Text {
              width: parent.width
              text: root.account !== "" ? "GitHub Builds · @" + root.account : "GitHub Builds"
              color: Color.popups.text
              font.family: Style.font.family
              font.pixelSize: Style.font.body
              font.bold: true
              elide: Text.ElideRight
            }
            Text {
              width: parent.width
              text: root.statusText + (root.updatedLabel !== "" ? " · " + root.updatedLabel : "")
              color: Color.muted
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
              wrapMode: Text.WrapAtWordBoundaryOrAnywhere
              visible: text !== ""
            }
          }
        }

        // Scrollable run list.
        Flickable {
          id: runsFlick
          width: parent.width
          height: root.popupRowsHeight
          contentHeight: runsList.implicitHeight
          clip: true
          boundsBehavior: Flickable.StopAtBounds

          Column {
            id: runsList
            width: parent.width
            spacing: Style.space(2)

            Repeater {
              model: root.popupRows

              delegate: Component {
                Item {
                  id: row
                  required property var modelData
                  width: runsList.width
                  height: row.typeHeight

                  readonly property int typeHeight: modelData.kind === "run" ? Style.space(46) : Style.space(32)

                  readonly property bool rowClickable: modelData.kind === "repo" ||
                    modelData.kind === "run" || modelData.kind === "error" ||
                    modelData.kind === "note" || modelData.kind === "selectedRepo"

                  readonly property bool rowTitle: modelData.kind === "repo" ||
                    modelData.kind === "selectedRepo" || modelData.kind === "notifications"

                  readonly property bool rowBadge: modelData.kind === "repo" &&
                    modelData.notifCount > 0

                  Item {
                    anchors.fill: parent
                    clip: true

                    Rectangle {
                      visible: rowMouse.containsMouse || rowMouse.pressed
                      anchors.fill: parent
                      radius: Style.cornerRadius
                      color: Color.popups.text
                      opacity: rowMouse.pressed ? 0.10 : 0.06
                    }

                    MouseArea {
                      id: rowMouse
                      anchors.fill: parent
                      hoverEnabled: row.rowClickable
                      cursorShape: row.rowClickable ? Qt.PointingHandCursor : Qt.ArrowCursor
                      onClicked: {
                        // Only real notifications go to the browser. Clicking a
                        // repo (its header, a run, or an error row) narrows the
                        // list to that repo's notifications instead.
                        if (modelData.kind === "note") {
                          if (modelData.url) root.shellOpen(modelData.url)
                        } else if (modelData.kind === "selectedRepo") {
                          root.selectedRepo = ""
                        } else if (modelData.kind === "repo" || modelData.kind === "run" || modelData.kind === "error") {
                          var r = modelData.repo
                          root.selectedRepo = r !== "" && root.selectedRepo === r ? "" : (r || "")
                        }
                      }
                    }

                    OpticalGlyph {
                      anchors.left: parent.left
                      anchors.verticalCenter: parent.verticalCenter
                      width: Style.space(22)
                      height: parent.height
                      text: modelData.icon
                      fontFamily: root.bar ? root.bar.fontFamily : Style.font.resolvedFamily
                      fontSize: modelData.kind === "run" ? Style.font.body : Style.font.caption
                      color: modelData.color
                    }

                    Column {
                      anchors.left: parent.left
                      anchors.leftMargin: Style.space(28)
                      anchors.right: parent.right
                      anchors.rightMargin: row.rowBadge ? Style.space(36) : 0
                      anchors.verticalCenter: parent.verticalCenter
                      spacing: Style.space(1)

                      Text {
                        width: parent.width
                        text: row.rowTitle
                          ? modelData.repo
                          : (modelData.kind === "error" ? modelData.repo : modelData.title)
                        color: Color.popups.text
                        font.family: Style.font.family
                        font.pixelSize: modelData.kind === "run" ? Style.font.body : Style.font.bodySmall
                        font.bold: row.rowTitle
                        elide: Text.ElideRight
                      }

                      Text {
                        width: parent.width
                        visible: text !== ""
                        text: modelData.kind === "error"
                          ? modelData.message
                          : (modelData.subtitle || modelData.state)
                        color: modelData.kind === "error" ? Color.urgent : Color.muted
                        font.family: Style.font.family
                        font.pixelSize: Style.font.caption
                        elide: Text.ElideRight
                        maximumLineCount: 1
                      }
                    }

                    // Unread-notification badge on a repo row (bell + count).
                    // Clicking the repo shows the actual notifications.
                    Row {
                      anchors.right: parent.right
                      anchors.verticalCenter: parent.verticalCenter
                      spacing: Style.space(2)
                      visible: row.rowBadge

                      OpticalGlyph {
                        width: Style.space(16)
                        height: Style.space(18)
                        text: "\uf0f3"
                        fontFamily: root.bar ? root.bar.fontFamily : Style.font.resolvedFamily
                        fontSize: Style.font.bodySmall
                        color: modelData.notifUrgent ? "#e0af68" : "#cacccc"
                      }

                      Text {
                        text: modelData.notifCount
                        color: modelData.notifUrgent ? "#e0af68" : Color.muted
                        font.family: Style.font.family
                        font.pixelSize: Style.font.bodySmall
                        verticalAlignment: Text.AlignVCenter
                      }
                    }
                  }
                }
              }
            }

            Text {
              id: emptyState
              width: parent.width
              visible: root.popupRows.length === 0 && !root.notLoggedIn
              text: root.repoCount === 0
                ? "No repositories found for " + (root.account !== "" ? root.account : "this account") + "."
                : "No workflow runs reported yet."
              color: Color.muted
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
              wrapMode: Text.WrapAtWordBoundaryOrAnywhere
              horizontalAlignment: Text.AlignHCenter
            }
          }
        }

        // Footer with rate-limit status, dashboard and refresh. All three are
        // vertically centered on the same line; the buttons are anchored to
        // the right edge and the status text takes exactly the leftover width,
        // so nothing can push them outside the panel.
        Item {
          id: footerRow
          width: parent.width
          height: refreshFooterButton.implicitHeight
          visible: !root.notLoggedIn

          Text {
            id: footerText
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            width: parent.width - dashboardButton.implicitWidth - refreshFooterButton.implicitWidth - Style.space(16)
            text: root.rateStatus !== ""
              ? root.rateStatus
              : (root.account !== ""
                  ? "@" + root.account + " · " + root.repoCount + (root.repoCount === 1 ? " repo" : " repos")
                  : "")
            color: Color.muted
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
            maximumLineCount: 1
          }

          Button {
            id: refreshFooterButton
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            text: "Refresh"
            iconText: "\uf021"
            iconSize: Style.font.caption
            fontSize: Style.font.caption
            bordered: true
            tooltipText: "Fetch now (right-click the bar icon also refreshes)"
            onClicked: root.refresh()
          }

          Button {
            id: dashboardButton
            anchors.right: refreshFooterButton.left
            anchors.rightMargin: Style.space(8)
            anchors.verticalCenter: parent.verticalCenter
            text: "Dashboard"
            fontSize: Style.font.caption
            bordered: true
            tooltipText: "Open the account's repositories in the browser"
            onClicked: {
              if (root.account !== "") root.shellOpen(Model.githubAccountReposUrl(root.account))
              else if (root.repoResults.length > 0 && root.repoResults[0].repoUrl)
                root.shellOpen(root.repoResults[0].repoUrl)
            }
          }
        }
      }
    }
  }

  // Heights for the fixed-size popup. Content taller than the cap scrolls
  // inside runsFlick.
  function rowHeightOf(row) {
    if (!row) return 0
    if (row.kind === "run") return Style.space(46)
    return Style.space(32)
  }

  readonly property real popupRowsHeight: {
    var need = Style.space(40)
    for (var i = 0; i < root.popupRows.length; i++) need += root.rowHeightOf(root.popupRows[i])
    return Math.min(need, Style.space(500))
  }

  readonly property real popupImplicitHeight: {
    var header = Style.space(34)
    var footer = root.notLoggedIn ? Style.space(6) : Style.space(36)
    var gaps = Style.space(6) * 3
    return header + root.popupRowsHeight + footer + gaps
  }

  // Bar pill rendering for the calm/G states: bigger font plus a gentle
  // downward bias so the G fills the same optical box as the other icons.
  Component {
    id: gPillIcon
    Item {
      anchors.fill: parent
      OpticalGlyph {
        anchors.centerIn: parent
        anchors.verticalCenterOffset: root.gDrop
        text: "\uDB82\uDEF4"
        fontFamily: root.bar ? root.bar.fontFamily : Style.font.resolvedFamily
        fontSize: Style.bar.iconFont * root.gScale * root.gExtra
        color: root.highlighted ? root.activeColor : (root.bar ? root.bar.foreground : Color.foreground)
      }
    }
  }
}