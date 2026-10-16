# Docs Drift Detector

Docs Drift Detector turns a release change into a prioritised, checkable list of
the documentation sections that likely need updating. It reads a changelog or a
git diff, heuristically extracts changed UI labels and routes, matches them
against your rendered documentation text plus screenshot change signals, and
produces a Markdown update list you can commit to your docs repository.

See [CONCEPT.md](CONCEPT.md) for the product concept and factory criteria.

## Run locally

No dependencies are required beyond Python 3.8+.

```sh
DDR_DATA_DIR=./data PORT=8080 python3 -m docs_drift_detector
```

Open <http://localhost:8080>. The service exposes:

- `GET  /healthz` — readiness/liveness probe.
- `GET  /api/scans` — list stored scans.
- `POST /api/scans` — create a scan (release trigger + docs + screenshots).
- `POST /api/analyze` — stateless analysis, returns findings without storing.
- `GET  /api/scans/{id}` — scan detail with findings.
- `GET  /api/scans/{id}/suppressed` — per-rule suppressed-finding drill-down.
- `GET  /api/scans/{id}/pull-request` — PR suggestion document (`base`, `prefix` optional).
- `POST /api/scans/{id}/pull-request/open` — open the branch and pull request on GitHub (`prefix` optional).
- `GET  /api/scans/{id}/export.md` — Markdown checklist export.
- `POST /api/scans/{id}/findings/{fid}/resolve` — set `{"resolved": true|false}`.
- `DELETE /api/scans/{id}` — delete a scan.
- `GET  /api/workspaces/{ws}/rules` — read the stored rule library (`[]` when unset).
- `PUT  /api/workspaces/{ws}/rules` — replace the library with `{"rules": [...]}`.
- `DELETE /api/workspaces/{ws}/rules` — clear the library.
- `GET  /api/workspaces/{ws}/rules/export` — download the library as a portable JSON document.
- `POST /api/workspaces/{ws}/rules/import` — replace the library from an exported document.
- `GET  /api/workspaces/{ws}/rules/history` — list recent library revisions, newest first.
- `POST /api/workspaces/{ws}/rules/history/{revision}/promote` — restore a revision as the live library.
- `GET  /api/workspaces/{ws}/webhook` — read the stored outbound webhook (`{"workspace": ws, "url": ""}` when unset).
- `PUT  /api/workspaces/{ws}/webhook` — set the webhook with `{"url": "https://..."}`; an empty URL clears it.
- `DELETE /api/workspaces/{ws}/webhook` — clear the webhook.
- `GET  /api/workspaces/{ws}/schedule` — read the recurring scan schedule (disabled default when unset).
- `PUT  /api/workspaces/{ws}/schedule` — set the schedule with `{"interval_hours": 1..168, "enabled": true, "scan": {...}}`; `{}` or `{"enabled": false}` disables but keeps the config.
- `DELETE /api/workspaces/{ws}/schedule` — clear the schedule.
- `GET  /api/workspaces/{ws}/github` — read the stored GitHub connector config.
- `PUT  /api/workspaces/{ws}/github` — set the connector with `{"repo": "owner/name", "base_branch": "main", "token_env": "GITHUB_TOKEN"}`; an empty `repo` clears it.
- `DELETE /api/workspaces/{ws}/github` — clear the connector config.
- `GET  /api/schedule/status` — scheduler status (`enabled`, `tick_seconds`, `due`, `schedules`).

### Scan request body

```json
{
  "name": "Release 4.2",
  "workspace": "ws1",
  "changelog": "renamed \"Save\" to \"Save changes\"\n\"/settings\" -> \"/account/settings\"",
  "diff": "optional unified diff; quoted strings in + / - lines are compared",
  "labels": [{"old": "Save", "new": "Save changes"}],
  "routes": [{"old": "/settings", "new": "/account/settings"}],
  "pages": [{"path": "help/getting-started.md", "content": "Click Save to store your work."}],
  "screenshots": [
    {"image": "img/dashboard.png", "page": "help/dashboard.md", "baseline_hash": "a1", "current_hash": "b2"}
  ],
  "rules": [
    {"kind": "label", "page": "help/*.md", "value": "Save", "reason": "intentional example label"}
  ],
  "metrics": {"help/getting-started.md": {"views": 3200, "tickets": 4}}
}
```

Only `pages` is required. Every changed label/route that still appears in a doc
page becomes a finding. Screenshots flagged as changed (explicit `"changed": true`,
differing `baseline_hash`/`current_hash`, or `changed_pixels > 0`) that are
embedded in a page become findings too.

