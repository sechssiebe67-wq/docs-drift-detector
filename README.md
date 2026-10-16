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
- `GET  /api/workspaces/{ws}/retention` — read the per-workspace scan retention limit (`0` when unset).
- `PUT  /api/workspaces/{ws}/retention` — set the limit with `{"max_scans": 0..10000}`; `0` clears the policy.
- `DELETE /api/workspaces/{ws}/retention` — clear the retention policy.
- `GET  /api/schedule/status` — scheduler status (`enabled`, `tick_seconds`, `due`, `schedules`).
- `GET  /api/workspaces/{ws}/digests` — recent scheduled-scan digests for a workspace, newest first (`limit` optional, default `10`).
- `GET  /api/workspaces/{ws}/trend` — chronological drift trend across a workspace's scans (`limit` optional, default `10`, max `50`; `from`/`to` optional inclusive window; `cursor` optional page cursor).
- `GET  /api/workspaces/{ws}/trend/buckets` — time-bucketed drift trend (`bucket` optional `day`|`week`, default `week`; `limit` optional, default `10`, max `50`; `from`/`to` optional inclusive window; `cursor` optional page cursor).
- `GET  /api/trend` — cross-workspace drift roll-up across the scans of all workspaces (`limit` optional, default `10`, max `50`; `from`/`to` optional inclusive window; `cursor` optional page cursor).
- `GET  /api/trend/buckets` — cross-workspace time-bucketed drift roll-up (`bucket` optional `day`|`week`, default `week`; `limit` optional, default `10`, max `50`; `from`/`to` optional inclusive window; `cursor` optional page cursor).
- `GET  /api/workspaces/{ws}/trend/buckets.csv` — time-bucketed drift trend as a CSV attachment (same query parameters as `/trend/buckets`).
- `GET  /api/trend/buckets.csv` — cross-workspace time-bucketed drift roll-up as a CSV attachment (same query parameters as `/trend/buckets`).

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

#### Digest history

So a missed or noisy digest can be audited after the fact instead of only being
visible in the webhook receiver's logs, the scheduler records every digest it
builds in the same JSON store as scans:

```sh
curl -sS 'localhost:8080/api/workspaces/ws1/digests?limit=5' | python3 -m json.tool
```

`GET /api/workspaces/{ws}/digests` returns HTTP 200 with:

```json
{
  "workspace": "ws1",
  "digests": [
    {
      "created_at": "2026-01-01T12:00:00+00:00",
      "scan_id": "a1b2c3d4e5f6",
      "total_findings": 3,
      "by_severity": {"high": 1, "medium": 1, "low": 1},
      "new_high_count": 1,
      "delivery_ok": true,
      "delivery_error": ""
    }
  ]
}
```

- Entries are ordered **newest-first** and capped at `MAX_DIGEST_HISTORY = 20`
  per workspace (the oldest is dropped). The history is stored under
  `workspace_digests` in the single JSON file, keyed by workspace only, with no
  pagination and no time-based expiry.
- The optional `limit` query parameter is an integer; values outside `1..20` are
  clamped and a non-integer (for example `limit=abc`) returns HTTP 400. The
  default is `10`.
- Recording is best-effort: every due scheduled scan appends exactly one entry.
  A missing, failing or erroring webhook still records the attempt with
  `delivery_ok: false` and a non-empty `delivery_error`, and neither changes the
  stored scan nor stops the scheduler.
- An invalid workspace id (for example `bad id!`) returns HTTP 400. A
  well-formed but unknown workspace returns HTTP 200 with an empty `digests`
  list.

The scheduler is started by `python -m docs_drift_detector` as a daemon thread
and shut down cleanly on `SIGTERM`/`SIGINT`. It is **single-process, in-memory
and best-effort**: it runs only inside the HTTP process that started it, a
process restart skips any tick that was missed while it was down, and there is
no cron expression support. Two environment variables control it:

- `DDR_SCHEDULER_TICK` — scheduler tick in seconds (default `60`).
- `DDR_SCHEDULER_ENABLED` — set to `0` to disable the background scheduler
  (default `1`); the status endpoint still works and reports `"enabled": false`.

