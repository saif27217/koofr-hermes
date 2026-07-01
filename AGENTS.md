# Koofr Hermes — AI Agent Guide

## Project
Lightweight Python media catalog for [Koofr](https://koofr.eu) cloud storage. Runs on Termux or any Linux. Caches Koofr's file tree into SQLite FTS5 for instant search/filter/browse.

## Tech Stack
- Python 3.11+, Flask, sqlite3 (stdlib), requests
- **No** Docker, No .NET, No FUSE, No systemd

## Key Files
| File | Purpose |
|------|---------|
| `server.py` | Single-file app — Koofr API client, SQLite cache, REST API, Web UI |
| `templates/index.html` | Web frontend (embedded in Flask) |
| `deploy-termux.sh` | Idempotent deploy to Termux |
| `.env.example` | Config template |
| `requirements.txt` | Python deps |

## Adding Features
- **New filter type**: add to `VALID_TYPES` in `server.py` and update the JS filter buttons in `index.html`
- **Custom metadata**: extend the SQLite schema in `init_db()`, add a migration in `Meta`
- **Multiple mounts**: the API already returns all mounts; the UI currently shows the primary

## Common Operations
```bash
# Run dev
uv run python server.py --dev

# Refresh catalog
curl http://localhost:5000/api/refresh

# Search API
curl 'http://localhost:5000/api/search?q=inception&type=video'

# Browse folder
curl 'http://localhost:5000/api/browse?path=/Movies'
```

## Pitfalls
- Koofr API rate limits: the tree fetch is a single call, so it won't hit limits. If you add per-file lookups, batch them and add delays.
- App password must be generated at `https://app.koofr.net/app/admin/preferences/password`
- No transcode support — this is a catalog, not a media server
