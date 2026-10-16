# Docs Drift Detector

## Goal

Docs Drift Detector helps documentation and product teams keep help pages in
sync with the software they describe. After every release, UI labels, routes and
screenshots change. Documentation silently rots until support tickets appear.
This product turns a release changelog or commit range into a prioritised,
checkable list of the documentation sections that most likely need an update.

It is intentionally a *release-to-docs* tool. It is not a feedback inbox and it
does not require customer tickets to work; it derives documentation work directly
from what shipped.

## Intended users

- Product and documentation teams in B2B SaaS companies (20–200 employees) that
  maintain a public help/ docs site and ship at least monthly.
- Small doc teams that cannot afford a manual review marathon before every
  release.

## Problem

After each release, screenshots, step-by-step guides and UI terminology in the
help centre age out. Nobody systematically checks which documentation sections a
code or UI change affects, so problems surface only through support volume or
abandoning self-service users.

## Value proposition

Link release changes (UI labels, screenshots, routes) to the affected doc
sections and produce a prioritised, tickable update list per release instead of a
manual review marathon.

## Current capabilities (implemented in this repository)

- **Release trigger import.** Accepts a unified diff, a changelog, or explicit
  label/route change lists as the release trigger.
- **Heuristic change extraction.** Detects renamed/added UI labels and changed
  routes from quoted strings in diff hunks and from changelog phrasing such as
  `renamed "Old" to "New"` or `"/old" -> "/new"`.
- **Documentation matching.** Matches changed labels and routes against supplied
  doc pages and reports the exact line/context that is now stale.
- **Screenshot drift.** Flags changed screenshots (explicit flag, hash mismatch or
  changed pixel count) that are embedded in a doc page.
- **Prioritisation.** Scores every finding with a transparent formula combining
  change type, page views and support-ticket volume, then labels it high/medium/
  low so the most impactful work is first.
- **Check-off overview.** A web UI lists findings per scan with a checkbox to mark
  each drift resolved; state persists.
- **Markdown export.** Exports the finding list as a checkbox Markdown list ready
  to commit into the documentation repository.
- **Persistent scans.** Scans are stored in a JSON file on a mounted volume; no
  external database is required.
- **Ignore rules.** A scan can declare rules that suppress known-benign findings
  before scoring. Each rule optionally pins a change `kind` (`label`, `route`,
  `screenshot` or `*`), a `page` glob and a case-insensitive `value` substring
  matched against the finding's old value (or screenshot image path). Rules are
  evaluated from the request body; the number of suppressed findings is reported
  in the summary. Malformed rules are dropped silently.
- **Per-workspace rule library.** Ignore rules can be persisted once per
  workspace and reused across scans (e.g. a label that is intentionally quoted in
  the docs). A scan records the optional `workspace` it belongs to; stored
  library rules and any per-scan `rules` are merged, with per-scan rules taking
  precedence, and the combined suppressions are reported in the summary. The
  library is managed through `GET`/`PUT`/`DELETE /api/workspaces/{ws}/rules` and
  is stored alongside scans in the same JSON file.
- **Portable rule-library import/export.** A workspace library can be downloaded
  as a versioned JSON document (`GET /api/workspaces/{ws}/rules/export`) and
  loaded back with a single request (`POST /api/workspaces/{ws}/rules/import`),
  so a curated rule set can move from staging to production or between
  workspaces. Import replaces the stored library and reports how many rules were
  imported; malformed documents are rejected with HTTP 400.
- **Versioned rule-library history and promotion.** Every library mutation
  (`PUT`/`import`/`clear`) appends a revision to a bounded, append-only history
  (up to `MAX_RULE_HISTORY = 20` revisions per workspace, oldest dropped) stored
  with the scans in the same JSON file. `GET /api/workspaces/{ws}/rules/history`
  lists revisions newest-first with `revision`, `created_at`, `action`
  (`set`/`import`/`clear`/`promote`), `rule_count` and the normalised `rules`;
  `POST /api/workspaces/{ws}/rules/history/{revision}/promote` restores that
  revision as the live library (recording a new `promote` revision) so a bad
  import can be undone and a curated staging rule set promoted to production in
  one call.
- **Rule provenance in the suppressed summary.** Every normalised rule carries a
  stable `id` (a short hash of `kind|page|value`) and an `origin`
  (`request` or `workspace`). The scan summary keeps the integer
  `stats.suppressed` total for backwards compatibility and adds
  `stats.suppressed_rules`, a per-rule breakdown of `{id, origin, kind, page,
  value, count}` sorted by descending count, so teams can audit *why* findings
  were suppressed and which rule did it. Each scan also records the optional
  `workspace` it belongs to.
- **Per-rule suppression drill-down.** A stored scan persists the exact findings
  each rule suppressed, under `suppressed_details`, and exposes them through
  `GET /api/scans/{id}/suppressed` as `{scan, suppressed, rules}`. Every rule
  entry adds a `count` and a `findings` list of `{id, kind, page, old_value,
  new_value, score, severity}` sorted by descending score then page then id, so a
  documentation lead can see *what* a rule hid, not only *how many* items it hid.
  The web UI expands these findings inline beneath the provenance table.
