# Wayback Offline Builder

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
.\install.ps1
.\run.bat
```

### Linux / macOS

```bash
chmod +x install.sh run.sh uninstall.sh
./install.sh
./run.sh
```

Open: `http://127.0.0.1:5000`

Installer prompts now let you choose:
- DB preference (`sqlite` or `mysql` config values)
- SQLite DB file location
- Output folder location

Note: app runtime currently uses SQLite. If `mysql` is chosen, installer saves mysql values in `.env` for future backend support, and the app continues with SQLite fallback.

## Tests

```bash
python smoke_test.py
python async_smoke_test.py
```

## Data & Output

- DB/cache file: `archive_cache.sqlite3`
- Downloaded files: `output/`
- Runtime logs: `runtime/server.log`

Deleting a project from UI removes DB/cache/history for that domain and can optionally delete local output files.

## Uninstall

Windows:

```powershell
.\uninstall.ps1
```

Linux / macOS:

```bash
./uninstall.sh
```

## Environment Options

- `PORT` (default `5000`)
- `HOST` (default `127.0.0.1`)
- `FLASK_DEBUG` (default `0`)
- `MAX_ACTIVE_JOBS` (default `4`)
- `JOB_RETENTION_SECONDS` (default `3600`)
- `JOB_CLEANUP_INTERVAL_SECONDS` (default `60`)
- `OUTPUT_ROOT_DIR` (default `./output`)
- `ALLOW_UNSAFE_OUTPUT_ROOT` (default `0`, restricts output to `OUTPUT_ROOT_DIR`)
- `DB_PRUNE_INTERVAL_SECONDS` (default `600`)
- `DB_CACHE_RETENTION_SECONDS` (default `1209600` / 14 days)
- `DB_JOBS_RETENTION_SECONDS` (default `2592000` / 30 days)

## Release Checklist

- Run: `python smoke_test.py`
- Run: `python async_smoke_test.py`
- Verify setup scripts: `install.ps1`, `install.sh`, `run.bat`, `run.sh`
- Start app and confirm health: `python run_and_healthcheck.py --check-only`
- Ensure `archive_cache.sqlite3` is backed up before major upgrades
