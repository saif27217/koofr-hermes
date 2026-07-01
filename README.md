# Koofr Hermes

**Lightweight media catalog for Koofr cloud storage.**

Drop-in query solution for your Koofr drive — search, browse, and filter all your files by type (video, image, audio, document, archive). Runs on **Termux** or any Linux in ~50MB RAM.

## Features

- **Instant search** — full-text (FTS5) on file names and paths, returns results as you type
- **Browse by folder** — directory tree with breadcrumbs
- **Filter by type** — video, image, audio, document, archive presets
- **Cached catalog** — fetches Koofr's file tree once, stores in SQLite; automatic background refresh
- **Self-contained** — single Python file, Flask web UI, no Docker, no .NET, no FUSE mount
- **Safe by default** — binds to 127.0.0.1 (local only); access via SSH tunnel

## Quick start

### Termux

```bash
git clone https://github.com/saif27217/koofr-hermes.git
cd koofr-hermes
bash deploy-termux.sh
```

Follow the prompts to enter your Koofr email and app password.

### Any Linux (including VPS)

```bash
# Prerequisites: Python 3.11+, uv/pip
git clone https://github.com/saif27217/koofr-hermes.git
cd koofr-hermes

# Set up
uv venv
uv pip install -r requirements.txt

# Configure — generate app password at
# https://app.koofr.net/app/admin/preferences/password
export KOOFR_EMAIL="you@example.com"
export KOOFR_PASSWORD="your-app-password"

# Run
uv run python server.py
```

Open [http://localhost:5000](http://localhost:5000).

### Access via SSH tunnel (from VPS or desktop)

```bash
ssh -L 5000:localhost:5000 termux  # or your VPS
```

## API

All endpoints return JSON `{"ok": true, ...}`.

| Endpoint | Params | Description |
|----------|--------|-------------|
| `GET /api/status` | — | Server status, file count, last refresh |
| `GET /api/browse` | `path` | List directory contents |
| `GET /api/search` | `q`, `type`, `page`, `per_page` | Full-text search with type filter |
| `POST /api/refresh` | — | Force-refresh catalog from Koofr |
| `GET /api/file` | `path` | Single file metadata |
| `GET /api/stats` | — | Media type breakdown |

Valid `type` values: `all`, `video`, `image`, `audio`, `document`, `archive`.

## Config

Set via environment variables or `.env` file:

| Variable | Default | Description |
|----------|---------|-------------|
| `KOOFR_EMAIL` | — | Koofr account email (required) |
| `KOOFR_PASSWORD` | — | Koofr app password (required) |
| `KOOFR_API_BASE` | `https://app.koofr.net` | API base URL |
| `KOOFR_PORT` | `5000` | Web server port |
| `KOOFR_HOST` | `127.0.0.1` | Bind address |
| `KOOFR_DB` | `~/.koofr-hermes/cache.db` | SQLite cache path |
| `KOOFR_REFRESH_INTERVAL` | `3600` | Catalog refresh interval (seconds) |

## Architecture

```
Termux/Server
  ├── server.py          ← Single Flask app
  │   ├── KoofrClient    — REST v2 API (Basic Auth)
  │   ├── Database       — SQLite FTS5 cache
  │   └── Flask routes   — REST API + Web UI
  └── templates/
      └── index.html     — Single-page web frontend (vanilla JS)
```

No filesystem mount needed. The Koofr `/files/tree` endpoint is fetched once, flattened into SQLite, and queried entirely from cache. Background thread refreshes every `REFRESH_INTERVAL` seconds.

## License

MIT