- **Per-workspace outbound webhook.** A webhook URL can be registered once per
  workspace (`GET`/`PUT`/`DELETE /api/workspaces/{ws}/webhook`; empty URL
  clears). After every stored scan for that workspace succeeds, the service
  POSTs a JSON payload describing the scan and its **high-severity findings
  only** (sorted by descending score) to the configured URL via
  `notify.build_payload` and `notify.dispatch`. Delivery is best-effort: a
  single plain `urllib.request` POST with `content-type: application/json`,
  no retry queue and no HMAC signing, so a missing or failing endpoint is
  logged to stderr and never blocks or changes the scan response. The config
  is stored in the same JSON file under `workspace_webhooks` and reloading
  tolerates a missing or malformed key.
- **Pull-request suggestion document.** A stored scan can be turned into a
  dependency-free, JSON-serialisable PR suggestion through
  `GET /api/scans/{id}/pull-request` (with optional `base` and `prefix` query
  parameters). The document carries a deterministic `branch`
  (`docs-drift/<scan_id>` by default), `base_branch`, a `title`, a Markdown
  `body`, a `files` list of `{path, action: "update", reason}` derived from the
  distinct pages of the scan's high-severity findings (sorted by path), and a
  `checklist` reusing `export.to_markdown`. The high-severity scope and ordering
  come from `notify.build_payload`, so PR suggestions and webhooks stay
  consistent. A scan with no high-severity findings still returns a document
  with an empty `files` list and a body stating that no action is required.
- **GitHub pull-request connector.** A workspace can store a GitHub repository
  config (`GET`/`PUT`/`DELETE /api/workspaces/{ws}/github`) carrying a `repo`
  (`owner/name`), a `base_branch` (default `main`) and a `token_env` naming the
  environment variable that holds the token. `POST /api/scans/{id}/pull-request/open`
  turns a stored scan into a real branch and pull request through the GitHub REST
  API (standard-library `urllib.request` only): it resolves the base branch,
  creates the ref, commits the exported drift checklist as
  `docs-drift/<scan_id>.md` and opens the pull request, returning the connector
  result `{ok, status, error, pr_url, branch}`. The token is read from the
  environment and never stored. Opening is also attempted automatically, as a
  best-effort side effect, after every stored scan for a configured workspace.
  An unknown scan returns 404, an invalid id, missing `repo` or unset token
  returns 400, and an upstream failure returns 200 with `ok: false`.
- **Scheduled drift scans with digest notifications.** A workspace can register
  a recurring drift check (`GET`/`PUT`/`DELETE /api/workspaces/{ws}/schedule`)
  carrying an interval in hours (`1..168`), an optional stored `scan` payload
  (the same shape as `POST /api/scans`) and an `enabled` flag. A background
  scheduler started by `python -m docs_drift_detector` (daemon thread, tick
  controlled by `DDR_SCHEDULER_TICK`, default 60s; disable with
  `DDR_SCHEDULER_ENABLED=0`) runs each enabled schedule whose `next_run_at` is
  due through the ordinary scan path, stores it like a manual scan, advances
  `next_run_at` by `interval_hours` and POSTs a **digest** payload to the
  workspace webhook. The digest (`notify.build_digest`) summarises the stored
  scan: counts by severity, the high-severity findings that are new since the
  previous scheduled scan, and a link to the scan. Operators can inspect the
  scheduler through `GET /api/schedule/status` (`enabled`, `tick_seconds`,
  `due`, `schedules`). A schedule that fires with no changes still produces a
  stored scan with zero new high-severity findings and never fails.
- **Per-workspace scan retention.** A workspace can cap how many stored scans it
  keeps (`GET`/`PUT`/`DELETE /api/workspaces/{ws}/retention`, with
  `max_scans` in `0..10000`; `0` clears the policy and an unset workspace
  reports `0`). After every stored scan the oldest scans of that workspace
  beyond the cap are pruned automatically (`store.prune_workspace_scans`),
  leaving scans of other workspaces and scans without a workspace untouched.
  Pruning returns the number of deleted scans and is idempotent. It is
  best-effort and never changes the scan response; a failure is logged to
  stderr. The policy is stored under `workspace_retention` in the same JSON
  file and reloading tolerates a missing or malformed key.
- **Operational endpoints.** `GET /healthz` plus a stateless JSON API.

## Differentiation

The closest portfolio product, *Feedback Triage Copilot*, bundles incoming user
feedback. Docs Drift Detector starts from the shipped software change and derives
documentation work from it: no customer feedback, no ticket pipeline, just a
release-to-docs comparison with text and screenshot matching.

## Next direction

