# GitHub Build Monitor

A real-time status indicator for the [Omarchy](https://omarchy.org/) shell bar
that shows the health of your GitHub Actions CI/CD pipelines.

One pill in the bar reflects the newest workflow state across every monitored
repository:

| State | Icon | Color | Meaning |
| --- | --- | --- | --- |
| running | `circle-o-notch` | amber | A workflow is executing |
| pending | `clock-o` | blue | A workflow is queued / waiting to start |
| success | `check` | green | The newest run of each repo passed |
| failure | `times` | red | A repo's newest run failed or timed out |
| action-required | `exclamation-circle` | amber | A workflow needs manual review |
| cancelled | `ban` | foreground | Newest run was cancelled |
| error | `exclamation-triangle` | orange | A repository could not be fetched |
| neutral | `square-o` | foreground | No active pipelines, nothing alarming |

Left-click opens a popup listing each repository's recent workflow runs
(status, workflow name, branch, commit title, when it last ran, and a link to
open the run). Right-click refreshes immediately; the middle-click opens the
first repository's Actions page.

## Requirements

- Omarchy 4.x with the stock shell bar
- `python3` (Arch base already includes it)
- Network access to `api.github.com` (or your GitHub Enterprise host)

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

## Configure

The widget hides itself until at least one repository is configured. Add the
entry to `~/.config/omarchy/shell.json` under the section you want:

```json
{
  "version": 1,
  "bar": {
    "layout": {
      "right": [
        { "id": "hancore.shibumi.bar" },
        {
          "id": "davidjm.github-build-monitor",
          "repos": "HANCORE-linux/Shibumi-Shell, cli/cli",
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
| `repos` | string | `""` | Comma-separated `owner/name` pairs. Required. |
| `token` | string | `""` | Optional GitHub personal access token. |
| `host` | string | `""` | API host for GitHub Enterprise (e.g. `github.example.com`). |
| `interval` | integer | `60` | Poll interval in seconds (15–3600). |
| `perPage` | integer | `6` | Workflow runs listed per repository (1–30). |

## Rate limits

Without a token GitHub allows 60 requests/hour per IP. With a token (created
with `repo` scope) the ceiling is 5,000/hour, which comfortably covers a
1-minute poll. The widget shows the remaining quota in the popup footer.

The token is **not** required to start. Set it on the widget setting or export
it for the shell process:

```sh
GITHUB_TOKEN=ghp_xxx omarchy restart shell
```

The token is stored in plain text in `~/.config/omarchy/shell.json` if you put
it there, and is passed as an argument to a short-lived Python process. Prefer
the environment variable if you share the config file.

## How it works

On each tick the widget runs the bundled `github-builds.py` (Python stdlib
only) against the
[`GET /repos/{owner}/{repo}/actions/runs`](https://docs.github.com/en/rest/actions/workflow-runs)
endpoint. Each repository's response is printed as one JSON line, so a failure
in one repo never corrupts the others. The QML parses those lines, derives a
widget-wide state (running > pending > failure > action-required > error >
success > neutral), colors the pill, and builds the popup list.

## License

MIT — see [`LICENSE`](LICENSE).