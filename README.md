# Archive Web Offline Tool

A local-first Flask app to inspect Wayback snapshots, analyze site structure, generate sitemap/check reports, and build a recoverable offline copy.

## Key Features

- Inspect snapshots with cache-first behavior and optional force refresh from Archive.
- Analyze selected snapshots deeply (WordPress + structure details).
- Run deep one-by-one analysis across many snapshots with pause/resume/stop.
- Build sitemap details and export as JSON/CSV.
- Check downloaded output (matched vs missing) and download missing files.
- Track recent projects and job history in SQLite.
- Show live job progress for all async flows.

## Tech Stack

- Backend: Flask (`app.py`)
- Archive engine: `archiver.py`
- Persistence: SQLite (`db.py`, `archive_cache.sqlite3`)
- UI: `templates/index.html`

## Quick Start

### Windows (PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python run_and_healthcheck.py
```

### Linux / macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python run_and_healthcheck.py
```

Open: `http://127.0.0.1:5000`

## Tests

```bash
python smoke_test.py
python async_smoke_test.py
```

## Data & Output

- DB/cache file: `archive_cache.sqlite3`
- Downloaded files: `output/`
- Runtime logs: `runtime/server.log`

Deleting a project from UI removes DB/cache/history for that domain, but does not remove output files unless you delete them manually.

## Environment Options

- `PORT` (default `5000`)
- `HOST` (default `127.0.0.1`)
- `FLASK_DEBUG` (default `0`)
- `MAX_ACTIVE_JOBS` (default `4`)
- `JOB_RETENTION_SECONDS` (default `3600`)
- `JOB_CLEANUP_INTERVAL_SECONDS` (default `60`)

## Suggested Project Names

If you want to rename before pushing, good options:

1. `WaybackWorkbench` (recommended)
2. `SnapshotForge`
3. `ArchivePilot`
4. `OfflineFromArchive`
5. `WaybackOps`