### Scan retention

Stored scans accumulate in the single JSON file. A per-workspace retention limit
caps how many scans a workspace keeps; after every stored scan for that workspace
the oldest scans beyond the limit are pruned automatically. Other workspaces and
scans without a workspace are never touched.

```sh
# keep at most the 50 most recent ws1 scans
curl -sS -X PUT localhost:8080/api/workspaces/ws1/retention \
  -H 'content-type: application/json' \
  --data '{"max_scans":50}' | python3 -m json.tool

curl -sS localhost:8080/api/workspaces/ws1/retention | python3 -m json.tool
curl -sS -X DELETE localhost:8080/api/workspaces/ws1/retention
```

- `GET .../retention` returns HTTP 200 with
  `{"workspace": "ws1", "max_scans": 0}` when unset, otherwise the stored limit.
- `PUT .../retention` accepts `{"max_scans": n}` where `n` is an integer in
  `0..10000`. A non-integer, a negative number or `10001` returns HTTP 400. A
  value of `0` clears the policy and returns `{"workspace": "ws1", "max_scans":
  0}`; a positive value is stored and returned.
- `DELETE .../retention` clears the policy and returns `{"deleted": "ws1"}`.
- An invalid workspace id returns HTTP 400 on all three routes.

Pruning happens automatically after each stored scan for that workspace, whether
the scan was created manually or by the scheduler. It is best-effort: a failure
(for example a malformed stored retention value) is logged to stderr and never
changes the `POST /api/scans` response, which still returns HTTP 201 with the
unchanged scan document. The policy is stored alongside the scans under
`workspace_retention`, and reloading a store with a missing or malformed key
treats every workspace as unset.

### Workspace drift trend

To answer "are we getting better or worse at keeping docs in sync?", a
workspace's recent stored scans can be summarised into a small chronological
time series. The endpoint is read-only: it reuses the stored scans, never
mutates the store, and never dispatches a webhook, opens a pull request or
records a digest.

```sh
curl -sS 'localhost:8080/api/workspaces/ws1/trend?limit=5' | python3 -m json.tool
```

`GET /api/workspaces/{ws}/trend` returns HTTP 200 with:

```json
{
  "workspace": "ws1",
  "scans": [
    {
      "scan_id": "a1b2c3d4e5f6",
      "created_at": "2026-01-01T12:00:00+00:00",
      "name": "Release 4.2",
      "total_findings": 3,
      "by_severity": {"high": 1, "medium": 1, "low": 1},
      "open_findings": 2,
      "high_open": 1,
      "suppressed": 0
    }
  ],
  "summary": {
    "scans": 1,
    "total_findings": 3,
    "by_severity": {"high": 1, "medium": 1, "low": 1},
    "open_findings": 2,
    "high_open": 1,
    "suppressed": 0,
    "delta_high": 0
  },
  "next_cursor": null
}
```

