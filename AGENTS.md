# Koofr Hermes — AI Agent Guide

## Project
Lightweight Python media catalog for [Koofr](https://koofr.eu) cloud storage. Runs on Termux or any Linux. Caches Koofr's file tree into SQLite FTS5 for instant search/filter/browse.

## Tech Stack
- Python 3.11+, Flask, sqlite3 (stdlib), requests
- **No** Docker, No .NET, No FUSE, No systemd

## Key Files
| File | Purpose |
|------|---------|
| `server.py` | Single-file app — Koofr API client, SQLite cache, Flask REST API + Web UI |
| `templates/index.html` | Web frontend (embedded in Flask via `render_template`) |
| `deploy-termux.sh` | Idempotent deploy to Termux (Android) |
| `run.sh` | Source `.env` and start with gunicorn or Flask |
| `.env.example` | Config template (copy to `.env` and fill in) |
| `requirements.txt` | Python deps |
| `AGENTS.md` | This file — setup and integration knowledge |

## Authentication

Koofr supports **two** auth methods:

### App Passwords + Basic Auth (what we use)
- User generates an app password at `https://app.koofr.net/app/admin/preferences/password`
- Email + app password are combined as `email:password` and base64-encoded
- Sent as `Authorization: Basic <base64>` header on every request
- Configured in `.env` as `KOOFR_EMAIL` and `KOOFR_PASSWORD`
- Works for both REST API (`/api/v2.1/`) and WebDAV (`/dav/`)

### OAuth 2 (not implemented here)
- Requires creating an app at `https://app.koofr.net/developers/apps`
- OAuth2 auth code flow with client_id, redirect_uri, refresh tokens
- More complex but doesn't trigger Basic Auth rate limits

## Koofr API v2.1 Reference

### Base URLs
```
REST API:  https://app.koofr.net/api/v2.1/
Content:   https://app.koofr.net/content/api/v2.1/
Public:    https://app.koofr.net/api/v2.1/basicconfig  (no auth needed)
```

### Endpoints Used

#### Get mounts: `GET /api/v2.1/mounts`
Returns all mounted storage locations (Koofr native + linked Google Drive, Dropbox, etc.).
Primary mount is identified by `isPrimary: true`.

#### Recursive file list: `GET /content/api/v2.1/mounts/{mountId}/files/listrecursive?path=/`
**Returns NDJSON** (Newline-Delimited JSON), not regular JSON.
- Content-Type: `application/x-ndjson; charset=utf-8`
- Each line is a separate JSON object: `{"type":"file"|"dir","path":"/full/path","file":{"name":"","type":"dir","modified":...,"size":...,"contentType":"...","hash":"...","tags":{}}}`
- Lines are ordered depth-first (parents before children)
- Root entry: empty name `""`, path `"/"`, type `"dir"`
- Directory entries have no `hash` or `contentType` fields
- Can be very large (observed: 57 MB for 185K items)

#### List directory: `GET /content/api/v2.1/mounts/{mountId}/files/list?path=/some/folder`
Returns regular JSON (not NDJSON). Non-recursive, single directory level.

#### File info: `GET /api/v2.1/mounts/{mountId}/files/info?path=/some/file.ext`
Returns file metadata as JSON.

#### File content: `GET /content/api/v2.1/mounts/{mountId}/files/get?path=/some/file.ext`
Returns raw file bytes.

### NDJSON Parsing (critical)
The `listrecursive` endpoint returns one JSON object per line. Parse with:
```python
for line in resp.iter_lines(decode_unicode=True):
    if line.strip():
        obj = json.loads(line)
        entry = obj.get("file", obj)
        entry["path"] = obj.get("path", "")
        files.append(entry)
```
Also works by splitting on newlines and parsing each, but `iter_lines()` is memory-efficient for large responses.

## Rate Limiting

### "Too many login retries" (HTTP 429)
- **IP-based** — the rate limit is per IP address, not per account
- Triggered by sending invalid credentials (HTTP 401) repeatedly to the same IP
- Affects ALL auth methods (REST API + WebDAV) from that IP
- The rate-limited IP still gets a TCP connection; the API returns `HTTP 429` with body `Too many login retries`

### How to reset
1. **Wait** — auto-clears after 15–60 minutes of no failed attempts
2. **Change IP** — requests from a different IP (e.g., Termux vs. VPS) work immediately
3. **Log into web UI** — visiting `https://app.koofr.net` from the blocked IP and logging in with the correct main password resets the counter

### Prevention
- Use the correct API version from the start (`/api/v2.1/`, not `/api/v2/`)
- Reuse a single `requests.Session()` — don't re-authenticate per request
- Add exponential backoff on 429 (implemented in `KoofrClient._get()` with 60s-interval retries up to 20 attempts)
- Don't hammer the API with debugging requests — each 401 counts toward the limit

## Mount Structure

A Koofr account can have multiple mounts:
- **Koofr native** — primary storage (`isPrimary: true`)
- **Linked cloud drives** — Google Drive, Dropbox, OneDrive (shown as separate mounts with `origin: "googledrive"` etc.)

The server currently catalogs only the **primary mount** (Koofr native).

Observed mount properties:
```json
{
  "id": "uuid-string",
  "name": "Koofr",
  "type": "device",
  "origin": "hosted",              // "hosted" | "googledrive" | "dropbox" | ...
  "online": true,
  "isPrimary": true,
  "spaceTotal": 1058816,
  "spaceUsed": 920683,
  "capabilities": {
    "rawThumbnails": true,
    "externalLinks": false,
    "officeOnline": true,
    "tags": true
  },
  "permissions": {
    "READ": true, "WRITE": true, "OWNER": true, ...
  }
}
```

Note: `spaceTotal`/`spaceUsed` on Koofr native doesn't always match actual file content sizes (the mount stores metadata differently from linked drives).

## File Schema (in Catalog)

Each file entry in the catalog has these fields:

| Field | Source | Example |
|-------|--------|---------|
| `name` | `file.name` from NDJSON | `"video.mp4"` |
| `path` | `obj.path` from NDJSON | `"/Movies/video.mp4"` |
| `type` | `file.type` from NDJSON | `"file"` or `"dir"` |
| `size` | `file.size` from NDJSON | `575974888` |
| `content_type` | `file.contentType` from NDJSON | `"video/mp4"` |
| `file_hash` | `file.hash` from NDJSON | `"fb66916c..."` (MD5) |
| `modified` | `file.modified` from NDJSON | Unix timestamp in ms |
| `parent_path` | Derived from `path.rsplit("/", 1)[0]` | `"/Movies"` |

## Setup

### Environment
```bash
cp .env.example .env
# Edit .env:
#   KOOFR_EMAIL=<your-email>
#   KOOFR_PASSWORD=<app-password, not main password>
#   KOOFR_HOST=0.0.0.0              # bind all interfaces (for Tailscale)
#   # KOOFR_HOST=127.0.0.1          # localhost only (default, safer)

### Deploy to Termux
```bash
bash deploy-termux.sh
```

### Run
```bash
bash run.sh
```
Server starts on `http://127.0.0.1:5000`.

### SSH Tunnel (for VPS access)
```bash
# From VPS to Termux (port 5000)
ssh -L 5000:127.0.0.1:5000 -p 8022 sak@<termux-tailscale-ip>
```

## Tailscale Access

The server runs on Termux with Tailscale (`100.x.x.x`). To make it reachable from
other devices on your Tailscale network without SSH tunnels:

### Steps

1. **Bind to all interfaces** by setting `KOOFR_HOST=0.0.0.0` in `.env`:
   ```bash
   echo "KOOFR_HOST=0.0.0.0" >> ~/koofr-hermes/.env
   ```

2. **Restart the server**:
   ```bash
   pkill -f "python server.py"
   bash ~/koofr-hermes/run.sh
   ```

3. **Access from any Tailscale device** at:
   ```
   http://<termux-tailscale-ip>:5000
   ```
   Find the Tailscale IP with `tailscale ip -4` on Termux.

### Why this works
- Tailscale is a private mesh VPN — only devices in your tailnet can reach this IP
- No SSH tunnel, no Cloudflare, no public exposure
- Still behind Tailscale ACLs if you've set them up
- The VPS in the same tailnet reaches it directly at `100.x.x.x:5000`

### Pitfall: `python-dotenv` dependency
The server calls `load_dotenv()` at import time to read `.env`. Without it,
you'd need `source .env` before starting. Install with:
```bash
pip install python-dotenv
```

## Common Operations
```bash
# Run dev (auto-reload)
uv run python server.py --dev

# Refresh catalog
curl http://localhost:5000/api/refresh

# Search API
curl 'http://localhost:5000/api/search?q=inception&type=video'

# Browse folder
curl 'http://localhost:5000/api/browse?path=/Movies'

# Server status
curl http://localhost:5000/api/status

# Health check
curl http://localhost:5000/api/ping
```

## Pitfalls & Lessons Learned

### 1. NDJSON vs JSON
The `listrecursive` endpoint returns `application/x-ndjson`, not `application/json`.
Attempting `resp.json()` raises `JSONDecodeError: Extra data: line 2 column 1`.
**Fix**: check `Content-Type` header and parse line-by-line with `iter_lines()`.

### 2. API version selection
- `/api/v2/mounts` → returns 401 with `WWW-Authenticate: Token` (requires OAuth2 Bearer token)
- `/api/v2.1/mounts` → works with Basic Auth (app passwords)
- Always use v2.1 for app password auth.

### 3. Rate limit persists across auth methods
If the REST API gets rate-limited, WebDAV also returns 429. The rate limit is IP-wide.
Waiting or logging in via web UI is the only fix from that IP.

### 4. Rediscovering the correct email
The Koofr account may be registered under a different email than expected.
If app passwords keep returning 401, verify the email in Koofr account settings.

### 5. `listrecursive` is slow for large accounts
Observed: 57 MB / 185K items / ~100 seconds on a mobile connection (Termux).
The catalog build happens once at startup and caches everything in SQLite.
Background refreshes use the same endpoint.

### 6. Uploading files via the API
The content API (`/content/api/v2.1/mounts/{mountId}/files/put`) supports file uploads.
Not currently used in the catalog server. If implementing batched operations, add delays to avoid triggering rate limits.

### 7. Rate limit on the VPS
Do initial development/testing from the same network as the user (e.g., Termux).
The VPS IP may be blocked from repeated auth attempts during debugging.
`KoofrClient._get()` has built-in 429 retry with 60s exponential backoff (up to 20 attempts).

### 8. App password scopes
App passwords generated at `https://app.koofr.net/app/admin/preferences/password` have all REST API scopes by default. No scope configuration is needed.

### 9. File hashes
Koofr provides MD5 hashes for files (in the `hash` field of NDJSON).
Useful for deduplication and change detection.

## Adding Features

- **New filter type**: add to `VALID_TYPES` in `server.py` and update the JS filter buttons in `index.html`
- **Custom metadata**: extend the SQLite schema in `init_db()`, add a migration in `Meta`
- **Multiple mounts**: the API already returns all mounts; the UI currently shows the primary
- **Thumbnails**: Koofr API supports `rawThumbnails` capability via the content API
- **Search across linked drives**: requires iterating mounts and separate `listrecursive` calls
