import QtQuick
import Quickshell
import Quickshell.Io
import "Model.js" as Model

// One GitHub Actions poller for the whole shell. The bar widget is
// instantiated once per monitor; keeping the helper, the ETag cache writes and
// the API calls here means a docked laptop does not poll every repository once
// per screen.
Item {
  id: root

  readonly property string moduleName: "davidjm.github-build-monitor"

  // The bar widget exists once per monitor, but the IPC target must exist
  // exactly once. The widgets register here, and the service forwards
  // open/close/toggle to the panel that is actually live.
  property var widgets: []
  property var activeWidget: null

  // ---- configuration ------------------------------------------------------

  property string repos: ""
  property int maxRepos: 30
  property string token: ""
  property string host: ""
  property int interval: 60
  property int perPage: 6

  // ---- state --------------------------------------------------------------

  property var repoResults: []          // [{repo, error?, runs?, runUrl?}]
  property string overall: Model.STATUS_UNKNOWN
  property string statusText: "Checking GitHub account…"
  property var derivedRows: []          // full popup list from the last fetch
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
  property bool ghAvailable: false
  property var notifications: []
  property int unreadCount: 0
  property int actionableCount: 0
  property bool notesChanged: false

  function registerWidget(widget) {
    if (!widget || root.widgets.indexOf(widget) !== -1) return
    var next = root.widgets.slice(0)
    next.push(widget)
    root.widgets = next
    if (!root.activeWidget) root.activeWidget = widget
  }

  function unregisterWidget(widget) {
    var next = root.widgets.filter(function(candidate) { return candidate !== widget })
    root.widgets = next
    if (root.activeWidget === widget) root.activeWidget = next.length ? next[0] : null
  }

  function noteOpened(widget, opened) {
    if (opened) root.activeWidget = widget
    else if (root.activeWidget === widget) root.activeWidget = null
  }

  function panelWidget() {
    if (root.activeWidget && root.widgets.indexOf(root.activeWidget) !== -1) return root.activeWidget
    return root.widgets.length ? root.widgets[0] : null
  }

  function openPanel() {
    var widget = root.panelWidget()
    if (widget) widget.open()
  }

  function closePanel() {
    for (var i = 0; i < root.widgets.length; i++) {
      if (root.widgets[i] && root.widgets[i].opened) root.widgets[i].close()
    }
  }

  function togglePanel() {
    for (var i = 0; i < root.widgets.length; i++) {
      if (root.widgets[i] && root.widgets[i].opened) {
        root.closePanel()
        return
      }
    }
    root.openPanel()
  }

  function configure(config) {
    var nextRepos = String(config.repos || "")
    var nextMaxRepos = Model.clampInt(config.maxRepos, 1, 100)
    var nextToken = String(config.token || "")
    var nextHost = String(config.host || "")
    var nextInterval = Model.clampInt(config.interval, 15, 3600)
    var nextPerPage = Model.clampInt(config.perPage, 1, 30)
    var changed = nextRepos !== root.repos || nextMaxRepos !== root.maxRepos ||
      nextToken !== root.token || nextHost !== root.host ||
      nextInterval !== root.interval || nextPerPage !== root.perPage
    root.repos = nextRepos
    root.maxRepos = nextMaxRepos
    root.token = nextToken
    root.host = nextHost
    root.interval = nextInterval
    root.perPage = nextPerPage
    root.applyPollInterval()
    if (changed || root.updatedLabel === "") root.queueRefresh()
  }

  function queueRefresh() {
    if (root.refreshQueued) return
    root.refreshQueued = true
    Qt.callLater(function() {
      root.refreshQueued = false
      root.refresh()
    })
  }

  function refresh() {
    var configuredHost = String(root.host || "")
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
    var repos = Model.parseRepos(root.repos)
    var maxRepos = root.maxRepos
    var rawToken = root.token
    var githubToken = rawToken.trim()
    if (githubToken.length > 1024 || /[\u0000-\u001f\u007f]/.test(githubToken)) githubToken = ""
    var perPage = root.perPage
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
  // wait. The configured interval is the calm-side preference, with a floor of
  // 180s so idle polls do not hammer the API.
  function pollIntervalMs() {
    var busy = root.overall === Model.STATUS_RUNNING ||
      root.overall === Model.STATUS_PENDING
    if (busy) return 20 * 1000
    return Math.max(root.interval, 180) * 1000
  }

  function applyPollInterval() {
    pollTimer.interval = root.pollIntervalMs()
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
    triggeredOnStart: false
    onTriggered: root.refresh()
  }

  IpcHandler {
    target: root.moduleName

    function refresh(): void { root.refresh() }
    function open(): void { root.openPanel() }
    function close(): void { root.closePanel() }
    function show(): void { root.openPanel() }
    function hide(): void { root.closePanel() }
    function toggle(): void { root.togglePanel() }
  }
}
