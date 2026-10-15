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
- **Operational endpoints.** `GET /healthz` plus a stateless JSON API.

## Differentiation

The closest portfolio product, *Feedback Triage Copilot*, bundles incoming user
feedback. Docs Drift Detector starts from the shipped software change and derives
documentation work from it: no customer feedback, no ticket pipeline, just a
release-to-docs comparison with text and screenshot matching.

## Next direction

- Optional outbound webhook / pull-request suggestion for the documentation repo.
- Screenshot diffing with per-region highlighting instead of change signals.
- Pluggable connectors for GitHub commit ranges and rendered docs sites.
- Per-workspace repositories and doc-page quotas for the tiered SaaS plan.

## Non-goals and known limits

- Screenshot comparison is signal-based (hash/pixel-count/flag) rather than a
  full visual diff engine, so cosmetic layout changes must be filtered upstream.
- Matching is lexical; heavily paraphrased documentation must still be reviewed
  by a human.
- The tool recommends work; it does not autonomously edit documentation.

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
