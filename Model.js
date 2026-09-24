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
var MAX_RESULTS = 100
var MAX_NOTIFICATIONS = 100
var MAX_RUNS = 30
var MAX_RAW_CHARS = 8 * 1024 * 1024
var MAX_FIELD_CHARS = 4096
var DEFAULT_WEB_ORIGIN = "https://github.com"
var MAX_URL_CHARS = 2048

function originParts(value) {
  var original = String(value || "")
  var text = original.trim()
  if (!text || text.length > MAX_URL_CHARS || text !== original ||
      text.indexOf("https://") !== 0 || /[\u0000-\u0020\u007f]/.test(text))
    return null
  var rest = text.slice(8)
  var slash = rest.indexOf("/")
  var authority = slash < 0 ? rest : rest.slice(0, slash)
  if (!authority || authority.indexOf("@") !== -1 || authority.indexOf("?") !== -1 ||
      authority.indexOf("#") !== -1 || authority.indexOf("\\") !== -1 || /:$/.test(authority))
    return null
  var tail = slash < 0 ? "" : rest.slice(slash)
  if (tail.indexOf("#") !== -1 || tail.indexOf("\\") !== -1 || tail.indexOf("//") === 0) return null
  var host = authority
  var port = ""
  if (authority.charAt(0) === "[") {
    var close = authority.indexOf("]")
    if (close < 1) return null
    host = authority.slice(1, close)
    var remainder = authority.slice(close + 1)
    if (remainder) {
      if (remainder.charAt(0) !== ":") return null
      port = remainder.slice(1)
    }
    if (!/^[0-9a-f:]+$/i.test(host) || host.indexOf(":") < 0) return null
  } else {
    var colon = authority.lastIndexOf(":")
    if (colon > -1) {
      if (authority.indexOf(":") !== colon) return null
      host = authority.slice(0, colon)
      port = authority.slice(colon + 1)
    }
  }
  if (port) {
    if (!/^[0-9]{1,5}$/.test(port)) return null
    var number = Number(port)
    if (number < 1 || number > 65535) return null
    port = number === 443 ? "" : ":" + String(number)
  }
  host = host.toLowerCase().replace(/\.$/, "")
  if (!host || host === "localhost" || host.endsWith(".localhost") || host.endsWith(".local") ||
      host === "0.0.0.0" || host === "::" || host === "::1" || /^127\./.test(host) || /^169\.254\./.test(host))
    return null
  var bracketed = authority.charAt(0) === "["
  if (!bracketed && !/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$/.test(host))
    return null
  var displayHost = bracketed ? "[" + host + "]" : host
  return { origin: "https://" + displayHost + port, authority: displayHost + port, path: tail }
}

function normalizeWebOrigin(value) {
  if (value === undefined || value === null || value === "") return DEFAULT_WEB_ORIGIN
  var parts = originParts(value)
  return parts ? parts.origin : ""
}

function canonicalApiHost(value) {
  var text = value === undefined || value === null || value === "" ? "api.github.com" : String(value).trim()
  if (text === "") text = "api.github.com"
  if (text.indexOf("https://") !== 0) text = "https://" + text
  var parts = originParts(text)
  if (!parts || (parts.path !== "" && parts.path !== "/")) return ""
  return parts.authority
}

function isGitHubApiHost(value) {
  return canonicalApiHost(value) === "api.github.com"
}

function safeWebUrl(value, origin) {
  var text = String(value || "")
  if (!text || text.length > MAX_URL_CHARS || text !== text.trim() ||
      /[\u0000-\u0020\u007f]/.test(text)) return ""
  var expected = normalizeWebOrigin(origin)
  if (!expected) return ""
  var parts = originParts(text)
  if (!parts || parts.origin !== expected || !parts.path) return ""
  if (parts.path.indexOf("//") === 0) return ""
  return text
}

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
  var n = Number(value)
  if (!isFinite(n)) return min
  n = Math.trunc ? Math.trunc(n) : (n < 0 ? Math.ceil(n) : Math.floor(n))
  return Math.max(min, Math.min(max, n))
}

