import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

// GitHub Build Monitor
//
// Real-time indicator in the bar for the state of your GitHub Actions
// CI/CD pipelines. Polls the Actions API for every configured repository
// and renders one status pill; left-click opens a list of recent workflow
// runs, right-click refreshes immediately.
//
// Configuration lives on the bar layout entry in shell.json:
//   {
//     "id": "davidjm.github-build-monitor",
//     "repos": "HANCORE-linux/Shibumi-Shell, cli/cli",
//     "token": "",
//     "host": "",
//     "interval": 60,
//     "perPage": 6
//   }
BarWidget {
  id: root
  moduleName: "davidjm.github-build-monitor"

  // ---- configuration ------------------------------------------------------

  readonly property var repoList: Model.parseRepos(setting("repos", ""))
  readonly property string apiHost: setting("host", "")
  readonly property string githubToken: setting("token", "")
  readonly property int perPage: Model.clampInt(setting("perPage", 6), 1, 30)
  readonly property int pollIntervalMs: Model.clampInt(setting("interval", 60), 15, 3600) * 1000

  // ---- state --------------------------------------------------------------

  property var repoResults: []          // [{repo, error?, runs?, runUrl?}]
  property string overall: Model.STATUS_UNKNOWN
  property string statusText: "No repositories configured"
  property var popupRows: []            // flattened [{kind, ...}] for the panel
  property string rateStatus: ""
  property string updatedLabel: ""
  property bool fetchBusy: false

  readonly property bool unconfigured: repoList.length === 0
  readonly property bool highlighted: overall === "running" || overall === "pending" ||
    overall === "failure" || overall === "action-required" || overall === "error"

  readonly property string activeIcon: Model.statusIcon(overall)
  readonly property color activeColor: Model.statusColor(overall, root.bar ? root.bar.foreground : Color.foreground)

  // Hide entirely until at least one repository is configured.
  visible: !unconfigured
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  // ---- polling ------------------------------------------------------------

  function refresh() {
    if (root.unconfigured) {
      root.repoResults = []
      root.overall = Model.STATUS_UNKNOWN
      root.statusText = "No repositories configured"
      root.popupRows = []
      root.updatedLabel = ""
      return
    }
    if (root.fetchBusy) return

    var command = ["python3",
      Model.scriptPath(Qt.resolvedUrl("github-builds.py")),
      "--per-page", String(root.perPage),
      "--timeout", "12"]
    if (root.apiHost !== "") { command.push("--host"); command.push(root.apiHost) }
    if (root.githubToken !== "") { command.push("--token"); command.push(root.githubToken) }
    for (var i = 0; i < root.repoList.length; i++) {
      command.push("--repo"); command.push(root.repoList[i])
    }

    fetcher.command = command
    root.fetchBusy = true
    fetcher.running = true
  }

  function ingest(raw) {
    root.fetchBusy = false
    var results = []
    var meta = {}
    var lines = String(raw || "").split("\n")
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i].trim()
      if (line === "") continue
      var obj = null
      try { obj = JSON.parse(line) } catch (e) { continue }
      if (!obj) continue
      if (obj.__meta) { meta = obj.__meta; continue }
      results.push(obj)
    }
    root.repoResults = results

    root.rateStatus = meta.remaining !== undefined && meta.limit !== undefined
      ? "Rate limit: " + meta.remaining + " / " + meta.limit + " used"
      : ""

    if (results.length === 0 && !root.unconfigured) {
      // The fetcher produced nothing readable — python3 missing, crash, or a
      // full transport failure before any per-repo line was written.
      root.overall = Model.STATUS_ERROR
      root.statusText = "Fetch failed (is python3 installed?)"
    } else {
      var derived = Model.deriveOverall(results, !root.unconfigured)
      root.overall = derived.overall
      root.statusText = derived.statusText
    }
    root.popupRows = Model.buildPopupRows(results)
    root.updatedLabel = "Updated " + Model.clockTime(new Date())
  }

  onSettingsChanged: Qt.callLater(root.refresh)

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
    interval: root.pollIntervalMs
    running: !root.unconfigured
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
  function open() { root.opened = true }
  function close() { root.opened = false }
  function togglePanel() { root.opened = !root.opened }

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
    tooltipText: root.statusText
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) root.refresh()
      else if (buttonCode === Qt.MiddleButton) { if (root.repoList.length > 0) root.shellOpen(Model.githubRepoUrl(root.repoList[0])) }
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
            fontSize: Style.font.icon
            color: root.activeColor
          }

          Column {
            id: headerText
            width: parent.width - refreshButton.width - headerRow.spacing
            spacing: Style.space(2)
            Text {
              width: parent.width
              text: "GitHub Builds"
              color: Color.popups.text
              font.family: Style.font.family
              font.pixelSize: Style.font.body
              font.bold: true
              elide: Text.ElideRight
            }
            Text {
              width: parent.width
              text: root.unconfigured ? Model.unconfiguredMessage() : (root.statusText + (root.updatedLabel !== "" ? " · " + root.updatedLabel : ""))
              color: Color.muted
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
              wrapMode: Text.WrapAtWordBoundaryOrAnywhere
              visible: text !== ""
            }
          }

          Button {
            id: refreshButton
            text: "Refresh"
            iconText: "\uf021"
            fontSize: Style.font.caption
            onClicked: root.refresh()
            tooltipText: "Fetch now (right-click the bar icon also refreshes)"
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
                      hoverEnabled: modelData.url !== ""
                      cursorShape: modelData.url !== "" ? Qt.PointingHandCursor : Qt.ArrowCursor
                      onClicked: if (modelData.url) root.shellOpen(modelData.url)
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
                      anchors.verticalCenter: parent.verticalCenter
                      spacing: Style.space(1)

                      Text {
                        width: parent.width
                        text: modelData.kind === "header"
                          ? modelData.repo
                          : (modelData.kind === "error" ? modelData.repo : modelData.title)
                        color: Color.popups.text
                        font.family: Style.font.family
                        font.pixelSize: modelData.kind === "run" ? Style.font.body : Style.font.bodySmall
                        font.bold: modelData.kind === "header"
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
                  }
                }
              }
            }

            Text {
              id: emptyState
              width: parent.width
              visible: root.popupRows.length === 0
              text: root.unconfigured
                ? "Add \"repos\": \"owner/repo\" to this widget's settings."
                : "No workflow runs reported yet."
              color: Color.muted
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
              wrapMode: Text.WrapAtWordBoundaryOrAnywhere
              horizontalAlignment: Text.AlignHCenter
            }
          }
        }

        // Footer with rate-limit status and open-in-browser affordance.
        Row {
          id: footerRow
          width: parent.width
          visible: !root.unconfigured
          Text {
            width: parent.width / 2
            text: root.rateStatus !== ""
              ? root.rateStatus
              : (root.repoList.length > 0 ? "Monitoring " + root.repoList.length + " repo(s)" : "")
            color: Color.muted
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
            elide: Text.ElideRight
          }
          Button {
            text: "Open Actions"
            fontSize: Style.font.caption
            onClicked: {
              if (root.repoList.length > 0)
                root.shellOpen(Model.githubActionsUrl(root.repoList[0]))
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
    var footer = root.unconfigured ? Style.space(6) : Style.space(30)
    var gaps = Style.space(6) * 3
    return header + root.popupRowsHeight + footer + gaps
  }
}