// Pure helper functions for the GitHub Build Monitor bar widget. No Qt/QML
// dependencies so the logic stays unit-testable outside the shell.

var STATUS_RUNNING = "running"
var STATUS_PENDING = "pending"
var STATUS_SUCCESS = "success"
var STATUS_FAILURE = "failure"
var STATUS_CANCELLED = "cancelled"
var STATUS_ACTION_REQUIRED = "action-required"
var STATUS_NEUTRAL = "neutral"
var STATUS_ERROR = "error"
var STATUS_UNKNOWN = "unknown"
var STATUS_LOGIN = "login"
var STATUS_ATTENTION = "attention"

// Nerd Font glyphs for each status. A plain geometric G is the widget's
// brand mark and covers the calm states (all green, nothing running, unknown).
var GLYPHS = {
  "running": "\uf110",              // nf-fa-circle_o_notch — active pipeline
  "pending": "\uf017",              // nf-fa-clock_o — queued / scheduled
  "success": "\uDB82\uDEF4",        // nf-md-alpha_g — plain geometric G
  "failure": "\uf00d",              // nf-fa-times — failed / timed out
  "cancelled": "\uf05e",            // nf-fa-ban — cancelled
  "action-required": "\uf06a",      // nf-fa-exclamation_circle
  "neutral": "\uDB82\uDEF4",        // nf-md-alpha_g — plain geometric G
  "error": "\uf071",                // nf-fa-exclamation_triangle — fetch error
  "unknown": "\uDB82\uDEF4",
  "login": "\uf09b",                // nf-fa-github — not signed in
  "attention": "\uf0f3"             // nf-fa-bell — unread feedback waiting
}

// Soft, readable-on-dark status colors. Neutral slots resolve to the bar's
// own foreground at render time (see statusColor()).
var COLORS = {
  "running": "#e0af68",
  "pending": "#7aa2f7",
  "success": "#9ece6a",
  "failure": "#f7768e",
  "cancelled": "",
  "action-required": "#e0af68",
  "neutral": "",
  "error": "#ff9e64",
  "unknown": "",
  "login": "#e0af68",
  "attention": "#e0af68"
}

// Notification reasons that mean a human wants something from you: a review
// of a submitted change, a reply on a thread you started, a mention, or an
// assignment. These flip the pill to the bell; other reasons (ci_activity,
// build, security…) only add to the popup list.
var NOTIFY_ACTIONABLE = ["review_requested", "mention", "comment", "author",
  "team_mention", "assign"]

function clampInt(value, min, max) {
  var n = parseInt(String(value), 10)
  if (isNaN(n)) return min
  return Math.max(min, Math.min(max, n))
}

// "owner/name, owner/name" -> ["owner/name", ...]. Malformed entries are
// dropped so a typo in shell.json can't break every other poll.
function parseRepos(raw) {
  var out = []
  var seen = {}
  var parts = String(raw || "").split(",")
  for (var i = 0; i < parts.length; i++) {
    var repo = String(parts[i] || "").trim()
    if (!/^[^/\s]+\/[^/\s]+$/.test(repo)) continue
    if (seen[repo]) continue
    seen[repo] = true
    out.push(repo)
  }
  return out
}