`rules` is an optional list of suppressions that keeps known-benign findings out
of the list: each rule optionally pins a change `kind` (`label`, `route`,
`screenshot` or `*`), a `page` glob and a case-insensitive `value` substring
matched against the finding's old value (or screenshot image path). Matched
findings are excluded from scoring and export, and their count is reported as
`stats.suppressed`. Malformed rules are ignored silently.

Every normalised rule carries a stable `id` (a short hash of `kind|page|value`)
and an `origin` (`request` for inline rules, `workspace` for library rules).
Alongside the backwards-compatible integer `stats.suppressed` total, the summary
reports `stats.suppressed_rules`: a list of
`{"id", "origin", "kind", "page", "value", "count"}` entries, one per matching
rule, sorted by descending `count` then `id`. This makes it possible to audit
*why* each finding was suppressed and which rule (and library) did it.

### Suppressed finding drill-down

A stored scan also records the exact findings each rule hid, so you can see
*what* was suppressed and not only *how many* items. The details are persisted
on the scan under `suppressed_details` and are exposed per rule:

```sh
curl -sS localhost:8080/api/scans/<scan-id>/suppressed | python3 -m json.tool
```

`GET /api/scans/{id}/suppressed` returns HTTP 200 with:

```json
{
  "scan": "a1b2c3d4e5f6",
  "suppressed": 2,
  "rules": [
    {
      "rule_id": "ab12cd34ef56",
      "origin": "workspace",
      "kind": "label",
      "page": "",
      "value": "Save",
      "count": 1,
      "findings": [
        {
          "id": "0f9c...",
          "kind": "label",
          "page": "help/getting-started.md",
          "old_value": "Save",
          "new_value": "Save changes",
          "score": 40.2,
          "severity": "medium"
        }
      ]
    }
  ]
}
```

`suppressed` is the same integer as `stats.suppressed`. Each entry in `rules`
carries the matching rule metadata, a `count` and a `findings` list sorted by
descending `score`, then `page`, then `id`. Only rules that suppressed at least
one finding appear. An unknown scan id returns HTTP 404 and an invalid scan id
returns HTTP 400.

### Workspace rule libraries

`workspace` is an optional name (`[A-Za-z0-9_-]{1,64}`) recorded on a scan.
Rules can be persisted once per workspace and reused across scans instead of
being pasted into every request. When a scan/create or analyze request carries a
`workspace`, the stored library rules are merged with the request `rules`: the
per-scan `rules` take precedence and **both** sets are applied, so the combined
suppressions are reflected in `stats.suppressed`. Requests without a `workspace`
behave exactly as before. Suppressions from inline rules are tagged
`origin=request`; those from the stored library are tagged `origin=workspace`.

```sh
curl -sS -X PUT localhost:8080/api/workspaces/ws1/rules \
  -H 'content-type: application/json' \
  --data '{"rules":[{"kind":"label","value":"Save"}]}' | python3 -m json.tool
```

Invalid workspace ids (for example `bad id!`) return HTTP 400 on all workspace
endpoints. The same name restriction applies when a scan or analyze request
carries a `workspace`.

#### Portable rule-library import/export

A workspace library can be moved between environments (staging → production) or
between workspaces as a single JSON document:

```sh
curl -sS localhost:8080/api/workspaces/ws1/rules/export \
  -o workspace-ws1-rules.json

curl -sS -X POST localhost:8080/api/workspaces/ws2/rules/import \
  -H 'content-type: application/json' \
  --data @workspace-ws1-rules.json | python3 -m json.tool
```

`GET .../rules/export` returns HTTP 200 with `Content-Type: application/json`, a
`Content-Disposition` attachment filename and this document shape:

```json
{
  "version": 1,
  "workspace": "ws1",
  "rules": [
    {"kind": "label", "page": "help/*.md", "value": "Save", "reason": "example", "id": "ab12cd34ef56", "origin": "workspace"}
  ]
}
```

`POST .../rules/import` accepts that document (or a bare `{"rules": [...]}`),
normalises it, replaces the stored library and returns
`{"rules": [...], "imported": n}` where `imported` is the number of stored
rules. A body without a usable `rules` list returns HTTP 400, as does an invalid
workspace id. `examples/sample_workspace_rules.json` is an example document.

#### Rule-library history and promotion

Every successful `PUT /api/workspaces/{ws}/rules`,
`POST /api/workspaces/{ws}/rules/import` and `DELETE /api/workspaces/{ws}/rules`
appends a revision to an append-only history for that workspace. History is
capped at `MAX_RULE_HISTORY` (20) revisions per workspace; once the cap is
reached the oldest revision is dropped. Revisions share the same JSON store as
scans, so no database or extra dependency is required.

