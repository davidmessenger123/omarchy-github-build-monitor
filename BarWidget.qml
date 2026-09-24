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
//     "interval": 60,               // calm poll preference (s); active pipelines poll at 20s

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
  property int fetchGeneration: 0
  property int activeGeneration: 0
  property int watchdogGeneration: -1
  property string activeSession: ""
  property bool refreshQueued: false
  property string activeConfigKey: ""
  property string account: ""
  property string webOrigin: Model.DEFAULT_WEB_ORIGIN
  property int repoCount: 0
  property bool notLoggedIn: false
  property bool ghAvailable: false      // is the gh CLI installed?
  property var notifications: []        // unread notifications from the account
  property int unreadCount: 0           // total unread notifications shown
  property int actionableCount: 0       // ...that need your review/response
  property bool notesChanged: false

  // Clicking a repo row switches the list to that repo's notifications;
  // click it again (or the "close this view" row) to see everything again.
  readonly property var popupRows: root.selectedRepo === ""
    ? root.derivedRows
    : Model.repoNotificationRows(root.selectedRepo, root.notifications, root.repoResults, root.webOrigin)

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

  function queueRefresh() {
    if (root.refreshQueued) return
    root.refreshQueued = true
    Qt.callLater(function() {
      root.refreshQueued = false
      root.refresh()
    })
  }

  function refresh() {
    var configuredHost = String(setting("host", "") || "")
    var apiHost = Model.canonicalApiHost(configuredHost)
    if (configuredHost !== "" && apiHost === "") {
      if (fetcherLoader.item) {
        fetcherLoader.item.ingested = true
        fetcherLoader.item.running = false
      }
      fetcherLoader.active = false
      watchdog.stop()
      root.fetchGeneration++
      root.activeGeneration = -1
      root.activeSession = ""
      root.activeConfigKey = ""
      root.fetchBusy = false
      root.overall = "error"
      root.statusText = "Invalid API host"
      return
    }
    var repos = Model.parseRepos(setting("repos", ""))
    var maxRepos = Model.clampInt(setting("maxRepos", 30), 1, 100)
    var rawToken = String(setting("token", "") || "")
    var githubToken = rawToken.trim()
    if (githubToken.length > 1024 || /[\u0000-\u001f\u007f]/.test(githubToken)) githubToken = ""
    var perPage = Model.clampInt(setting("perPage", 6), 1, 30)
    var inheritedToken = ""
    if (typeof Quickshell.env === "function") {
      var environmentToken = Quickshell.env("GITHUB_TOKEN")
      if (environmentToken !== null && environmentToken !== undefined)
        inheritedToken = String(environmentToken).trim()
    }
    if (inheritedToken.length > 1024 || /[\u0000-\u001f\u007f]/.test(inheritedToken)) inheritedToken = ""
    var childEnvironment = {}
    if (githubToken !== "" && githubToken.length <= 1024) childEnvironment.GITHUB_TOKEN = githubToken
    else if (Model.isGitHubApiHost(apiHost) && inheritedToken.length <= 1024 && inheritedToken !== "")
      childEnvironment.GITHUB_TOKEN = inheritedToken
    var configKey = [apiHost, githubToken, perPage, maxRepos, repos.join(",")].join("|")
    if (root.fetchBusy && root.activeConfigKey === configKey) return
    if (root.fetchBusy && fetcherLoader.item) {
      fetcherLoader.item.ingested = true
      fetcherLoader.item.running = false
      fetcherLoader.active = false
      root.fetchBusy = false
    }

    var generation = root.fetchGeneration + 1
    root.fetchGeneration = generation
    root.activeGeneration = generation
    root.activeSession = String(Date.now()) + "-" + String(generation)
    root.activeConfigKey = configKey
    root.webOrigin = Model.normalizeWebOrigin("https://" + (apiHost === "api.github.com" ? "github.com" : (apiHost.indexOf("api.") === 0 ? apiHost.slice(4) : apiHost)))
    var helperPath = Model.scriptPath(Qt.resolvedUrl("github-builds.py"))
    if (helperPath === "") {
      root.fetchBusy = false
      root.overall = "error"
      root.statusText = "Monitor helper is unavailable"
      return
    }
    var command = ["/usr/bin/python3", "-I", helperPath,
      "--per-page", String(perPage),
      "--max-repos", String(maxRepos),
      "--timeout", "12",
      "--session", root.activeSession]
    if (root.repoResults.length === 0) command.push("--no-etag")
    if (configuredHost !== "") { command.push("--host"); command.push(apiHost) }
    for (var i = 0; i < repos.length; i++) {
      command.push("--repo"); command.push(repos[i])
    }

    fetcherLoader.active = true
    var fetcher = fetcherLoader.item
    if (!fetcher) {
      root.fetchBusy = false
      return
    }
    fetcher.generation = generation
    fetcher.session = root.activeSession
    fetcher.ingested = false
    fetcher.overflow = false
    fetcher.output = ""
    fetcher.clearEnvironment = true
    fetcher.environment = childEnvironment
    fetcher.command = command
    root.fetchBusy = true
    fetcher.running = true
    root.watchdogGeneration = generation
    watchdog.interval = root.pollDeadlineMs(maxRepos, repos.length, perPage)
    watchdog.start()
  }

  // Mirror of the helper's own SIGALRM deadline (timeout 12s, 8 workers).
  function pollDeadlineMs(maxRepos, extras, perPage) {
    var totalRepos = Math.min(100, Math.max(1, maxRepos) + Math.max(0, extras || 0))
    var repoWaves = Math.max(1, Math.ceil(totalRepos / 8))
    var pageWork = 10
    var pageFactor = Math.max(1, Math.ceil((perPage || 6) / 30))
    var work = 2 + pageWork * pageFactor + repoWaves + 1
    return Math.min(900000, Math.max(30000, (8 + 12 * work + 5) * 1000))
  }

  Timer {
    id: watchdog
    interval: 60000
    repeat: false
    onTriggered: {
      if (!root.fetchBusy || root.watchdogGeneration !== root.activeGeneration) return
      var current = fetcherLoader.item
      if (current) current.running = false
      root.fetchBusy = false
      root.activeGeneration = -1
      root.activeSession = ""
      root.activeConfigKey = ""
      root.overall = "error"
      root.statusText = "Poll timed out — the shell aborted the fetch"
    }
  }

  function ingest(raw, generation, session, source) {
    if (!root.fetchBusy || generation !== root.activeGeneration || session !== root.activeSession) return
    if (source && source.ingested) return
    var state = null
    try {
      state = Model.deriveState(raw, {
        results: root.repoResults,
        notifications: root.notifications
      }, root.webOrigin)
    } catch (parseError) {
      root.failRun(generation, session, source, 1)
      return
    }
    if (!state.hasMeta) {
      root.failRun(generation, session, source, 1)
      return
    }
    if (state.session !== session) {
      root.failRun(generation, session, source, 1)
      return
    }
    if (source) source.ingested = true
    watchdog.stop()
    root.fetchBusy = false
    root.repoResults = state.results
    root.overall = state.overall
    root.statusText = state.statusText
    root.derivedRows = state.rows
    root.rateStatus = state.rateStatus
    root.account = state.account
    root.webOrigin = state.webOrigin || root.webOrigin
    root.repoCount = state.repoCount
    root.notLoggedIn = state.notLoggedIn
    root.ghAvailable = state.ghAvailable
    root.notifications = state.notifications
    root.unreadCount = state.unreadCount
    root.actionableCount = state.actionableCount
    root.notesChanged = state.notesChanged
    root.updatedLabel = "Updated " + Model.clockTime(new Date())
    root.applyPollInterval()
  }

  function failRun(generation, session, source, exitCode) {
    if (!root.fetchBusy || generation !== root.activeGeneration || session !== root.activeSession) return
    if (source && source.ingested) return
    if (source) source.ingested = true
    watchdog.stop()
    root.fetchBusy = false
    root.activeGeneration = -1
    root.activeSession = ""
    root.activeConfigKey = ""
    root.overall = "error"
    root.statusText = "Poll failed — helper exited with code " + String(exitCode)
  }

  // Adaptive polling: active pipelines need a short interval; calm states can
  // wait. The shell.json `interval` setting is the calm-side preference, with
  // a floor of 180s so idle polls do not hammer the API.
  function pollIntervalMs() {
    var configured = Model.clampInt(setting("interval", 60), 15, 3600)
    var busy = root.overall === Model.STATUS_RUNNING ||
      root.overall === Model.STATUS_PENDING
    if (busy) return 20 * 1000
    return Math.max(configured, 180) * 1000
  }

  function applyPollInterval() {
    pollTimer.interval = root.pollIntervalMs()
  }

  onSettingsChanged: function() {
    root.applyPollInterval()
    root.queueRefresh()
  }

  Component {
    id: fetcherTemplate
    Process {
      id: sessionFetcher
      property int generation: -1
      property string session: ""
      property bool ingested: false
      property bool overflow: false
      property string output: ""
      command: []
      stdout: SplitParser {
        onRead: function(data) {
          if (sessionFetcher.overflow) return
          var value = String(data || "")
          if (sessionFetcher.output.length + value.length + 1 > Model.MAX_RAW_CHARS) {
            sessionFetcher.overflow = true
            sessionFetcher.output = ""
            sessionFetcher.running = false
            return
          }
          sessionFetcher.output += value + "\n"
        }
      }
      stderr: SplitParser {}
      onExited: function(exitCode) {
        if (sessionFetcher.ingested) return
        if (sessionFetcher.overflow || exitCode !== 0) {
          root.failRun(sessionFetcher.generation, sessionFetcher.session, sessionFetcher, exitCode)
          return
        }
        root.ingest(sessionFetcher.output, sessionFetcher.generation, sessionFetcher.session, sessionFetcher)
      }
    }
  }

  Loader {
    id: fetcherLoader
    active: false
    sourceComponent: fetcherTemplate
  }

  Timer {
    id: pollTimer
    interval: 180000
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
      var apiHost = Model.canonicalApiHost(setting("host", ""))
      var loginHost = apiHost.indexOf("api.") === 0 ? apiHost.slice(4) : apiHost
      var command = ["/usr/share/omarchy/bin/omarchy-launch-terminal", "/usr/bin/gh", "auth", "login"]
      if (loginHost !== "" && loginHost !== "api.github.com" && loginHost !== "github.com")
        command.push("--hostname", loginHost)
      Quickshell.execDetached(command)
    } else {
      Quickshell.execDetached(["/usr/share/omarchy/bin/omarchy-notification-send",
        "GitHub Notify Center",
        "The gh CLI is missing. Install the github-cli package and sign in, and the monitor will start tracking your account."])
    }
  }

  function shellOpen(url) {
    var safe = Model.safeWebUrl(url, root.webOrigin)
    if (!safe) return
    if (typeof Quickshell.execDetached === "function")
      Quickshell.execDetached(["/usr/bin/xdg-open", safe])
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
        if (root.actionableCount > 0) root.shellOpen(Model.githubNotificationsUrl(root.webOrigin))
        else if (root.account !== "") root.shellOpen(Model.githubAccountReposUrl(root.account, root.webOrigin))
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
              if (root.account !== "") root.shellOpen(Model.githubAccountReposUrl(root.account, root.webOrigin))
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

  Component.onDestruction: {
    root.fetchGeneration++
    root.activeGeneration = -1
    root.activeSession = ""
    root.refreshQueued = false
    root.fetchBusy = false
    if (fetcherLoader.item) {
      fetcherLoader.item.ingested = true
      fetcherLoader.item.running = false
    }
    fetcherLoader.active = false
    watchdog.stop()
    pollTimer.stop()
  }
}
