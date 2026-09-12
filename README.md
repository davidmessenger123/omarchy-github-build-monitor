# GitHub Notify Center

A real-time status indicator for the [Omarchy](https://omarchy.org/) shell bar
that watches the GitHub Actions CI/CD pipelines of **every repository owned by
the GitHub account currently logged in on your machine.**

The account comes from the `gh` CLI (`gh auth login`); an explicit token or
`GITHUB_TOKEN` overrides it. One pill in the bar reflects the newest workflow
state across all of those repositories:

| State | Icon | Color | Meaning |
| --- | --- | --- | --- |
| running | `circle-o-notch` | amber | A workflow is executing |
| pending | `clock-o` | blue | A workflow is queued / waiting to start |
| success | `G-circle` | green | The newest run of each repo passed |
| failure | `times` | red | A repo's newest run failed or timed out |
| action-required | `exclamation-circle` | amber | A workflow needs manual review |
| cancelled | `ban` | foreground | Newest run was cancelled |
| error | `exclamation-triangle` | orange | A repository could not be fetched |
| neutral | `G-circle` | foreground | No active pipelines, nothing alarming |
| no account | `github` | amber | You aren't signed in — click to log in |
| notification | `bell` | amber | Unread feedback needs your attention (see below) |

The calm states — everything green, nothing running yet, or an unknown result —
all show the widget's geometric-G badge (`G-circle`), so the pill reads as a
GitHub mark while its color still tells you the health.

Left-click opens a popup listing each repository's recent workflow runs
(status, workflow name, branch, commit title, when it last ran, and a link to
open the run). Right-click refreshes immediately; middle-click opens your
repositories dashboard.

## Unread notifications

The monitor also watches your unread GitHub notifications. When one of them
needs something from you specifically — a review requested on a PR you pushed,
a mention, a reply on a thread you started, or a new assignment — the pill flips
to a bell and reads "N notifications waiting". Each affected repo row in the
popup carries a bell badge with its unread count (amber when it needs action,
grey otherwise). Clicking the repo reveals its notifications, and clicking one
of those opens the page in your browser. Middle-click on the pill jumps straight
to your notifications inbox. Other push-relevant events (CI activity, security
alerts, subscriptions) stay behind the badge without flipping the pill.

## Requirements

- Omarchy 4.x with the stock shell bar
- `python3` (Arch base already includes it)
- The [`gh` CLI](https://cli.github.com/) installed and signed in — unless you
  set a `token` or `GITHUB_TOKEN` yourself (see below)
- Network access to `api.github.com` (or your GitHub Enterprise host)

## No account signed in?

While no GitHub account is logged in the pill turns amber with a GitHub glyph
and reads "No GitHub account logged in — click to sign in". Clicking it opens
your terminal on `gh auth login`; once you complete the device flow the widget
picks the account up on its next poll (a minute, or right-click to refresh
immediately).

If `gh` isn't installed you get a notification telling you how to install it.

## Install

```bash
omarchy plugin add https://github.com/davidmessenger123/omarchy-github-build-monitor --enable --yes
```

Or install by hand:

```bash
mkdir -p ~/.config/omarchy/plugins/davidjm.github-build-monitor
cp manifest.json BarWidget.qml Model.js github-builds.py ~/.config/omarchy/plugins/davidjm.github-build-monitor/
omarchy-shell shell rescanPlugins
omarchy plugin enable davidjm.github-build-monitor
```

Bar plugins land in the section from their manifest (`right`) and can be moved
with `omarchy bar move`.

## Remove

```bash
omarchy plugin remove davidjm.github-build-monitor
```

The plugin keeps no state of its own: everything it reads comes from GitHub on
each poll, and its settings (if any) live only in the entry you put in
`~/.config/omarchy/shell.json`, which `omarchy plugin remove` leaves untouched
so a reinstall keeps your configuration.

## Configure

Nothing is required — add the entry and it starts monitoring the logged-in
account. Optional settings go on the entry in `~/.config/omarchy/shell.json`:

```json
{
  "version": 1,
  "bar": {
    "layout": {
      "right": [
        { "id": "hancore.shibumi.bar" },
        {
          "id": "davidjm.github-build-monitor",
          "repos": "HANCORE-linux/Shibumi-Shell",
          "maxRepos": 30,
          "interval": 60,
          "perPage": 6
        }
      ]
    }
  }
}
```

The shell hot-reloads `shell.json` on save.

### Settings

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `repos` | string | `""` | Extra `owner/name` pairs to monitor **in addition** to your account's own repos (org-owned, teammates', …). Empty = your account only. |
| `maxRepos` | integer | `30` | How many of the account's repositories to monitor, newest-updated first. |
| `token` | string | `""` | Personal access token. Overrides the `gh` login. |
| `host` | string | `""` | API host for GitHub Enterprise. Requires a token for that host. |
| `interval` | integer | `60` | Poll interval in seconds (15–3600). |
| `perPage` | integer | `6` | Workflow runs listed per repository (1–30). |

## Rate limits

Signed in through `gh` the API limit is 5,000 requests/hour. A 1-minute poll
over 30 repos costs ~32 requests, so even smaller setups get a large headroom.
Without a token the anonymous limit drops to 60/hour — the login flow exists
precisely to keep you on the authenticated quota. The popup footer shows the
current remaining quota.

The token is **not** required to start. Set it on the widget setting or export
it for the shell process:

```sh
GITHUB_TOKEN=ghp_xxx omarchy restart shell
```

If you put it in the `token` setting it is stored in plain text in
`~/.config/omarchy/shell.json`. Either way the widget hands it to the helper
as an **environment variable** (`GITHUB_TOKEN`) on a cleared child environment
— never on the process command line, so it is not readable from `ps`. Prefer
the `gh` login if you share the config file.

## Security

The helper is designed to stay free of common credential-exposure and
resource-bound traps:

- The widget launches it as `/usr/bin/python3 -E` with a **cleared
  environment**, so nothing (PATH hijacks, inherited variables) bleeds into
  the child; the interpreter itself is an absolute path.
- Any configured token is delivered via the `GITHUB_TOKEN` **environment
  variable**, never through argv.
- Credentials are attached **only to the configured API origin**
  (`api.github.com` by default, or your `host` setting). When the helper
  follows a notification's `subject.url` to read thread text, the URL must be
  `https` on exactly that host or it is refused — a hostile URL can never
  receive the Bearer token.
- Every response is read **incrementally and capped** (4 MiB default, 512 KiB
  for subject bodies), and arrays are sliced to the requested page sizes, so
  a malformed or hostile payload cannot balloon memory.
- There is a **whole-process deadline**: the helper arms a SIGALRM wall clock
  and the shell process keeps an independent watchdog that stops the child and
  surfaces a timeout instead of hanging the recurring poll.

This is a best-effort hardening, not an audit.

## How it works

On each tick the widget runs the bundled `github-builds.py` (Python stdlib
only). It resolves the logged-in account (`GITHUB_TOKEN`, then `gh auth
token`), lists the account's owned repositories newest-updated first,
and fetches each repo's
[`GET /repos/{owner}/{repo}/actions/runs`](https://docs.github.com/en/rest/actions/workflow-runs)
in parallel. Each repository's response is printed as one JSON line, so a
failure in one repo never corrupts the others. A `__meta` line reports the
account, repo count, rate limit, and whether anyone is signed in at all. The
QML derives a widget-wide state (running > pending > failure > action-required
> error > success > neutral), colors the pill, and builds the popup list.

## License

MIT — see [`LICENSE`](LICENSE).