// Strip the file:// prefix off a Qt.resolvedUrl() result so it can be handed
// to a Process command as a plain filesystem path.
function scriptPath(url) {
  return String(url || "").replace(/^file:\/\//, "")
}

// GitHub API does not document this, but payloads can arrive with a null
// `name` for performance reasons. Fall back to the commit title so rows
// never read "Workflow #3" when a real title is available.
function runName(run) {
  if (!run) return ""
  var name = String(run.name || "").trim()
  if (name !== "") return name
  return String(run.display_title || "").trim()
}

// Collapse GitHub's raw status/conclusion pair into our flat status set.
function runState(run) {
  if (!run) return STATUS_UNKNOWN
  var status = String(run.status || "").trim()
  if (status === "in_progress") return STATUS_RUNNING
  if (status === "queued" || status === "pending" || status === "waiting" ||
      status === "requested" || status === "approved") return STATUS_PENDING
  if (status === "completed") {
    var conclusion = String(run.conclusion || "").trim()
    if (conclusion === "success") return STATUS_SUCCESS
    if (conclusion === "failure" || conclusion === "timed_out" ||
        conclusion === "startup_failure") return STATUS_FAILURE
    if (conclusion === "cancelled") return STATUS_CANCELLED
    if (conclusion === "action_required") return STATUS_ACTION_REQUIRED
    return STATUS_NEUTRAL // skipped, neutral, stale, or empty conclusion
  }
  return STATUS_UNKNOWN
}

// State of one repository: the newest run wins unless it is still active.
function repoState(runs) {
  if (!runs || runs.length === 0) return STATUS_NEUTRAL
  for (var i = 0; i < runs.length; i++) {
    var state = runState(runs[i])
    if (state === STATUS_RUNNING || state === STATUS_PENDING) return state
    return state
  }
  return STATUS_NEUTRAL
}

// Widget-wide state across every configured repo. Precedence: running >
// pending > failure > action-required > any error > all success > neutral >
// unknown. A red pipeline on any repo therefore raises an alarm even when
// sister repos are green, matching how CI is usually watched.
function deriveOverall(results, configured) {
  if (!configured || !results || results.length === 0)
    return { overall: STATUS_UNKNOWN, statusText: "No repositories configured" }

  var repoStates = []
  var errorCount = 0
  var fetched = 0
  for (var i = 0; i < results.length; i++) {
    if (results[i].error) { errorCount++; continue }
    fetched++
    repoStates.push(repoState(results[i].runs))
  }

  var any = function(target) {
    for (var j = 0; j < repoStates.length; j++) if (repoStates[j] === target) return true
    return false
  }
  var allOf = function(target) {
    if (repoStates.length === 0) return false
    for (var k = 0; k < repoStates.length; k++) if (repoStates[k] !== target) return false
    return true
  }

  if (fetched === 0 && errorCount > 0) {
    return { overall: STATUS_ERROR, statusText: "Could not reach " + results.length + " repo" + (results.length > 1 ? "s" : "") }
  }
  if (any(STATUS_RUNNING)) {
    var runningCount = 0
    for (var r = 0; r < repoStates.length; r++) if (repoStates[r] === STATUS_RUNNING) runningCount++
    return { overall: STATUS_RUNNING, statusText: runningCount + " pipeline" + (runningCount > 1 ? "s" : "") + " running" }
  }
  if (any(STATUS_PENDING))
    return { overall: STATUS_PENDING, statusText: "Pipeline waiting to start" }
  if (any(STATUS_FAILURE))
    return { overall: STATUS_FAILURE, statusText: "A workflow failed" }
  if (any(STATUS_ACTION_REQUIRED))
    return { overall: STATUS_ACTION_REQUIRED, statusText: "Workflow needs attention" }
  if (errorCount > 0)
    return { overall: STATUS_ERROR, statusText: errorCount + " repo" + (errorCount > 1 ? "s" : "") + " unreachable" }
  if (allOf(STATUS_SUCCESS))
    return { overall: STATUS_SUCCESS, statusText: "All pipelines green" }
  if (allOf(STATUS_CANCELLED))
    return { overall: STATUS_CANCELLED, statusText: "Latest run cancelled" }
  return { overall: STATUS_NEUTRAL, statusText: "No active pipelines" }
}

function statusIcon(status) {
  return GLYPHS[status] || GLYPHS[STATUS_NEUTRAL]
}

// Neutral / cancelled / unknown statuses have no alarm color; they resolve to
// the bar foreground so the pill blends with the rest of the bar.
function statusColor(status, fallbackForeground) {
  var c = COLORS[status] || ""
  return c !== "" ? c : (fallbackForeground || "#cacccc")
}

function pad2(n) {
  return n < 10 ? "0" + n : String(n)
}

function clockTime(date) {
  if (!date) return ""
  return pad2(date.getHours()) + ":" + pad2(date.getMinutes())
}

function timeAgo(iso) {
  if (!iso) return ""
  var t = new Date(iso).getTime()
  if (isNaN(t)) return ""
  var seconds = Math.floor((Date.now() - t) / 1000)
  if (seconds < 60) return "just now"
  var minutes = Math.floor(seconds / 60)
  if (minutes < 60) return minutes + "m ago"
  var hours = Math.floor(minutes / 60)
  if (hours < 24) return hours + "h ago"
  var days = Math.floor(hours / 24)
  if (days < 7) return days + "d ago"
  return days + "d ago"
}

// First line of a workflow run: "<workflow> #<run#>" or the commit title.
function runTitle(run) {
  var wf = runName(run)
  if (wf !== "" && run && run.run_number)
    return wf + " #" + run.run_number
  if (wf !== "") return wf
  return "Workflow run"
}

// Second, muted line: state · branch · commit title · when.
function runSubtitle(run) {
  if (!run) return ""
  var parts = []
  var state = runState(run)
  if (state === STATUS_RUNNING) parts.push("running")
  else if (state === STATUS_PENDING) parts.push("queued")
  else if (state === STATUS_SUCCESS) parts.push("passed")
  else if (state === STATUS_FAILURE) parts.push("failed")
  else if (state === STATUS_CANCELLED) parts.push("cancelled")
  else if (state === STATUS_ACTION_REQUIRED) parts.push("needs review")

  var branch = String(run.head_branch || "").trim()
  if (branch) parts.push(branch)

  var title = String(run.display_title || "").trim()
  if (title !== "" && runName(run) !== title) parts.push(title)

  var when = timeAgo(run.updated_at || run.created_at)
  if (when !== "") parts.push(when)
  return parts.join(" · ")
}

// Flatten repo results into ready-to-render rows for the popup list.
// Repos with unread notifications carry a badge (bell + count). Notifications
// whose repo is not in the watched list (threads on other people's repos you
// follow, marketplace issues…) get a row of their own at the end so nothing
// is orphaned.
// Row shapes:
//   {kind:"repo", repo, state, icon, color, url, notifCount, notifUrgent}
//   {kind:"run",  repo, icon, color, title, subtitle, url}   (click: same filter)
//   {kind:"error", repo, message}                 (click: same filter)
function buildPopupRows(results, notifications) {
  var rows = []
  var watched = {}
  for (var i = 0; i < (results || []).length; i++) {
    var result = results[i]
    if (!result) continue
    if (result.error) {
      watched[result.repo] = true
      rows.push({
        kind: "error",
        repo: result.repo,
        icon: GLYPHS[STATUS_ERROR],
        color: COLORS[STATUS_ERROR],
        message: String(result.error).slice(0, 160)
      })
      continue
    }
    watched[result.repo] = true
    var state = repoState(result.runs)
    var notes = notificationsForRepo(result.repo, notifications)
    rows.push({
      kind: "repo",
      repo: result.repo,
      state: state,
      icon: statusIcon(state),
      color: statusColor(state, "#cacccc"),
      url: result.repoUrl || githubActionsUrl(result.repo),
      notifCount: notes.length,
      notifUrgent: notes.length > 0 && repoActionableCount(notes) > 0
    })
    for (var j = 0; j < (result.runs || []).length; j++) {
      var run = result.runs[j]
      var st = runState(run)
      rows.push({
        kind: "run",
        repo: result.repo,
        icon: statusIcon(st),
        color: statusColor(st, "#cacccc"),
        title: runTitle(run),
        subtitle: runSubtitle(run),
        url: run.html_url || ""
      })
    }
  }

  // Repos that appear only in notifications and are not otherwise watched.
  var extraRepos = {}
  for (var k = 0; k < (notifications || []).length; k++) {
    var repo = notifications[k].repo
    if (repo && !watched[repo]) extraRepos[repo] = true
  }
  for (var repoName in extraRepos) {
    var extraNotes = notificationsForRepo(repoName, notifications)
    var extraCount = repoActionableCount(extraNotes)
    rows.push({
      kind: "repo",
      repo: repoName,
      state: STATUS_NEUTRAL,
      icon: statusIcon(STATUS_NEUTRAL),
      color: "#cacccc",
      url: githubRepoUrl(repoName),
      notifCount: extraNotes.length,
      notifUrgent: extraNotes.length > 0 && extraCount > 0,
      subtitle: extraNotes.length + " notification" + (extraNotes.length > 1 ? "s" : "")
    })
  }
  return rows
}

function githubActionsUrl(repo) {
  return "https://github.com/" + String(repo || "") + "/actions"
}

function githubRepoUrl(repo) {
  return "https://github.com/" + String(repo || "")
}

function githubAccountReposUrl(account) {
  return "https://github.com/" + String(account || "") + "?tab=repositories"
}

function githubNotificationsUrl() {
  return "https://github.com/notifications"
}

// Short human label for a GitHub notification reason.
function notificationLabel(reason) {
  if (reason === "review_requested") return "review requested"
  if (reason === "mention") return "mentioned you"
  if (reason === "comment") return "commented"
  if (reason === "author") return "replied to you"
  if (reason === "team_mention") return "team mention"
  if (reason === "assign") return "assigned to you"
  if (reason === "ci_activity") return "CI activity"
  if (reason === "build") return "build finished"
  if (reason === "security_alert") return "security alert"
  if (reason === "subscribed") return "subscribed"
  return String(reason || "notification").replace(/_/g, " ")
}

function isActionable(reason) {
  for (var i = 0; i < NOTIFY_ACTIONABLE.length; i++)
    if (NOTIFY_ACTIONABLE[i] === reason) return true
  return false
}

// Unread notifications belonging to one repository.
function notificationsForRepo(repo, notifications) {
  var notes = []
  for (var i = 0; i < (notifications || []).length; i++)
    if (notifications[i].repo === repo) notes.push(notifications[i])
  return notes
}

function repoActionableCount(notes) {
  var count = 0
  for (var i = 0; i < notes.length; i++)
    if (isActionable(notes[i].reason)) count++
  return count
}

// Second, muted line for a notification row.
function notificationSubtitle(note) {
  var parts = []
  if (note.repo) parts.push(note.repo)
  parts.push(notificationLabel(note.reason))
  var when = timeAgo(note.updated_at)
  if (when !== "") parts.push(when)
  return parts.join(" · ")
}

// Parse the helper's raw stdout into widget state in one pass. Handles the
// account/not-logged-in/zero-repo shapes on top of the per-repo results.
//
// raw lines:
//   {"__notification": {"reason", "title", "type", "repo", "html", "updated_at"}}
//   {"repo": "...", "runs": [...], "repoUrl": "..."}   (or "error")
//   {"__meta": {mode, account, repoCount, notLoggedIn, ghAvailable,
//               limit, remaining, note}}
function deriveState(raw) {
  var lines = String(raw || "").split("\n")
  var results = []
  var notifications = []
  var meta = {}
  for (var i = 0; i < lines.length; i++) {
    var line = lines[i].trim()
    if (line === "") continue
    var obj = null
    try { obj = JSON.parse(line) } catch (e) { continue }
    if (!obj) continue
    if (obj.__meta) meta = obj.__meta
    else if (obj.__notification) {
      // Flat sibling keys ({"__notification": true, "reason": ...}), or
      // nested, depending on producer — normalize both.
      notifications.push(typeof obj.__notification === "object" ? obj.__notification : obj)
    }
    else results.push(obj)
  }

  var notLoggedIn = meta.notLoggedIn === true
  var account = String(meta.account || "")
  var ghAvailable = meta.ghAvailable === true
  var repoCount = clampInt(meta.repoCount, 0, 100000)

  var rateStatus = ""
  if (meta.remaining !== undefined && meta.remaining !== null &&
      meta.limit !== undefined && meta.limit !== null) {
    rateStatus = "Rate limit: " + meta.remaining + " / " + meta.limit + " used"
  }

  if (notLoggedIn) {
    return {
      results: results,
      notifications: notifications,
      unreadCount: notifications.length,
      actionableCount: 0,
      overall: STATUS_LOGIN,
      statusText: ghAvailable
        ? "No GitHub account logged in - click to sign in"
        : "gh CLI not found - install it to sign in",
      rows: [],
      rateStatus: rateStatus,
      notLoggedIn: true,
      ghAvailable: ghAvailable,
      account: account,
      repoCount: 0
    }
  }

  var actionableCount = 0
  for (var n = 0; n < notifications.length; n++)
    if (isActionable(notifications[n].reason)) actionableCount++

  // Notifications are not listed at the top level: each affected repo row
  // carries a bell badge instead, and clicking the repo reveals them.
  var rows = buildPopupRows(results, notifications)

  var overall = STATUS_NEUTRAL
  var statusText = "No activity yet"
  // Unread review/mention/comment feedback outranks the pipeline state: a
  // human asked you to look at something.
  if (actionableCount > 0) {
    overall = STATUS_ATTENTION
    statusText = actionableCount + " notification" + (actionableCount > 1 ? "s" : "") + " waiting"
  } else if (results.length === 0) {
    if (repoCount === 0 && meta.note) {
      overall = STATUS_ERROR
      statusText = String(meta.note).slice(0, 160)
    } else if (repoCount === 0) {
      overall = STATUS_NEUTRAL
      statusText = account !== ""
        ? "No repositories found for " + account
        : "No repositories found"
    }
  } else {
    var derived = deriveOverall(results, true)
    overall = derived.overall
    statusText = derived.statusText
  }

  return {
    results: results,
    notifications: notifications,
    unreadCount: notifications.length,
    actionableCount: actionableCount,
    overall: overall,
    statusText: statusText,
    rows: rows,
    rateStatus: rateStatus,
    notLoggedIn: false,
    ghAvailable: ghAvailable,
    account: account,
    repoCount: repoCount
  }
}

// Popup rows for the notifications section (sits above the repo runs).
// Row shapes:
//   {kind:"notifications", repo, state, icon, color, url}
//   {kind:"note",   icon, color, title, subtitle, url}
function notificationRows(notifications, actionableCount) {
  var rows = []
  if (notifications.length === 0) return rows
  rows.push({
    kind: "notifications",
    repo: "Notifications (" + notifications.length + ")",
    state: actionableCount > 0 ? STATUS_ATTENTION : STATUS_NEUTRAL,
    icon: GLYPHS[STATUS_ATTENTION],
    color: actionableCount > 0 ? COLORS[STATUS_ATTENTION] : "",
    url: ""
  })
  for (var i = 0; i < notifications.length; i++) {
    var note = notifications[i]
    rows.push({
      kind: "note",
      icon: GLYPHS[STATUS_ATTENTION],
      color: isActionable(note.reason) ? COLORS[STATUS_ATTENTION] : "#cacccc",
      title: note.title || "Notification",
      subtitle: notificationSubtitle(note),
      url: note.html || githubNotificationsUrl()
    })
  }
  return rows
}

// Filtered view shown when the user clicks a repo row: only that repository's
// notifications — or a "No Notifications" row when it has none. The first row
// doubles as an obvious "click to go back" affordance.
// Row shapes:
//   {kind:"selectedRepo", repo, icon, color, subtitle}
//   {kind:"note",   icon, color, title, subtitle, url}
//   {kind:"empty",  title, subtitle}
function repoNotificationRows(repo, notifications) {
  var rows = []
  rows.push({
    kind: "selectedRepo",
    repo: repo,
    icon: GLYPHS[STATUS_ATTENTION],
    color: COLORS[STATUS_ATTENTION],
    subtitle: "Click to close this view"
  })
  var notes = notificationsForRepo(repo, notifications)
  if (notes.length === 0) {
    rows.push({
      kind: "empty",
      title: "No Notifications",
      subtitle: "Nothing unread for " + repo
    })
    return rows
  }
  for (var j = 0; j < notes.length; j++) {
    var note = notes[j]
    rows.push({
      kind: "note",
      icon: GLYPHS[STATUS_ATTENTION],
      color: isActionable(note.reason) ? COLORS[STATUS_ATTENTION] : "#cacccc",
      title: note.title || "Notification",
      subtitle: notificationSubtitle(note),
      url: note.html || githubNotificationsUrl()
    })
  }
  return rows
}

// Empty-state text shown when shell.json has no repos field.
function unconfiguredMessage() {
  return "Configure repositories in shell.json, e.g.\n" +
    "{ \"id\": \"davidjm.github-build-monitor\", \"repos\": \"owner/repo\" }"
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    clampInt: clampInt,
    parseRepos: parseRepos,
    scriptPath: scriptPath,
    runName: runName,
    runState: runState,
    repoState: repoState,
    deriveOverall: deriveOverall,
    deriveState: deriveState,
    statusIcon: statusIcon,
    statusColor: statusColor,
    timeAgo: timeAgo,
    runTitle: runTitle,
    runSubtitle: runSubtitle,
    buildPopupRows: buildPopupRows,
    githubActionsUrl: githubActionsUrl,
    githubRepoUrl: githubRepoUrl,
    githubAccountReposUrl: githubAccountReposUrl,
    githubNotificationsUrl: githubNotificationsUrl,
    notificationLabel: notificationLabel,
    notificationSubtitle: notificationSubtitle,
    notificationsForRepo: notificationsForRepo,
    repoActionableCount: repoActionableCount,
    notificationRows: notificationRows,
    repoNotificationRows: repoNotificationRows,
    unconfiguredMessage: unconfiguredMessage,
    STATUS_UNKNOWN: STATUS_UNKNOWN,
    STATUS_LOGIN: STATUS_LOGIN,
    STATUS_ATTENTION: STATUS_ATTENTION
  }
}