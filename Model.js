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

// Nerd Font (FA4 range, universally present) glyphs for each status.
var GLYPHS = {
  "running": "\uf110",          // nf-fa-circle_o_notch — active pipeline
  "pending": "\uf017",          // nf-fa-clock_o — queued / scheduled
  "success": "\uf00c",          // nf-fa-check — passed
  "failure": "\uf00d",          // nf-fa-times — failed / timed out
  "cancelled": "\uf05e",        // nf-fa-ban — cancelled
  "action-required": "\uf06a",  // nf-fa-exclamation_circle
  "neutral": "\uf0c8",          // nf-fa-square_o — nothing notable
  "error": "\uf071",            // nf-fa-exclamation_triangle — fetch error
  "unknown": "\uf0c8",
  "login": "\uf09b"             // nf-fa-github — not signed in
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
  "login": "#e0af68"
}

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
// Row shapes:
//   {kind:"header", repo, state, icon, color, url}
//   {kind:"run",    icon, color, title, subtitle, url}
//   {kind:"error",  repo, message}
function buildPopupRows(results) {
  var rows = []
  for (var i = 0; i < (results || []).length; i++) {
    var result = results[i]
    if (!result) continue
    if (result.error) {
      rows.push({
        kind: "error",
        repo: result.repo,
        icon: GLYPHS[STATUS_ERROR],
        color: COLORS[STATUS_ERROR],
        message: String(result.error).slice(0, 160)
      })
      continue
    }
    var state = repoState(result.runs)
    rows.push({
      kind: "header",
      repo: result.repo,
      state: state,
      icon: statusIcon(state),
      color: statusColor(state, "#cacccc"),
      url: result.repoUrl || githubActionsUrl(result.repo)
    })
    for (var j = 0; j < (result.runs || []).length; j++) {
      var run = result.runs[j]
      var st = runState(run)
      rows.push({
        kind: "run",
        icon: statusIcon(st),
        color: statusColor(st, "#cacccc"),
        title: runTitle(run),
        subtitle: runSubtitle(run),
        url: run.html_url || ""
      })
    }
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

// Parse the helper's raw stdout into widget state in one pass. Handles the
// account/not-logged-in/zero-repo shapes on top of the per-repo results.
//
// raw lines:
//   {"repo": "...", "runs": [...], "repoUrl": "..."}   (or "error")
//   {"__meta": {mode, account, repoCount, notLoggedIn, ghAvailable,
//               limit, remaining, note}}
function deriveState(raw) {
  var lines = String(raw || "").split("\n")
  var results = []
  var meta = {}
  for (var i = 0; i < lines.length; i++) {
    var line = lines[i].trim()
    if (line === "") continue
    var obj = null
    try { obj = JSON.parse(line) } catch (e) { continue }
    if (!obj) continue
    if (obj.__meta) meta = obj.__meta
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

  var overall = STATUS_NEUTRAL
  var statusText = "No activity yet"
  if (results.length === 0) {
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
    overall: overall,
    statusText: statusText,
    rows: buildPopupRows(results),
    rateStatus: rateStatus,
    notLoggedIn: false,
    ghAvailable: ghAvailable,
    account: account,
    repoCount: repoCount
  }
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
    unconfiguredMessage: unconfiguredMessage,
    STATUS_UNKNOWN: STATUS_UNKNOWN,
    STATUS_LOGIN: STATUS_LOGIN
  }
}