```sh
curl -sS localhost:8080/api/workspaces/ws1/rules/history | python3 -m json.tool

curl -sS -X POST localhost:8080/api/workspaces/ws1/rules/history/1/promote \
  | python3 -m json.tool
```

`GET .../rules/history` returns HTTP 200 with
`{"workspace": "ws1", "history": [...]}`, newest revision first. Each entry has:

```json
{
  "revision": 3,
  "created_at": "2024-01-01T12:00:00+00:00",
  "action": "set",
  "rule_count": 1,
  "rules": [
    {"kind": "label", "page": "", "value": "Save", "reason": "", "id": "ab12cd34ef56", "origin": "workspace"}
  ]
}
```

`action` is one of `set` (PUT), `import` (import), `clear` (DELETE) or `promote`.
`POST .../rules/history/{revision}/promote` restores the rules of that revision as
the current library, records a new `promote` revision and returns HTTP 200 with
`{"rules": [...], "promoted": revision}`. An unknown revision returns HTTP 404,
and an invalid workspace id or non-integer revision returns HTTP 400.

### Outbound webhooks

A webhook URL can be registered once per workspace so a documentation team is
notified in Slack, Teams or CI without polling the UI. After a stored scan for
that workspace is created successfully, the service POSTs a JSON payload to the
URL:

```sh
curl -sS -X PUT localhost:8080/api/workspaces/ws1/webhook \
  -H 'content-type: application/json' \
  --data '{"url":"https://hooks.example.com/ddr"}'

curl -sS localhost:8080/api/workspaces/ws1/webhook | python3 -m json.tool

curl -sS -X DELETE localhost:8080/api/workspaces/ws1/webhook
```

- `GET .../webhook` returns HTTP 200 with `{"workspace": "ws1", "url": "..."}`;
  `url` is `""` when unset. An invalid workspace id returns HTTP 400.
- `PUT .../webhook` accepts `{"url": "https://..."}` and returns the stored
  value. The URL must be `http`/`https` and at most 500 characters; anything
  else (for example `ftp://x` or a URL longer than 500 chars) returns HTTP 400.
  An empty `url` clears the webhook.
- `DELETE .../webhook` clears it and returns `{"deleted": "ws1"}`.

The POSTed body is produced by `notify.build_payload`:

```json
{
  "scan_id": "a1b2c3d4e5f6",
  "name": "Release 4.2",
  "workspace": "ws1",
  "created_at": "2026-01-01T12:00:00+00:00",
  "url": "/api/scans/a1b2c3d4e5f6",
  "findings": [
    {
      "id": "0f9c...",
      "kind": "label",
      "page": "help/getting-started.md",
      "title": "UI label \"Save\" changed in the release",
      "old_value": "Save",
      "new_value": "Save changes",
      "score": 95.0,
      "severity": "high"
    }
  ]
}
```

Only `severity == "high"` findings are sent, sorted by descending `score` (then
page, then id); a scan with no high-severity findings sends an empty `findings`
list. Delivery is best-effort: a single plain `urllib.request` POST with
`content-type: application/json`, no retry queue and no HMAC signing. A missing
or failing endpoint is logged to stderr and never blocks or changes the scan
response, so `POST /api/scans` still returns HTTP 201 with the unchanged scan
document.

### Scheduled drift scans and digests

A workspace can register a recurring drift check so the team does not have to
remember to create a scan after every release. The background scheduler runs
each enabled schedule that is due, stores the resulting scan exactly like a
manual one and POSTs a digest to the workspace webhook.

```sh
# register a nightly scan for ws1
curl -sS -X PUT localhost:8080/api/workspaces/ws1/schedule \
  -H 'content-type: application/json' \
  --data '{
    "interval_hours": 24,
    "enabled": true,
    "scan": {
      "name": "Nightly drift",
      "changelog": "renamed \"Save\" to \"Save changes\"",
      "pages": [{"path": "help/getting-started.md", "content": "Click Save."}]
    }
  }' | python3 -m json.tool

curl -sS localhost:8080/api/workspaces/ws1/schedule | python3 -m json.tool
curl -sS -X DELETE localhost:8080/api/workspaces/ws1/schedule
curl -sS localhost:8080/api/schedule/status | python3 -m json.tool
```

`GET .../schedule` returns the stored config (or a disabled default with
`interval_hours: 0` when unset). `PUT .../schedule` accepts:

```json
{
  "interval_hours": 24,
  "enabled": true,
  "scan": {"name": "Nightly drift", "changelog": "...", "pages": []}
}
```

