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

### Scan request body

```json
{
  "name": "Release 4.2",
  "changelog": "renamed \"Save\" to \"Save changes\"\n\"/settings\" -> \"/account/settings\"",
  "diff": "optional unified diff; quoted strings in + / - lines are compared",
  "labels": [{"old": "Save", "new": "Save changes"}],
  "routes": [{"old": "/settings", "new": "/account/settings"}],
  "pages": [{"path": "help/getting-started.md", "content": "Click Save to store your work."}],
  "screenshots": [
    {"image": "img/dashboard.png", "page": "help/dashboard.md", "baseline_hash": "a1", "current_hash": "b2"}
  ],
  "metrics": {"help/getting-started.md": {"views": 3200, "tickets": 4}}
}
```

Only `pages` is required. Every changed label/route that still appears in a doc
page becomes a finding. Screenshots flagged as changed (explicit `"changed": true`,
differing `baseline_hash`/`current_hash`, or `changed_pixels > 0`) that are
embedded in a page become findings too.

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
