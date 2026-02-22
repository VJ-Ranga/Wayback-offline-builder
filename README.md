# Wayback Offline Builder

A local-first Flask app to inspect Wayback snapshots, analyze site structure, generate sitemap/check reports, and build a recoverable offline copy.

## Project Status

This project is actively under development and evolving quickly.

- It is currently a functional beta and intended as a practical, learning-driven tool.
- APIs, workflows, and configuration behavior may still change between updates.
- Feedback, bug reports, and suggestions are welcome and appreciated.

If you use it in production-like environments, please review settings carefully and test with your own data and limits first.

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
chmod +x install.sh run.sh update.sh uninstall.sh
./install.sh
./run.sh
```

Open: `http://127.0.0.1:5000`

Installer prompts now let you choose:
- DB preference (`sqlite` or `mysql` config values)
- SQLite DB file location
- Output folder location

If `mysql` is selected, installer creates the database (if missing), and app startup auto-creates required tables.

## Tests

```bash
python smoke_test.py
python async_smoke_test.py
```

## Data & Output

- DB/cache file: `archive_cache.sqlite3`
- Downloaded files: `output/`
- Runtime logs: `runtime/server.log`

Local runtime files (`.env`, sqlite DB, output, runtime) are ignored by git and are not published.

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

## Update (Git)

Use this when you want the latest tool changes from the repo.

Windows:

```powershell
.\update.ps1
```

Linux / macOS:

```bash
./update.sh
```

What update scripts do:
- ensure working tree is clean before pulling
- backup SQLite DB to `runtime/db-backups/`
- `git pull --ff-only`
- update Python dependencies in `.venv`

## Environment Options

- `PORT` (default `5000`)
- `HOST` (default `127.0.0.1`)
- `FLASK_DEBUG` (default `0`)
- `DB_BACKEND` (`sqlite` default, `mysql` config can be saved)
- `SQLITE_DB_PATH` (default `./archive_cache.sqlite3`)
- `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_DATABASE`, `MYSQL_USER`, `MYSQL_PASSWORD` (saved config)
- `MAX_ACTIVE_JOBS` (default `4`)
- `JOB_RETENTION_SECONDS` (default `3600`)
- `JOB_CLEANUP_INTERVAL_SECONDS` (default `60`)
- `OUTPUT_ROOT_DIR` (default `./output`)
- `ALLOW_UNSAFE_OUTPUT_ROOT` (default `0`, restricts output to `OUTPUT_ROOT_DIR`)
- `WAYBACK_MIN_REQUEST_INTERVAL_MS` (default `250`, minimum delay between Archive.org requests)
- `CDX_CACHE_MAX_ITEMS` (default `5000`, max in-memory timestamp cache entries)
- `DB_PRUNE_INTERVAL_SECONDS` (default `600`)
- `DB_CACHE_RETENTION_SECONDS` (default `1209600` / 14 days)
- `DB_JOBS_RETENTION_SECONDS` (default `2592000` / 30 days)

WARNING: Setting `ALLOW_UNSAFE_OUTPUT_ROOT=1` disables output root safety boundary checks and may allow writes outside your configured project output area. Keep it `0` unless you fully trust all inputs and runtime context.

## Release Checklist

- Run: `python smoke_test.py`
- Run: `python async_smoke_test.py`
- Verify setup scripts: `install.ps1`, `install.sh`, `run.bat`, `run.sh`
- Start app and confirm health: `python run_and_healthcheck.py --check-only`
- Ensure `archive_cache.sqlite3` is backed up before major upgrades

## License

MIT. See `LICENSE`.