var OWNER_RE = /^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$/
var REPO_NAME_RE = /^[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?$/

function validRepoName(repo) {
  if (typeof repo !== "string") return false
  var pair = repo.split("/")
  return pair.length === 2 && OWNER_RE.test(pair[0]) && REPO_NAME_RE.test(pair[1]) &&
    pair[0] !== "constructor" && pair[1] !== "constructor" && pair[1].indexOf("..") < 0
}

function validLogin(login) {
  return typeof login === "string" && /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$/.test(login)
}

function parseRepos(raw) {
  var text = String(raw || "")
  if (text.length > 16384) return []
  var out = []
  var seen = Object.create(null)
  var parts = text.split(",")
  for (var i = 0; i < parts.length && out.length < MAX_RESULTS; i++) {
    var repo = String(parts[i] || "").trim()
    if (!validRepoName(repo)) continue
    var pair = repo.split("/")
    var key = pair[0].toLowerCase() + "/" + pair[1].toLowerCase()
    if (seen[key]) continue
    seen[key] = true
    out.push(repo)
  }
  return out
}

// Strip the file:// prefix off a Qt.resolvedUrl() result so it can be handed
// to a Process command as a plain filesystem path.
function scriptPath(url) {
  var text = String(url || "")
  if (text.indexOf("file://") !== 0) return ""
  text = text.slice(7)
  if (!text || text.length > 4096 || text.charAt(0) !== "/" || /[\u0000-\u001f\u007f]/.test(text) ||
      text.split("/").some(function(part) { return part === ".." })) return ""
  return text
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
var RUN_STATUS_VALUES = ["queued", "in_progress", "completed", "waiting", "requested", "pending", "approved"]
var RUN_CONCLUSION_VALUES = ["success", "failure", "cancelled", "timed_out", "action_required", "neutral", "skipped", "stale", "startup_failure"]

function runState(run) {
  if (!run) return STATUS_UNKNOWN
  var status = String(run.status || "").trim()
  if (RUN_STATUS_VALUES.indexOf(status) < 0) return STATUS_UNKNOWN
  if (status === "in_progress") return STATUS_RUNNING
  if (status === "queued" || status === "pending" || status === "waiting" ||
      status === "requested" || status === "approved") return STATUS_PENDING
  if (status === "completed") {
    var conclusion = String(run.conclusion || "").trim()
    if (RUN_CONCLUSION_VALUES.indexOf(conclusion) < 0) return STATUS_UNKNOWN
    if (conclusion === "success") return STATUS_SUCCESS
    if (conclusion === "failure" || conclusion === "timed_out" ||
        conclusion === "startup_failure") return STATUS_FAILURE
    if (conclusion === "cancelled") return STATUS_CANCELLED
    if (conclusion === "action_required") return STATUS_ACTION_REQUIRED
    return STATUS_NEUTRAL
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
// whose repo is not in the watched list (threads on other people's repos,
// marketplace issues…) are attributed to a watched repo when their title
// mentions it; otherwise they get a row of their own at the end so nothing
// is orphaned.
// Row shapes:
//   {kind:"repo", repo, state, icon, color, url, notifCount, notifUrgent}
//   {kind:"run",  repo, icon, color, title, subtitle, url}   (click: same filter)
//   {kind:"error", repo, message}                 (click: same filter)
function buildPopupRows(results, notifications, webOrigin) {
  var origin = normalizeWebOrigin(webOrigin)
  var attr = notificationAttribution(results, notifications)
  var rows = []
  var watched = Object.create(null)
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
    var badge = attr[result.repo]
    rows.push({
      kind: "repo",
      repo: result.repo,
      state: state,
      icon: statusIcon(state),
      color: statusColor(state, "#cacccc"),
      url: safeWebUrl(result.repoUrl, origin) || githubActionsUrl(result.repo, origin),
      notifCount: badge ? badge.count : 0,
      notifUrgent: badge ? badge.urgent : false
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
        url: safeWebUrl(run.html_url, origin)
      })
    }
  }

  // Unattributed repos (not watched, no name match) each get their own row.
  for (var extraName in attr) {
    if (watched[extraName]) continue
    var extra = attr[extraName]
    rows.push({
      kind: "repo",
      repo: extraName,
      state: STATUS_NEUTRAL,
      icon: statusIcon(STATUS_NEUTRAL),
      color: "#cacccc",
      url: githubRepoUrl(extraName, origin),
      notifCount: extra.count,
      notifUrgent: extra.urgent,
      subtitle: extra.count + " notification" + (extra.count > 1 ? "s" : "")
    })
  }
  return rows
}

function githubActionsUrl(repo, webOrigin) {
  var origin = normalizeWebOrigin(webOrigin)
  if (!origin || !validRepoName(repo)) return ""
  var pair = String(repo).split("/")
  return origin + "/" + encodeURIComponent(pair[0]) + "/" + encodeURIComponent(pair[1]) + "/actions"
}

function githubRepoUrl(repo, webOrigin) {
  var origin = normalizeWebOrigin(webOrigin)
  if (!origin || !validRepoName(repo)) return ""
  var pair = String(repo).split("/")
  return origin + "/" + encodeURIComponent(pair[0]) + "/" + encodeURIComponent(pair[1])
}

function githubAccountReposUrl(account, webOrigin) {
  var origin = normalizeWebOrigin(webOrigin)
  if (!origin || !validLogin(account)) return ""
  return origin + "/" + encodeURIComponent(account) + "?tab=repositories"
}

function githubNotificationsUrl(webOrigin) {
  var origin = normalizeWebOrigin(webOrigin)
  return origin ? origin + "/notifications" : ""
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

function isWordChar(c) {
  return (c >= "a" && c <= "z") || (c >= "0" && c <= "9")
}

// Does `text` contain `word` as a standalone term (alphanumerics only, so
// "scripture" matches inside "davidjm.scripture" but not "scripturenotes")?
function containsWord(text, word) {
  var s = String(text || "").toLowerCase()
  var w = String(word || "").toLowerCase()
  if (w === "") return false
  var i = s.indexOf(w)
  while (i !== -1) {
    var before = i === 0 || !isWordChar(s.charAt(i - 1))
    var after = i + w.length >= s.length || !isWordChar(s.charAt(i + w.length))
    if (before && after) return true
    i = s.indexOf(w, i + 1)
  }
  return false
}

// Work out which repo row each notification should be shown under. Notes on a
// watched repo stay there. Notes on an unwatched repo (threads on other
// people's repositories, marketplace issues…) are attributed to a watched
// repo whose name appears in the notification title or body text — e.g. the
// issue title "[Verify]: davidjm.scripture" badges omarchy-scripture, and a
// marketplace submission whose body links
// davidmessenger123/omarchy-github-build-monitor badges that repo — so the
// user can tell which of their repos the notification is about. Notes that
// match nothing keep their real repo as a standalone row.
function notificationAttribution(results, notifications) {
  var watchedFull = Object.create(null)
  var watchedRepos = []
  for (var i = 0; i < (results || []).length; i++) {
    var name = String((results[i] || {}).repo || "")
    if (name !== "") {
      watchedRepos.push(name)
      var tail = name
      if (tail.indexOf("/") !== -1) tail = tail.slice(tail.lastIndexOf("/") + 1)
      // Match by full repo tail ("omarchy-scripture") and, for Omarchy plugin
      // repos, by the bare core name ("scripture") so titles that mention the
      // plugin id ("[Verify]: davidjm.scripture") or just its name still land
      // on the right row.
      var tokens = { "": true }
      if (tail.length > 9 && tail.slice(0, 8) === "omarchy-")
        tokens[tail.slice(8)] = true
      tokens[tail] = true
      for (var t in tokens) if (t !== "") watchedFull[t] = name
    }
  }
  var tokensList = []
  for (var token in watchedFull) tokensList.push(token)
  tokensList.sort(function(a, b) { return b.length - a.length })
  function fullFor(token) { return watchedFull[token] || "" }
  var buckets = Object.create(null)
  function take(repo) {
    if (!buckets[repo]) buckets[repo] = { repo: repo, notes: [] }
    return buckets[repo]
  }
  for (var n = 0; n < (notifications || []).length; n++) {
    var note = notifications[n]
    var repo = String(note.repo || "")
    var target = watchedFull[repo] ? repo : ""
    if (target === "") {
      for (var j = 0; j < tokensList.length; j++) {
        if (containsWord(note.title, tokensList[j])) {
          target = fullFor(tokensList[j])
          break
        }
      }
    }
    // Thread text often names the repo it is about (marketplace bodies carry
    // the repository URL). Match the exact owner/repo path there.
    if (target === "") {
      var hay = String(note.body || "")
      if (hay !== "") {
        for (var b = 0; b < watchedRepos.length; b++)
          if (hay.indexOf(watchedRepos[b]) !== -1) {
            target = watchedRepos[b]
            break
          }
      }
    }
    // A note whose repo is not watched (threads on other people's repos)
    // keeps its real repo so it can never be lost, even with no title match.
    if (target === "" && repo !== "") target = repo
    if (target !== "") take(target).notes.push(note)
  }
  var out = Object.create(null)
  for (var key in buckets) {
    var notes = buckets[key].notes
    var urgent = false
    for (var m = 0; m < notes.length; m++)
      if (isActionable(notes[m].reason)) urgent = true
    out[key] = { repo: key, count: notes.length, urgent: urgent, notes: notes }
  }
  return out
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

function clipField(value, limit) {
  if (value === undefined || value === null) return ""
  return String(value).slice(0, limit)
}

function cleanRun(run, webOrigin) {
  if (!run || typeof run !== "object" || Array.isArray(run)) return null
  if (run.status !== undefined && run.status !== null &&
      (typeof run.status !== "string" || RUN_STATUS_VALUES.indexOf(run.status) < 0)) return null
  if (run.conclusion !== undefined && run.conclusion !== null &&
      (typeof run.conclusion !== "string" || RUN_CONCLUSION_VALUES.indexOf(run.conclusion) < 0)) return null
  var clean = {}
  var fields = ["name", "display_title", "status", "conclusion", "head_branch",
    "updated_at", "created_at"]
  for (var i = 0; i < fields.length; i++) {
    var key = fields[i]
    if (run[key] === undefined || run[key] === null) continue
    if (typeof run[key] !== "string" || run[key].length > MAX_FIELD_CHARS || /[\u0000-\u001f\u007f]/.test(run[key])) return null
    clean[key] = run[key]
  }
  if (run.html_url !== undefined && run.html_url !== null) {
    if (typeof run.html_url !== "string") return null
    var safe = safeWebUrl(run.html_url, webOrigin)
    if (!safe) return null
    clean.html_url = safe
  }
  if (run.run_number !== undefined && run.run_number !== null) {
    if (typeof run.run_number !== "number" || !isFinite(run.run_number) || run.run_number < 0) return null
    clean.run_number = Math.trunc(run.run_number)
  }
  if (Object.keys(clean).length === 0) return null
  return clean
}

function cleanResult(result, webOrigin) {
  if (!result || typeof result !== "object" || Array.isArray(result)) return null
  if (typeof result.repo !== "string" || result.repo.length === 0 || result.repo.length > 256 || !validRepoName(result.repo)) return null
  var clean = { repo: result.repo.slice(0, 256) }
  if (result.error !== undefined && result.error !== null) {
    if (typeof result.error !== "string" || result.error.length > 160 || /[\u0000-\u001f\u007f]/.test(result.error)) return null
    clean.error = result.error
    return clean
  }
  if (!Array.isArray(result.runs)) return null
  clean.runs = []
  for (var i = 0; i < result.runs.length && clean.runs.length < MAX_RUNS; i++) {
    var run = cleanRun(result.runs[i], webOrigin)
    if (run) clean.runs.push(run)
  }
  var runUrl = safeWebUrl(result.runUrl, webOrigin)
  var repoUrl = safeWebUrl(result.repoUrl, webOrigin)
  if (runUrl) clean.runUrl = runUrl
  if (repoUrl) clean.repoUrl = repoUrl
  return clean
}

function cleanNotification(note, webOrigin) {
  if (!note || typeof note !== "object" || Array.isArray(note)) return null
  if (note.id !== undefined && note.id !== null && typeof note.id !== "string" && typeof note.id !== "number") return null
  if (note.reason !== undefined && note.reason !== null && typeof note.reason !== "string") return null
  if (note.title !== undefined && note.title !== null && typeof note.title !== "string") return null
  if (note.body !== undefined && note.body !== null && typeof note.body !== "string") return null
  if (note.repo !== undefined && note.repo !== null && note.repo !== "" && !validRepoName(note.repo)) return null
  if (note.type !== undefined && note.type !== null && typeof note.type !== "string") return null
  if (note.updated_at !== undefined && note.updated_at !== null && typeof note.updated_at !== "string") return null
  var id = note.id === undefined || note.id === null ? "" : String(note.id)
  var reason = note.reason === undefined || note.reason === null ? "subscribed" : String(note.reason)
  var title = note.title === undefined || note.title === null ? "" : note.title
  var body = note.body === undefined || note.body === null ? "" : note.body
  if (id.length > 128 || reason.length > 128 || title.length > MAX_FIELD_CHARS || body.length > MAX_FIELD_CHARS) return null
  if (/[\u0000-\u001f\u007f]/.test(id + reason + title + body)) return null
  return {
    id: clipField(id, 128),
    reason: clipField(reason, 128),
    title: clipField(title, MAX_FIELD_CHARS),
    type: clipField(note.type || "Notification", 128),
    repo: validRepoName(note.repo) ? clipField(note.repo, 256) : "",
    html: safeWebUrl(note.html, webOrigin),
    body: clipField(body, MAX_FIELD_CHARS),
    updated_at: clipField(note.updated_at, 128)
  }
}

function noteKey(note) {
  note = note || {}
  return clipField(note.id, 128) + "|" + clipField(note.updated_at, 128) + "|" +
    clipField(note.reason, 128) + "|" + clipField(note.repo, 256) + "|" +
    clipField(note.title, MAX_FIELD_CHARS)
}

function notesChanged(previous, current) {
  var counts = Object.create(null)
  var i
  for (i = 0; i < (current || []).length; i++) {
    var currentKey = noteKey(current[i])
    counts[currentKey] = (counts[currentKey] || 0) + 1
  }
  for (i = 0; i < (previous || []).length; i++) {
    var previousKey = noteKey(previous[i])
    if (!counts[previousKey]) return true
    counts[previousKey]--
  }
  for (var key in counts) if (counts[key] !== 0) return true
  return false
}

function deriveState(raw, previous, webOrigin) {
  previous = previous || {}
  var origin = normalizeWebOrigin(webOrigin)
  var text = String(raw || "")
  if (text.length > MAX_RAW_CHARS) text = text.slice(0, MAX_RAW_CHARS)
  var lines = text.split("\n")
  for (var scan = 0; scan < lines.length && scan < 1100; scan++) {
    try {
      var candidate = JSON.parse(lines[scan].trim())
      if (candidate && candidate.__meta && typeof candidate.__meta === "object" &&
          typeof candidate.__meta.webOrigin === "string" && candidate.__meta.webOrigin !== "") {
        var candidateOrigin = normalizeWebOrigin(candidate.__meta.webOrigin)
        if (candidateOrigin) origin = candidateOrigin
      }
    } catch (e) {}
  }
  var results = []
  var unchanged = []
  var notifications = []
  var meta = {}
  var hasMeta = false
  var inboxUnchanged = false
  var parsed = 0
  for (var i = 0; i < lines.length && parsed < 1100; i++) {
    var line = lines[i].trim()
    if (line === "") continue
    var obj = null
    try { obj = JSON.parse(line) } catch (e) { continue }
    if (!obj || typeof obj !== "object" || Array.isArray(obj)) continue
    parsed++
    if (obj.__meta && typeof obj.__meta === "object" && !Array.isArray(obj.__meta)) {
      meta = obj.__meta
      hasMeta = true
      if (typeof meta.webOrigin === "string" && meta.webOrigin !== "") {
        var metaOrigin = normalizeWebOrigin(meta.webOrigin)
        if (metaOrigin) origin = metaOrigin
      }
    } else if (obj.__notifications === "unchanged") {
      inboxUnchanged = true
    } else if (obj.__unchanged === true && typeof obj.repo === "string") {
      if (unchanged.length < MAX_RESULTS) unchanged.push({ repo: obj.repo.slice(0, 256) })
    } else if (obj.__notification) {
      var note = typeof obj.__notification === "object" ? obj.__notification : obj
      var cleanNote = cleanNotification(note, origin)
      if (cleanNote && notifications.length < MAX_NOTIFICATIONS) notifications.push(cleanNote)
    } else {
      var clean = cleanResult(obj, origin)
      if (clean && results.length < MAX_RESULTS) results.push(clean)
    }
  }

  var previousResults = Array.isArray(previous.results) ? previous.results : []
  var previousByRepo = Object.create(null)
  for (var r = 0; r < previousResults.length; r++) {
    var old = cleanResult(previousResults[r], origin)
    if (old && typeof old.repo === "string") previousByRepo[old.repo] = old
  }
  var merged = []
  var seenRepos = Object.create(null)
  for (var u = 0; u < unchanged.length; u++) {
    var oldResult = previousByRepo[unchanged[u].repo]
    if (oldResult && !seenRepos[oldResult.repo] && merged.length < MAX_RESULTS) {
      seenRepos[oldResult.repo] = true
      merged.push(oldResult)
    }
  }
  for (var m = 0; m < results.length; m++) {
    if (!seenRepos[results[m].repo] && merged.length < MAX_RESULTS) {
      seenRepos[results[m].repo] = true
      merged.push(results[m])
    }
  }
  results = merged

  var previousNotes = Array.isArray(previous.notifications) ? previous.notifications : []
  var changed = !inboxUnchanged
  if (inboxUnchanged) {
    notifications = []
    for (var p = 0; p < previousNotes.length && notifications.length < MAX_NOTIFICATIONS; p++) {
      var previousNote = cleanNotification(previousNotes[p], origin)
      if (previousNote) notifications.push(previousNote)
    }
  } else if (!notesChanged(previousNotes, notifications)) {
    changed = false
    notifications = []
    for (var q = 0; q < previousNotes.length && notifications.length < MAX_NOTIFICATIONS; q++) {
      var unchangedNote = cleanNotification(previousNotes[q], origin)
      if (unchangedNote) notifications.push(unchangedNote)
    }
  }

  var notLoggedIn = meta.notLoggedIn === true
  var account = validLogin(meta.account) ? clipField(meta.account, 256) : ""
  var ghAvailable = meta.ghAvailable === true
  var repoCount = clampInt(meta.repoCount, 0, MAX_RESULTS)
  var session = clipField(meta.session, 128)
  var rateStatus = ""
  if (meta.remaining !== undefined && meta.remaining !== null &&
      meta.limit !== undefined && meta.limit !== null) {
    rateStatus = "Rate limit: " + clipField(meta.remaining, 32) + " / " + clipField(meta.limit, 32) + " used"
  }
  if (meta.noteError) {
    var noteError = "Notifications: " + clipField(meta.noteError, 120)
    rateStatus = rateStatus !== "" ? rateStatus + " · " + noteError : noteError
  }

  var actionableCount = 0
  for (var n = 0; n < notifications.length; n++)
    if (isActionable(notifications[n].reason)) actionableCount++

  var rows = buildPopupRows(results, notifications, origin)
  var overall = STATUS_NEUTRAL
  var statusText = "No activity yet"
  if (notLoggedIn) {
    actionableCount = 0
    rows = []
    overall = STATUS_LOGIN
    statusText = ghAvailable
      ? "No GitHub account logged in - click to sign in"
      : "gh CLI not found - install it to sign in"
  } else if (actionableCount > 0) {
    overall = STATUS_ATTENTION
    statusText = actionableCount + " notification" + (actionableCount > 1 ? "s" : "") + " waiting"
  } else if (results.length === 0) {
    if (repoCount === 0 && meta.note) {
      overall = STATUS_ERROR
      statusText = clipField(meta.note, 160)
    } else if (repoCount === 0) {
      statusText = account !== "" ? "No repositories found for " + account : "No repositories found"
    }
  } else {
    var derived = deriveOverall(results, true)
    overall = derived.overall
    statusText = derived.statusText
  }

  return {
    results: results,
    notifications: notifications,
    hasMeta: hasMeta,
    unreadCount: notifications.length,
    actionableCount: actionableCount,
    overall: overall,
    statusText: statusText,
    rows: rows,
    rateStatus: rateStatus,
    notesChanged: changed,
    notLoggedIn: notLoggedIn,
    ghAvailable: ghAvailable,
    account: account,
    webOrigin: origin,
    repoCount: notLoggedIn ? 0 : repoCount,
    session: session
  }
}

// Popup rows for the notifications section (sits above the repo runs).
// Row shapes:
//   {kind:"notifications", repo, state, icon, color, url}
//   {kind:"note",   icon, color, title, subtitle, url}
function notificationRows(notifications, actionableCount, webOrigin) {
  var origin = normalizeWebOrigin(webOrigin)
  var rows = []
  notifications = Array.isArray(notifications) ? notifications : []
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
      url: safeWebUrl(note.html, origin) || githubNotificationsUrl(origin)
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
function repoNotificationRows(repo, notifications, results, webOrigin) {
  var origin = normalizeWebOrigin(webOrigin)
  var rows = []
  notifications = Array.isArray(notifications) ? notifications : []
  results = Array.isArray(results) ? results : []
  rows.push({
    kind: "selectedRepo",
    repo: repo,
    icon: GLYPHS[STATUS_ATTENTION],
    color: COLORS[STATUS_ATTENTION],
    subtitle: "Click to close this view"
  })
  var bucket = notificationAttribution(results, notifications)[repo]
  var notes = bucket ? bucket.notes : []
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
      url: safeWebUrl(note.html, origin) || githubNotificationsUrl(origin)
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
    validLogin: validLogin,
    scriptPath: scriptPath,
    normalizeWebOrigin: normalizeWebOrigin,
    canonicalApiHost: canonicalApiHost,
    isGitHubApiHost: isGitHubApiHost,
    safeWebUrl: safeWebUrl,
    DEFAULT_WEB_ORIGIN: DEFAULT_WEB_ORIGIN,
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
    notificationAttribution: notificationAttribution,
    notificationRows: notificationRows,
    repoNotificationRows: repoNotificationRows,
    noteKey: noteKey,
    notesChanged: notesChanged,
    unconfiguredMessage: unconfiguredMessage,
    MAX_RESULTS: MAX_RESULTS,
    MAX_NOTIFICATIONS: MAX_NOTIFICATIONS,
    MAX_RAW_CHARS: MAX_RAW_CHARS,
    hasMeta: function(raw) { return deriveState(raw).hasMeta === true },
    STATUS_UNKNOWN: STATUS_UNKNOWN,
    STATUS_LOGIN: STATUS_LOGIN,
    STATUS_ATTENTION: STATUS_ATTENTION
  }
}