- The outbound-notification and pull-request items are now shipped: the
  configurable per-workspace webhook notifies a Slack/Teams/CI endpoint about
  high-severity findings once per stored scan, the **pull-request suggestion**
  remains available as a dependency-free document
  (`GET /api/scans/{id}/pull-request`) for a thin CI wrapper, and the **GitHub
  connector** now opens that branch/PR autonomously, pushing the exported
  checklist with the same high-severity scope. The **per-workspace scan
  retention** policy now caps unbounded store growth by pruning the oldest scans
  after each stored scan. What remains is broader connector coverage and making
  repositories first-class per-workspace objects.
- Screenshot diffing with per-region highlighting instead of change signals.
- Pluggable connectors for GitHub commit ranges and rendered docs sites.
- Per-workspace repositories and doc-page quotas for the tiered SaaS plan.
- Richer scheduling: cron expressions, per-workspace time zones and
  multi-process/durable job execution instead of the single in-process
  scheduler thread shipped now.

## Non-goals and known limits

- Screenshot comparison is signal-based (hash/pixel-count/flag) rather than a
  full visual diff engine, so cosmetic layout changes must be filtered upstream.
- Matching is lexical; heavily paraphrased documentation must still be reviewed
  by a human.
- Webhook delivery is best-effort with no retry queue, so a scan is never
  delayed or failed by a webhook. Delivery is plain HTTP POST with no HMAC
  signing yet, so receivers should treat the endpoint as untrusted.
- The scheduler is single-process and in-memory: it runs only inside the HTTP
  process that started it, there is no cron expression support (intervals are
  whole hours only) and no durable job queue, so a process restart skips any
  tick that was missed while it was down. Digest delivery is the same
  best-effort webhook POST as ordinary notifications.
- The GitHub connector is best-effort and single-PR: it commits the drift
  checklist report rather than editing the documentation prose, never
  force-pushes (if the branch already exists the call fails instead of moving
  it), and reads the token from a named environment variable rather than storing
  it. Automatic opening is skipped when a workspace has no configured repository,
  and any failure is only logged to stderr.
- The tool recommends work; it does not autonomously edit documentation.
- Scan retention prunes whole scans oldest-first and is keyed by workspace only:
  scans without a workspace are never pruned, there is no time-based expiry, and
  the remaining scans still live in the single JSON store rather than a database.

## Revenue hypothesis

SaaS subscription per documentation workspace, tiered by connected repositories
and checked doc pages (e.g. 49–199 EUR/month). Upsell through automatic pull
request suggestions for documentation repositories.

## Risks

- False positives from purely cosmetic code changes could erode trust in reports;
  scoring and check-off state mitigate this.
- Documentation in mixed formats and languages makes matching noisy.
- Screenshot comparison is fragile against layout, font and responsive changes.
- Value depends on the customer's release discipline.

---

# Appendix: Factory Product Criteria

The following criteria are the infrastructure's product-quality gates. The
implementation above is measured against them.

## Hard Requirements (MUST)

- **Web Deployable:** Runs as a web application served over HTTP on port 8080.
- **Originality:** A release-to-docs drift detector, not a copy of an existing
  product.
- **Expandable:** Pure-Python package split into analysis, storage, export and
  HTTP layers; new change extractors and connectors plug in.
- **Real User Value:** Saves documentation teams a manual pre-release review.
- **Clearly Defined Audience:** B2B SaaS documentation and product teams.
- **Standalone:** No dependency on another AI Product Factory application.
- **Autonomous Operation:** Runs unattended; scans are created through the API.
- **Kubernetes Compatible:** Ships a Helm chart under `deploy/helm/`.
- **Resource Efficient:** Standard-library Python image with small requests and
  limits.
- **Security:** Non-root container, input validation, size limits, path-traversal
  guard on static files.
- **Testable:** `tests/` contains automated unit and HTTP tests.
- **Maintainable:** Readable package layout and documentation.
- **Legal Compliance:** Standard-library-only code, no copied assets.
- **Health Monitoring:** `GET /healthz`.
- **Unique Product Identity:** Repository, product and chart named
  `docs-drift-detector`.

## Soft Requirements (SHOULD)

- **Database Independence:** Uses a JSON file, no database.
- **Monetization Potential:** Tiered workspace subscription (see above).
- **Low Operating Costs:** No external APIs or paid services.
- **Fast MVP Development:** Single-language, dependency-free implementation.
- **Low Maintenance:** Deterministic, dependency-free runtime.
- **Market Differentiation:** Release-change-driven, screenshot-aware.
- **Growth Potential:** Stateless HTTP layer scales horizontally.
- **User Retention:** Persistent backlog and check-off state bring teams back.
- **Discoverability:** Stable JSON API and Markdown export.
- **Mobile Friendly:** Responsive layout in the web UI.
- **Accessibility:** Semantic HTML, labelled controls, live status text.
- **Minimal Dependencies:** Python standard library only.
- **Measurable Success:** Per-scan stats (findings, open, high severity).
- **Reusability:** Generic analysis functions independent of the HTTP layer.
- **Innovation Potential:** Connectors and visual diffing are natural extensions.