- `interval_hours` must be an integer in the range `1..168` (whole hours only;
  there is no cron expression support). A non-integer, `0`, `200` or `"soon"`
  returns HTTP 400.
- `scan` is the same payload shape as `POST /api/scans` and is required when
  setting an interval; a body without a usable `scan` object returns HTTP 400.
- An empty body `{}` or `{"enabled": false}` disables the schedule but keeps the
  stored interval and scan payload. `DELETE` clears it and returns
  `{"deleted": "ws1"}`.
- An invalid workspace id returns HTTP 400 on every schedule route.

When a schedule is due, the scheduler runs the stored `scan` payload through the
ordinary analysis path, stores the scan (with the schedule's `workspace`) and
advances `next_run_at` by `interval_hours`. It then POSTs a **digest** payload
built by `notify.build_digest` to the workspace webhook:

```json
{
  "scan_id": "a1b2c3d4e5f6",
  "workspace": "ws1",
  "created_at": "2026-01-01T12:00:00+00:00",
  "total_findings": 3,
  "by_severity": {"high": 1, "medium": 1, "low": 1},
  "new_high": [
    {
      "id": "0f9c...",
      "kind": "label",
      "page": "help/getting-started.md",
      "title": "UI label \"Save\" changed in the release",
      "old_value": "Save",
      "new_value": "Save changes",
      "score": 95.0,
      "severity": "high"
    }
  ],
  "url": "/api/scans/a1b2c3d4e5f6"
}
```

`by_severity` counts every finding in the scan. `new_high` reuses the
high-severity scope of `notify.build_payload` and keeps only findings whose
`(kind, page, old_value, new_value)` key was absent from the previous stored
scan for that workspace, so a schedule that fires with no changes sends an empty
`new_high` list. Digest delivery is the same best-effort webhook POST as
ordinary notifications: a failure is logged to stderr and never stops the
scheduler or the other due schedules.

`GET /api/schedule/status` always returns HTTP 200 with:

```json
{
  "enabled": true,
  "tick_seconds": 60,
  "due": [{"workspace": "ws1", "next_run_at": "2026-01-01T00:00:00+00:00", "interval_hours": 24}],
  "schedules": [{"workspace": "ws1", "interval_hours": 24, "enabled": true, "scan": {}, "next_run_at": "2026-01-01T00:00:00+00:00"}]
}
```

The scheduler is started by `python -m docs_drift_detector` as a daemon thread
and shut down cleanly on `SIGTERM`/`SIGINT`. It is **single-process, in-memory
and best-effort**: it runs only inside the HTTP process that started it, a
process restart skips any tick that was missed while it was down, and there is
no cron expression support. Two environment variables control it:

- `DDR_SCHEDULER_TICK` — scheduler tick in seconds (default `60`).
- `DDR_SCHEDULER_ENABLED` — set to `0` to disable the background scheduler
  (default `1`); the status endpoint still works and reports `"enabled": false`.

### Pull-request suggestion

A stored scan can be turned into a PR suggestion document that a team (or a thin
CI wrapper) can use to open a branch and pull request in the documentation
repository. It reuses the exact high-severity scope of outbound webhooks, so a
notification and a PR suggestion always describe the same findings.

```sh
curl -sS 'localhost:8080/api/scans/<scan-id>/pull-request?base=main&prefix=docs-drift' \
  | python3 -m json.tool
```

`GET /api/scans/{id}/pull-request` returns HTTP 200 with
`Content-Type: application/json` and this document shape (see
`examples/sample_pull_request.json`):

```json
{
  "scan_id": "a1b2c3d4e5f6",
  "name": "Release 4.2 preview",
  "workspace": "ws1",
  "created_at": "2026-01-01T12:00:00+00:00",
  "branch": "docs-drift/a1b2c3d4e5f6",
  "base_branch": "main",
  "title": "docs: address drift from Release 4.2 preview",
  "body": "## Docs drift update: ...",
  "files": [
    {"path": "help/getting-started.md", "action": "update", "reason": "Route \"/settings\" changed in the release"}
  ],
  "checklist": "- [ ] **[HIGH] Route \"/settings\" changed in the release** ..."
}
```

- `branch` is deterministic: `<prefix>/<scan_id>`, defaulting to
  `docs-drift/<scan_id>`.
- `base_branch` defaults to `main`.
- `title` is `docs: address drift from <scan name>`.
- `body` is Markdown with a summary line and the high-severity findings sorted by
  descending score, each shown as page and old → new value.
- `files` is the deduplicated, path-sorted list of `{path, action: "update",
  reason}` for the distinct pages of the scan's high-severity findings.
- `checklist` reuses `export.to_markdown` (the same Markdown produced by
  `GET /api/scans/{id}/export.md`).

The high-severity finding selection and ordering come from
`notify.build_payload`, the same function the per-workspace webhook uses, so the
scope is identical. Optional `base` and `prefix` query parameters are validated
against `[A-Za-z0-9._/-]`; anything else returns HTTP 400. An unknown scan id
returns HTTP 404 and an invalid scan id returns HTTP 400, matching the other scan
routes. A scan with no high-severity findings still returns HTTP 200 with an
empty `files` list and a body stating that no action is required.

### Opening the pull request on GitHub

The connector turns the same high-severity suggestion into a real branch and
pull request through the GitHub REST API. It is dependency-free (standard-library
`urllib.request` only) and is configured once per workspace:

```sh
curl -sS -X PUT localhost:8080/api/workspaces/ws1/github \
  -H 'content-type: application/json' \
  --data '{"repo":"acme/help-center","base_branch":"main","token_env":"GITHUB_TOKEN"}'

curl -sS localhost:8080/api/workspaces/ws1/github | python3 -m json.tool

# open a branch and pull request for an existing scan
curl -sS -X POST localhost:8080/api/scans/<scan-id>/pull-request/open \
  | python3 -m json.tool

curl -sS -X DELETE localhost:8080/api/workspaces/ws1/github
```

- `GET .../github` returns HTTP 200 with the stored config, or the default
  `{"workspace": ws, "repo": "", "base_branch": "main", "token_env":
  "GITHUB_TOKEN"}` when unset. An invalid workspace id returns HTTP 400.
- `PUT .../github` requires a `repo`. `repo` must be `owner/name`, `base_branch`
  must match `[A-Za-z0-9._/-]{1,200}` (default `main`) and `token_env` must be a
  valid environment-variable name (default `GITHUB_TOKEN`). Anything else returns
  HTTP 400. An empty `repo` (for example `{"repo": ""}`) clears the config.
- `DELETE .../github` clears it and returns `{"deleted": "ws1"}`.

The token itself is never stored: `token_env` names the environment variable the
running process reads when it opens the PR. `POST /api/scans/{id}/pull-request/open`
builds the suggestion with the stored `base_branch` (an optional `prefix` query
parameter overrides the `docs-drift` branch prefix) and calls the connector:

- an unknown scan id returns HTTP 404; an invalid id, a scan without a configured
  workspace repo, or an unset `token_env` returns HTTP 400;
- otherwise it returns HTTP 200 with the connector result
  `{"ok", "status", "error", "pr_url", "branch"}` (see
  `examples/sample_github_pull_request.json`). An upstream failure (for example a
  branch that already exists, a bad token or an unreachable API) is reported as
  HTTP 200 with `{"ok": false, "error": "..."}` rather than failing the request.

The connector resolves the base branch, creates the new ref, commits the exported
drift checklist as `docs-drift/<scan_id>.md` and opens the pull request. It never
force-pushes: if the branch already exists the call fails instead of moving it.
After every stored scan for a workspace **with a configured repo**, the service
also opens the PR automatically as a best-effort step, exactly like the outbound
webhook: a missing config is a no-op and any failure is logged to stderr and
never changes the scan response, so `POST /api/scans` still returns HTTP 201 with
the unchanged scan document.

### Example

```sh
curl -sS localhost:8080/api/analyze \
  -H 'content-type: application/json' \
  --data @examples/sample_scan.json | python3 -m json.tool
```

## Test

```sh
python3 -m unittest discover -s tests -v
```

The development pipeline tests are separate:
`python3 -m unittest discover -s .github/scripts -p 'test_*.py' -v`.

## Container and deployment

The `Dockerfile` builds a non-root image exposing port 8080 with `GET /healthz`.
The Helm chart lives in `deploy/helm/docs-drift-detector/` and is rendered with
`image.repository` + `image.digest` as `repository@digest`, optional ingress via
`ingress.enabled`, `ingress.host` and `ingress.className`, resource
requests/limits, non-root execution and a persistent volume for `/data`
(`DDR_DATA_DIR=/data`).

```sh
helm template deploy/helm/docs-drift-detector \
  --set image.repository=ghcr.io/example/docs-drift-detector \
  --set image.digest=sha256:0000000000000000000000000000000000000000000000000000000000000000
```

The `factory-release.yml` workflow builds reviewed merges and publishes the
multi-arch image manifest; the Factory manager publishes the chart.