- The window selects the workspace's stored scans **newest-first**, takes up to
  `limit`, then presents them **oldest-first** so `scans` reads as a
  chronological series. `next_cursor` is `null` here because this page reaches
  the oldest scan; see [Pagination](#pagination-trend-endpoints). Counting a
  finding as *open* means it is not resolved; `by_severity` and openness match
  the stored scan semantics used by webhooks, digests and PR suggestions.
- `summary.delta_high` is `newest.high_open - oldest.high_open` across the
  window (a positive value means the newest scan has more open high-severity
  findings than the oldest). It is `0` when fewer than two scans are in the
  window. The other summary fields aggregate the window.
- The optional `limit` query parameter is an integer (default `10`) clamped into
  `1..50`; a non-integer such as `limit=abc` returns HTTP 400.
- The optional `from`/`to` query parameters restrict the window to scans whose
  `created_at` falls inside the inclusive UTC range; see
  [Date-range filtering](#date-range-filtering-trend-endpoints) below.
- An invalid workspace id (for example `bad id!`) returns HTTP 400. A
  well-formed but unknown workspace returns HTTP 200 with an empty `scans` list
  and a zeroed `summary`.
- The trend is bounded per page and workspace-keyed only: there is no
  cross-workspace or global trend and no time-based bucketing beyond scan order,
  but older pages can be fetched with the optional `cursor` (see
  [Pagination](#pagination-trend-endpoints) below). The scans live in the single
  JSON store.

### Cross-workspace drift roll-up

Where the per-workspace trend answers "is *this* workspace getting better or
worse?", `GET /api/trend` gives a portfolio-level view by aggregating the recent
stored scans of **all** workspaces (plus scans with no workspace, grouped under
the empty-string bucket) into a single chronological window. It is read-only: it
reuses the stored scans and never mutates the store or dispatches a webhook,
opens a pull request or records a digest.

```sh
curl -sS 'localhost:8080/api/trend?limit=5' | python3 -m json.tool
```

`GET /api/trend` returns HTTP 200 with:

```json
{
  "scans": [
    {
      "scan_id": "a1b2c3d4e5f6",
      "workspace": "ws1",
      "created_at": "2026-01-01T12:00:00+00:00",
      "name": "Release 4.2",
      "total_findings": 3,
      "by_severity": {"high": 1, "medium": 1, "low": 1},
      "open_findings": 2,
      "high_open": 1,
      "suppressed": 0
    },
    {
      "scan_id": "b2c3d4e5f6a1",
      "workspace": "ws2",
      "created_at": "2026-01-02T12:00:00+00:00",
      "name": "Release 4.3",
      "total_findings": 2,
      "by_severity": {"high": 2, "medium": 0, "low": 0},
      "open_findings": 2,
      "high_open": 2,
      "suppressed": 0
    }
  ],
  "workspaces": [
    {
      "workspace": "ws2",
      "scans": 1,
      "total_findings": 2,
      "by_severity": {"high": 2, "medium": 0, "low": 0},
      "open_findings": 2,
      "high_open": 2,
      "suppressed": 0,
      "delta_high": 0
    },
    {
      "workspace": "ws1",
      "scans": 1,
      "total_findings": 3,
      "by_severity": {"high": 1, "medium": 1, "low": 1},
      "open_findings": 2,
      "high_open": 1,
      "suppressed": 0,
      "delta_high": 0
    }
  ],
  "summary": {
    "scans": 2,
    "workspaces": 2,
    "total_findings": 5,
    "by_severity": {"high": 3, "medium": 1, "low": 1},
    "open_findings": 4,
    "high_open": 3,
    "suppressed": 0,
    "delta_high": 1
  },
  "next_cursor": null
}
```

- `scans` holds the window **oldest-first** and each entry adds `workspace`
  (empty string when the scan has none) to the exact same per-scan summary that
  `GET /api/workspaces/{ws}/trend` reports, so the two endpoints agree on
  `total_findings`, `by_severity`, `open_findings`, `high_open` and
  `suppressed` for a given scan.
- `workspaces` aggregates the window per workspace, sorted by descending
  `high_open` then workspace name. Each entry's `delta_high` is
  `newest.high_open - oldest.high_open` within that workspace's window (`0`
  when the window holds fewer than two scans for it); a negative value means the
  newest scan has fewer open high-severity findings than the oldest.
- `summary` aggregates the whole window (`scans`, `workspaces`,
  `total_findings`, `by_severity`, `open_findings`, `high_open`, `suppressed`)
  and adds `delta_high = newest.high_open - oldest.high_open` across the window
  (`0` when fewer than two scans are in the window). `summary.total_findings`
  equals the sum of the per-workspace `total_findings`.
- The optional `limit` query parameter is an integer (default `10`) clamped into
  `1..50`; a non-integer such as `limit=abc` returns HTTP 400.
- Malformed stored scans (a non-dict entry, or one with a missing/invalid `id`
  or `created_at`) are skipped without changing the HTTP status. On an empty
  store the response is HTTP 200 with empty `scans` and `workspaces` and a
  zeroed `summary`.
- The roll-up is bounded per page and keyed by workspace only: one chronological
  window in scan order rather than time buckets. The optional `from`/`to` window
  and `cursor` described below apply here too. The scans live in the single JSON
  store.

### Time-bucketed drift trend

The trend and roll-up endpoints above present scans in *scan order* within a
single window. To compare calendar periods instead — for example a release week
against a quiet week, or to watch high-severity drift accumulate week over week —
`GET /api/workspaces/{ws}/trend/buckets` groups a workspace's stored scans into
day or ISO-week buckets by `created_at`. Both bucket routes are read-only: they
reuse the stored scans and never mutate the store or dispatch a webhook, open a
pull request or record a digest.

```sh
curl -sS 'localhost:8080/api/workspaces/ws1/trend/buckets?bucket=week&limit=5' \
  | python3 -m json.tool
```

`GET /api/workspaces/{ws}/trend/buckets` returns HTTP 200 with:

```json
{
  "workspace": "ws1",
  "bucket": "week",
  "buckets": [
    {
      "key": "2026-W02",
      "start": "2026-01-05T00:00:00+00:00",
      "end": "2026-01-11T23:59:59+00:00",
      "scans": 2,
      "total_findings": 3,
      "by_severity": {"high": 2, "medium": 1, "low": 0},
      "open_findings": 3,
      "high_open": 2,
      "suppressed": 0
    }
  ],
  "summary": {
    "scans": 2,
    "buckets": 1,
    "total_findings": 3,
    "by_severity": {"high": 2, "medium": 1, "low": 0},
    "open_findings": 3,
    "high_open": 2,
    "suppressed": 0,
    "delta_high": 0
  },
  "next_cursor": null
}
```

- `bucket` selects the period width: `day` (UTC calendar day) or `week` (ISO
  week starting Monday, UTC). The default is `week`. A missing value defaults to
  `week`; any other value (for example `bucket=month` or an empty value) returns
  HTTP 400.
- Each bucket carries a stable `key` (`2026-01-05` for a day, `2026-W02` for a
  week) and a UTC ISO-8601 `start`/`end` range (inclusive last second of the
  period). `scans` is the number of stored scans in the period and
  `total_findings`, `by_severity`, `open_findings`, `high_open` and `suppressed`
  are the same per-scan counts the trend/roll-up endpoints report, so the two
  views never diverge. Buckets are ordered **oldest-first**.
- `summary` aggregates the whole window and adds `delta_high`
  (`newest bucket high_open - oldest bucket high_open`, `0` when fewer than two
  buckets are in the window) plus a `buckets` count.
- The optional `limit` query parameter is an integer (default `10`) clamped into
  `1..50`; it bounds the number of buckets, keeping the most recent ones. A
  non-integer such as `limit=abc` returns HTTP 400. `limit=0` and `limit=999`
  are clamped rather than rejected.
- Malformed stored scans (a non-dict entry, or one with a missing/invalid `id`
  or `created_at`) are skipped without changing the HTTP status. An invalid
  workspace id (for example `bad id!`) returns HTTP 400; a well-formed but
  unknown workspace returns HTTP 200 with an empty `buckets` list.

The cross-workspace companion applies the same bucketing to **all** workspaces
(plus scans with no workspace, grouped under the empty-string bucket):

```sh
curl -sS 'localhost:8080/api/trend/buckets?bucket=day' | python3 -m json.tool
```

`GET /api/trend/buckets` returns HTTP 200 with:

```json
{
  "bucket": "day",
  "buckets": [
    {
      "key": "2026-01-05",
      "start": "2026-01-05T00:00:00+00:00",
      "end": "2026-01-05T23:59:59+00:00",
      "scans": 2,
      "total_findings": 3,
      "by_severity": {"high": 2, "medium": 1, "low": 0},
      "open_findings": 3,
      "high_open": 2,
      "suppressed": 0,
      "workspaces": [
        {
          "workspace": "ws1",
          "scans": 1,
          "total_findings": 3,
          "by_severity": {"high": 2, "medium": 1, "low": 0},
          "open_findings": 3,
          "high_open": 2,
          "suppressed": 0,
          "delta_high": 0
        },
        {
          "workspace": "ws2",
          "scans": 1,
          "total_findings": 0,
          "by_severity": {"high": 0, "medium": 0, "low": 0},
          "open_findings": 0,
          "high_open": 0,
          "suppressed": 0,
          "delta_high": 0
        }
      ]
    }
  ],
  "workspaces": [
    {
      "workspace": "ws1",
      "scans": 1,
      "total_findings": 3,
      "by_severity": {"high": 2, "medium": 1, "low": 0},
      "open_findings": 3,
      "high_open": 2,
      "suppressed": 0,
      "delta_high": 0
    },
    {
      "workspace": "ws2",
      "scans": 1,
      "total_findings": 0,
      "by_severity": {"high": 0, "medium": 0, "low": 0},
      "open_findings": 0,
      "high_open": 0,
      "suppressed": 0,
      "delta_high": 0
    }
  ],
  "summary": {
    "scans": 2,
    "buckets": 1,
    "total_findings": 3,
    "by_severity": {"high": 2, "medium": 1, "low": 0},
    "open_findings": 3,
    "high_open": 2,
    "suppressed": 0,
    "delta_high": 0,
    "workspaces": 2
  },
  "next_cursor": null
}
```

- Each bucket adds a `workspaces` breakdown aggregated per workspace and sorted
  by descending `high_open` then workspace name (scans with no workspace appear
  under the empty-string bucket), mirroring the top-level `workspaces` list that
  aggregates the whole window.
- `summary` aggregates the window plus `delta_high` and a distinct-`workspaces`
  count. `bucket`/`limit` validation and malformed-scan skipping match the
  per-workspace route; on an empty store the response is HTTP 200 with empty
  `buckets`/`workspaces` and a zeroed `summary`.
- Both routes are bounded per page (`bucket` is `day`/`week` only and `limit`
  buckets are clamped to `1..50`, default `10`) and UTC-only. The optional
  inclusive `from`/`to` window and the `cursor` pagination described below apply
  to both.

### Date-range filtering (trend endpoints)

All four read-only trend endpoints — `GET /api/workspaces/{ws}/trend`,
`GET /api/workspaces/{ws}/trend/buckets`, `GET /api/trend` and
`GET /api/trend/buckets` — accept optional `from` and `to` query parameters to
answer questions such as "how bad was drift during the 4.2 release window?".
Scans whose `created_at` falls outside the inclusive `[from, to]` UTC window are
excluded from `scans`, `buckets`, `workspaces` and every `summary` field alike,
and `limit` is applied after filtering.

```sh
curl -sS 'localhost:8080/api/workspaces/ws1/trend?from=2026-01-01&to=2026-01-31' \
  | python3 -m json.tool

# open-ended: everything from 2026-01-05 onwards, cross-workspace, bucketed by day
curl -sS 'localhost:8080/api/trend/buckets?bucket=day&from=2026-01-05' \
  | python3 -m json.tool
```

- Each bound is optional and independent: supplying only `from` leaves `to`
  unbounded, supplying only `to` leaves `from` unbounded, and supplying neither
  reproduces the unfiltered response byte-for-byte.
- A plain date (`from=2026-01-05`) is interpreted as `2026-01-05T00:00:00+00:00`.
  A full ISO-8601 timestamp (`from=2026-01-05T12:00:00+00:00` or a `Z` suffix) is
  accepted and keeps its offset; a naive timestamp is treated as UTC. Remember
  to URL-encode a literal `+` as `%2B`.
- The window is inclusive on both ends, so a scan exactly at `from` or `to` is
  kept. A malformed value such as `from=not-a-date` returns HTTP 400 on all four
  endpoints; a blank value (`from=`) is treated as absent.
- A window that excludes every scan is not an error: the endpoint returns HTTP
  200 with empty `scans`/`buckets` and a zeroed `summary`, and `delta_high` is
  `0` whenever fewer than two scans (or buckets) remain.

### Pagination (trend endpoints)

All four read-only trend endpoints — `GET /api/workspaces/{ws}/trend`,
`GET /api/workspaces/{ws}/trend/buckets`, `GET /api/trend` and
`GET /api/trend/buckets` — return a `next_cursor` field and accept an optional
`cursor` query parameter so a documentation lead can page further back to an
older release window once the most recent `limit` scans or buckets are
exhausted. Without a cursor the response is unchanged (the most recent `limit`
items, `limit` clamped to `1..50`, default `10`) apart from the added
`next_cursor`; `from`/`to` filtering and `bucket` selection are applied before
pagination.

Each page is presented **oldest-first**, and following the returned
`next_cursor` walks **backwards** in time. Prepend each new page to reconstruct
the full chronological series. `next_cursor` is `null` on the last page. The
cursor is an opaque, URL-safe string derived from the item's sort key
(`created_at` plus a tiebreaker for scans, the bucket `start` plus key for
buckets) rather than an offset index, so a scan or bucket created between
requests never shifts an older page.

```sh
# page 1: the most recent two scans for ws1 (oldest-first within the page)
curl -sS 'localhost:8080/api/workspaces/ws1/trend?limit=2' | python3 -m json.tool

# page 2: follow the returned next_cursor to the next older two scans
# (URL-encode the cursor when scripting; this example uses a shell variable)
CURSOR=$(curl -sS 'localhost:8080/api/workspaces/ws1/trend?limit=2' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["next_cursor"])')
curl -sS "localhost:8080/api/workspaces/ws1/trend?limit=2&cursor=${CURSOR}" \
  | python3 -m json.tool
```

- A malformed cursor (for example `cursor=%%%%`) returns HTTP 400 on all four
  endpoints.
- A well-formed but unknown or expired cursor returns HTTP 200 with an empty
  `scans`/`buckets` list, a zeroed `summary` and `next_cursor: null`.
- A blank cursor (`cursor=`) is treated as absent.
- Pagination composes with the inclusive `from`/`to` window and `bucket`
  selection: only in-window scans/buckets are paged.

### CSV export (trend endpoints)

The two **time-bucketed** trend endpoints can be downloaded as a CSV attachment
instead of JSON, so a lead can paste the weekly numbers straight into a
spreadsheet or attach them to a release review:

- `GET /api/workspaces/{ws}/trend/buckets.csv` — the per-workspace document,
  one row per bucket.
- `GET /api/trend/buckets.csv` — the cross-workspace roll-up, one row per bucket
  per workspace (a bucket with no workspace breakdown still emits one row with an
  empty `workspace`).

Both are read-only, use the exact same `bucket`, `limit`, `from`/`to` and
`cursor` query parameters as their JSON siblings, and stream
`Content-Type: text/csv; charset=utf-8` with a `Content-Disposition: attachment`
filename.

```sh
curl -sS 'localhost:8080/api/workspaces/ws1/trend/buckets.csv?bucket=week&limit=5'
# key,start,end,scans,total_findings,by_severity_high,by_severity_medium,by_severity_low,open_findings,high_open,suppressed
# 2026-W02,2026-01-05T00:00:00+00:00,2026-01-11T23:59:59+00:00,2,3,2,1,0,3,2,0

curl -sS 'localhost:8080/api/trend/buckets.csv?bucket=day' > trend.csv
```

- Every data row carries `key`, `start`, `end`, `scans`, `total_findings`,
  `by_severity_high`, `by_severity_medium`, `by_severity_low`, `open_findings`,
  `high_open` and `suppressed`; the roll-up adds a trailing `workspace` column.
  The values are the same ones the JSON `buckets` array reports, so the CSV and
  JSON never diverge.
- Rows follow the JSON order exactly: buckets oldest-first, and within the
  roll-up the per-bucket `workspaces` breakdown order (descending `high_open`
  then workspace name).
- Values are RFC 4180 quoted only when they contain `,`, `"` or a newline; a
  plain value (including an empty `workspace`) stays unquoted.
- A malformed `bucket` (for example `bucket=month`), non-integer `limit`
  (`limit=abc`), malformed `from`/`to` (`from=not-a-date`) or malformed `cursor`
  returns HTTP 400 with the same JSON error body as the JSON siblings. A
  well-formed but unknown workspace returns HTTP 200 with a header-only CSV.
- The CSV rendering lives in the dependency-free
  `docs_drift_detector/csv_export.py` (`to_csv(document, kind)`) and reuses the
  `buckets.build_workspace_buckets` / `buckets.build_rollup_buckets` output
  rather than recomputing anything.

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
