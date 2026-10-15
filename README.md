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
- `GET  /api/scans/{id}/export.md` — Markdown checklist export.
- `POST /api/scans/{id}/findings/{fid}/resolve` — set `{"resolved": true|false}`.
- `DELETE /api/scans/{id}` — delete a scan.
- `GET  /api/workspaces/{ws}/rules` — read the stored rule library (`[]` when unset).
- `PUT  /api/workspaces/{ws}/rules` — replace the library with `{"rules": [...]}`.
- `DELETE /api/workspaces/{ws}/rules` — clear the library.

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

### Workspace rule libraries

`workspace` is an optional name (`[A-Za-z0-9_-]{1,64}`) recorded on a scan.
Rules can be persisted once per workspace and reused across scans instead of
being pasted into every request. When a scan/create or analyze request carries a
`workspace`, the stored library rules are merged with the request `rules`: the
per-scan `rules` take precedence and **both** sets are applied, so the combined
suppressions are reflected in `stats.suppressed`. Requests without a `workspace`
behave exactly as before.

```sh
curl -sS -X PUT localhost:8080/api/workspaces/ws1/rules \
  -H 'content-type: application/json' \
  --data '{"rules":[{"kind":"label","value":"Save"}]}' | python3 -m json.tool
```

Invalid workspace ids (for example `bad id!`) return HTTP 400 on all three
workspace endpoints. The same name restriction applies when a scan or analyze
request carries a `workspace`.

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
