#!/usr/bin/env python3
"""Koofr Hermes — lightweight media catalog for Koofr cloud storage.

Caches Koofr's file tree into SQLite FTS5 for instant search/filter/browse.
Single file, ~50MB RAM, runs on Termux or any Linux.

Usage:
  export KOOFR_EMAIL=you@example.com
  export KOOFR_PASSWORD="<app-password-from-koofr-settings>"
  uv run server.py
"""

from __future__ import annotations

import os
import re
import sys
import json
import time
import math
import fnmatch
import sqlite3
import hashlib
import pathlib
import threading
from dotenv import load_dotenv

load_dotenv()  # Load .env file if present

import traceback

import requests
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import quote

from flask import (Flask, Response, g, jsonify, request, render_template,
                   send_from_directory, stream_with_context)

# ── Constants ────────────────────────────────────────────────────────────────

APP_NAME = "Koofr Hermes"
VERSION = "0.1.0"

DEFAULT_PORT = 5000
DEFAULT_HOST = "127.0.0.1"          # safe: only localhost (behind SSH tunnel)
DEFAULT_REFRESH_INTERVAL = 3600      # 1 hour
DEFAULT_API_BASE = "https://app.koofr.net"

# Content-type prefixes we recognise for filtering
MEDIA_TYPES: dict[str, list[str]] = {
    "video":   ["video/"],
    "image":   ["image/"],
    "audio":   ["audio/"],
    "document": ["application/pdf", "application/msword",
                 "application/vnd.openxmlformats-officedocument",
                 "text/plain", "text/csv"],
    "archive": ["application/zip", "application/x-7z", "application/x-rar",
                "application/gzip", "application/x-tar"],
}

VALID_TYPES = ("all",) + tuple(MEDIA_TYPES.keys())

# ── Config ────────────────────────────────────────────────────────────────────

class Config:
    def __init__(self):
        self.email = os.environ.get("KOOFR_EMAIL", "")
        self.password = os.environ.get("KOOFR_PASSWORD", "")
        self.api_base = os.environ.get("KOOFR_API_BASE", DEFAULT_API_BASE)
        self.port = int(os.environ.get("KOOFR_PORT", str(DEFAULT_PORT)))
        self.host = os.environ.get("KOOFR_HOST", DEFAULT_HOST)
        self.db_path = os.path.expanduser(
            os.environ.get("KOOFR_DB", "~/.koofr-hermes/cache.db"))
        self.refresh_interval = int(
            os.environ.get("KOOFR_REFRESH_INTERVAL",
                           str(DEFAULT_REFRESH_INTERVAL)))
        self.dev = "--dev" in sys.argv

    @property
    def configured(self) -> bool:
        return bool(self.email) and bool(self.password)

    def validate(self):
        if not self.email:
            sys.exit("ERROR: KOOFR_EMAIL not set")
        if not self.password:
            sys.exit("ERROR: KOOFR_PASSWORD not set (generate an app password "
                     "at https://app.koofr.net/app/admin/preferences/password)")


config = Config()

# ── Koofr API Client ──────────────────────────────────────────────────────────

class KoofrClient:
    """Koofr REST v2.1 API client (Basic Auth with app password).

    Auth: HTTP Basic Auth with email + app-specific password.
    Base URL: https://app.koofr.net
    API version: 2.1 (paths under /api/v2.1/ and /content/api/v2.1/)
    """

    def __init__(self, base_url: str, email: str, password: str):
        self.base = base_url.rstrip("/")
        self.api_prefix = "/api/v2.1"
        self.content_prefix = "/content/api/v2.1"
        self.session = requests.Session()
        self.session.auth = (email, password)
        self.session.headers["User-Agent"] = f"{APP_NAME}/{VERSION}"
        self._mount_id: str | None = None
        self._mount_name: str = ""

    def _get(self, path: str, **kwargs) -> dict:
        max_429_retries = 20  # ~1 hour total with backoff
        resp = self.session.get(f"{self.base}{path}", **kwargs)

        # Handle 429 rate limit with backoff
        if resp.status_code == 429:
            for attempt in range(max_429_retries):
                wait = 60 * (attempt + 1)
                print(f"[{APP_NAME}] Koofr rate limited (too many login retries). "
                      f"Waiting {wait}s (attempt {attempt+1}/{max_429_retries})...")
                print(f"[{APP_NAME}] Tip: visiting https://app.koofr.net in a browser "
                      f"may reset the login retry counter.")
                time.sleep(wait)
                resp = self.session.get(f"{self.base}{path}", **kwargs)
                if resp.status_code != 429:
                    break
            else:
                sys.exit("ERROR: Koofr rate limit persists after retries.\n"
                         "  → Log into https://app.koofr.net in your browser to reset.\n"
                         "  → Or wait ~1 hour and try again.")

        if resp.status_code == 401:
            sys.exit("ERROR: Koofr auth failed — check your email and app password\n"
                     "  KOOFR_EMAIL and KOOFR_PASSWORD in .env must match an app password\n"
                     "  Generate one at: https://app.koofr.net/app/admin/preferences/password")

        resp.raise_for_status()
        ct = resp.headers.get("Content-Type", "")
        if "ndjson" in ct:
            # NDJSON: one JSON object per line, accumulate into a dict
            # that has a "files" key with the list of all items
            files: list[dict] = []
            for line in resp.iter_lines(decode_unicode=True):
                line = line.strip()
                if line:
                    obj = json.loads(line)
                    entry = obj.get("file", obj)
                    entry["path"] = obj.get("path", "")
                    files.append(entry)
            return {"files": files}
        return resp.json()

    def _content_get(self, path: str, **kwargs) -> bytes:
        """Fetch file content (raw bytes)."""
        resp = self.session.get(f"{self.base}{path}", **kwargs)
        resp.raise_for_status()
        return resp.content

    @property
    def mount_id(self) -> str:
        if self._mount_id:
            return self._mount_id
        data = self._get(f"{self.api_prefix}/mounts")
        for m in data.get("mounts", []):
            if m.get("isPrimary"):
                self._mount_id = m["id"]
                return self._mount_id
        if data.get("mounts"):
            self._mount_id = data["mounts"][0]["id"]
            return self._mount_id
        raise RuntimeError("No mounts found in Koofr account")

    def get_file_list(self, mount_id: str, path: str = "/") -> list[dict]:
        """List files in a directory (non-recursive)."""
        data = self._get(
            f"{self.api_prefix}/mounts/{mount_id}/files/list",
            params={"path": path},
        )
        return data.get("files", [])

    def get_file_list_recursive(self, mount_id: str, path: str = "/") -> list[dict]:
        """Fetch ALL files recursively from Koofr in a single API call."""
        data = self._get(
            f"{self.content_prefix}/mounts/{mount_id}/files/listrecursive",
            params={"path": path},
        )
        return data.get("files", [])

    def get_file_info(self, mount_id: str, path: str) -> dict:
        """Get file/folder metadata."""
        return self._get(
            f"{self.api_prefix}/mounts/{mount_id}/files/info",
            params={"path": path},
        )

    def check_connection(self) -> str:
        """Verify credentials work and return the mount name."""
        mid = self.mount_id
        mount = self._get(f"{self.api_prefix}/mounts/{mid}")
        return mount.get("name", mid)


# ── Database ──────────────────────────────────────────────────────────────────

DB_INIT_SQL = """
CREATE TABLE IF NOT EXISTS files (
    path        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    type        TEXT NOT NULL,        -- 'file' | 'dir'
    content_type TEXT DEFAULT '',
    size        INTEGER DEFAULT 0,
    modified    INTEGER DEFAULT 0,
    hash        TEXT DEFAULT '',
    parent_path TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_files_parent ON files(parent_path);
CREATE INDEX IF NOT EXISTS ix_files_type ON files(type);
CREATE INDEX IF NOT EXISTS ix_files_content_type ON files(content_type);

CREATE VIRTUAL TABLE IF NOT EXISTS files_fts
USING fts5(name, path, content='files', content_rowid='rowid', tokenize='unicode61');

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class Database:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def init(self):
        """Create tables if they don't exist."""
        self.conn.executescript(DB_INIT_SQL)
        self.conn.commit()

    def get_meta(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?",
                                (key,)).fetchone()
        if row and row["value"] is not None:
            return str(row["value"])
        return default

    def set_meta(self, key: str, value: str):
        self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                          (key, value))
        self.conn.commit()

    def clear_files(self):
        self.conn.execute("DELETE FROM files")
        self.conn.execute("INSERT INTO files_fts(files_fts) VALUES('rebuild')")
        self.conn.commit()

    def upsert_file(self, path: str, name: str, ftype: str,
                    content_type: str = "", size: int = 0,
                    modified: int = 0, hash_val: str = "",
                    parent_path: str = ""):
        self.conn.execute("""INSERT OR REPLACE INTO files
            (path, name, type, content_type, size, modified, hash, parent_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (path, name, ftype, content_type, size, modified, hash_val, parent_path))

    def count_files(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) as c FROM files WHERE type='file'").fetchone()
        return row["c"] if row else 0

    def count_dirs(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) as c FROM files WHERE type='dir'").fetchone()
        return row["c"] if row else 0

    # ── Queries ───────────────────────────────────────────────────────────

    def search(self, query: str, media_type: str = "all",
               offset: int = 0, limit: int = 50) -> tuple[list[dict], int]:
        """Full-text search via FTS5. Returns (results, total_count)."""
        clauses: list[str] = ["files.type = 'file'"]
        params: list = []

        if media_type != "all":
            prefixes = MEDIA_TYPES.get(media_type, [])
            if prefixes:
                or_clauses = " OR ".join(
                    f"files.content_type LIKE ?" for _ in prefixes)
                clauses.append(f"({or_clauses})")
                params.extend(f"{p}%" for p in prefixes)

        if query:
            # FTS5 search
            q = sanitize_fts5(query)
            fts_clause = "files_ts.name MATCH ? OR files_ts.path MATCH ?"
            clauses.append(fts_clause)
            params.extend([q, q])

        where = " AND ".join(clauses)

        count_sql = f"""SELECT COUNT(*) FROM files_fts files_ts
            JOIN files ON files.rowid = files_ts.rowid
            WHERE {where}"""

        total = self.conn.execute(count_sql, params).fetchone()[0]

        data_sql = f"""SELECT files.* FROM files_fts files_ts
            JOIN files ON files.rowid = files_ts.rowid
            WHERE {where}
            ORDER BY rank
            LIMIT ? OFFSET ?"""

        rows = self.conn.execute(data_sql, params + [limit, offset]).fetchall()

        return ([dict(r) for r in rows], total)

    def browse(self, parent_path: str, include_files: bool = True,
               sort_by: str = "name") -> list[dict]:
        """List items in a directory."""
        rows = self.conn.execute(
            "SELECT * FROM files WHERE parent_path = ? ORDER BY "
            "CASE WHEN type='dir' THEN 0 ELSE 1 END, name",
            (parent_path,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_file(self, path: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM files WHERE path=?",
                                (path,)).fetchone()
        return dict(row) if row else None

    def get_directory_tree(self, prefix: str = "/") -> list[str]:
        """Get all unique directory paths (for breadcrumbs)."""
        rows = self.conn.execute(
            "SELECT DISTINCT path FROM files WHERE type='dir' "
            "AND path LIKE ? ORDER BY path",
            (f"{prefix}%",)
        ).fetchall()
        return [r["path"] for r in rows]

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None


def sanitize_fts5(query: str) -> str:
    """Escape special FTS5 characters and split into AND terms."""
    # FTS5 special chars: ^ * " ( ) : + -
    # We just escape or remove them for safety
    q = re.sub(r'[^a-zA-Z0-9_\s.\-]', ' ', query)
    q = re.sub(r'\s+', ' ', q).strip()
    if not q:
        return ""
    # Prefix search with * for partial matching
    terms = q.split()
    return " AND ".join(f'"{t}"*' if len(t) > 2 else f'"{t}"' for t in terms)


# ── Catalog Refresh ──────────────────────────────────────────────────────────

def refresh_catalog(client: KoofrClient, db: Database) -> dict:
    """Fetch the full tree from Koofr and rebuild the SQLite cache."""
    start = time.time()
    print(f"[{APP_NAME}] Fetching file list from Koofr...")
    files = client.get_file_list_recursive(client.mount_id, "/")

    print(f"[{APP_NAME}] Processing {len(files)} items...")
    db.clear_files()
    db.conn.execute("BEGIN")
    for item in files:
        path_val = item.get("path", "")
        name = item.get("name", "")
        ftype = item.get("type", "file")
        # Parse parent path from full path
        parent_path = path_val.rsplit("/", 1)[0] if "/" in path_val else "/"
        if parent_path == "":
            parent_path = "/"

        db.upsert_file(
            path=path_val,
            name=name,
            ftype=ftype,
            content_type=item.get("contentType", ""),
            size=item.get("size", 0),
            modified=item.get("modified", 0),
            hash_val=item.get("hash", ""),
            parent_path=parent_path,
        )
    db.conn.commit()

    elapsed = time.time() - start
    db.set_meta("last_refresh", str(int(start)))
    db.set_meta("file_count", str(db.count_files()))
    db.set_meta("dir_count", str(db.count_dirs()))

    return {
        "ok": True,
        "items_written": len(files),
        "files": db.count_files(),
        "dirs": db.count_dirs(),
        "elapsed_s": round(elapsed, 2),
    }


# ── Flask App ────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = config.dev

# Make db accessible from routes
def get_db() -> Database:
    if "db" not in g:
        g.db = Database(config.db_path)
        g.db.init()
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db:
        db.close()


def api_error(msg: str, status: int = 400):
    return jsonify({"ok": False, "error": msg}), status


# ── API Routes ──

@app.route("/api/status")
def api_status():
    db = get_db()
    last_refresh = db.get_meta("last_refresh", "0")
    last_refresh_dt = (datetime.fromtimestamp(int(last_refresh), tz=timezone.utc)
                       .isoformat() if last_refresh != "0" else None)
    return jsonify({
        "ok": True,
        "app": APP_NAME,
        "version": VERSION,
        "configured": config.configured,
        "mount_name": getattr(client, "_mount_name", ""),
        "files": db.get_meta("file_count", "0"),
        "dirs": db.get_meta("dir_count", "0"),
        "last_refresh": last_refresh_dt,
    })


@app.route("/api/search")
def api_search():
    db = get_db()
    q = request.args.get("q", "").strip()
    media_type = request.args.get("type", "all")
    page = int(request.args.get("page", "1"))
    per_page = min(int(request.args.get("per_page", "50")), 200)

    if media_type not in VALID_TYPES:
        return api_error(f"Invalid type. Valid: {', '.join(VALID_TYPES)}")

    if not q:
        return api_error("Query param 'q' is required")

    offset = (page - 1) * per_page
    results, total = db.search(q, media_type, offset, per_page)

    return jsonify({
        "ok": True,
        "results": results,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": math.ceil(total / per_page) if total else 0,
    })


@app.route("/api/browse")
def api_browse():
    db = get_db()
    path = request.args.get("path", "/")
    # Normalise
    path = path or "/"
    if not path.startswith("/"):
        path = "/" + path

    items = db.browse(path)
    current = db.get_file(path)

    subdirs = [i for i in items if i["type"] == "dir"]
    files_list = [i for i in items if i["type"] == "file"]

    return jsonify({
        "ok": True,
        "path": path,
        "current": current,
        "subdirs": subdirs,
        "files": files_list,
    })


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    if not config.configured:
        return api_error("Koofr credentials not configured", 503)
    try:
        result = refresh_catalog(_get_client(), get_db())
        return jsonify(result)
    except Exception as e:
        return api_error(str(e), 500)


@app.route("/api/file")
def api_file():
    db = get_db()
    path = request.args.get("path", "")
    if not path:
        return api_error("Path param required")
    info = db.get_file(path)
    if not info:
        return api_error("File not found", 404)
    return jsonify({"ok": True, "file": info})


@app.route("/api/file/content")
def api_file_content():
    """Proxy file content from Koofr, supporting Range requests for video seeking."""
    path = request.args.get("path", "")
    if not path:
        return api_error("Path param required")

    c = _get_client()
    mount_id = c.mount_id

    # Look up file in catalog for Content-Type
    db = get_db()
    info = db.get_file(path)
    if not info:
        return api_error("File not found", 404)

    content_type = info.get("content_type") or "application/octet-stream"
    filename = info.get("name", "file")

    # Build Koofr content URL and forward Range header
    url = f"{c.base}{c.content_prefix}/mounts/{mount_id}/files/get"
    params = {"path": path}
    headers = {}
    range_hdr = request.headers.get("Range")
    if range_hdr:
        headers["Range"] = range_hdr

    try:
        upstream = c.session.get(url, params=params, headers=headers, stream=True, timeout=300)
        upstream.raise_for_status()
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else 502
        return api_error(f"Upstream error: {e}", status)

    # Build streaming Flask response
    def generate():
        for chunk in upstream.iter_content(chunk_size=65536):
            if chunk:
                yield chunk

    resp = Response(generate(), status=upstream.status_code)
    resp.headers["Content-Type"] = content_type
    resp.headers["Content-Disposition"] = f'inline; filename="{quote(filename)}"'
    resp.headers["Accept-Ranges"] = "bytes"
    resp.headers["Cache-Control"] = "private, max-age=3600"

    # Forward Content-Range + Content-Length from Koofr (for video seeking)
    for h in ("Content-Range", "Content-Length"):
        val = upstream.headers.get(h)
        if val:
            resp.headers[h] = val

    return resp


@app.route("/api/stats")
def api_stats():
    """Media type breakdown."""
    db = get_db()
    total = db.count_files()
    breakdown = {}
    for tname, prefixes in MEDIA_TYPES.items():
        or_clauses = " OR ".join("content_type LIKE ?" for _ in prefixes)
        params = [f"{p}%" for p in prefixes]
        row = db.conn.execute(
            f"SELECT COUNT(*) FROM files WHERE type='file' AND ({or_clauses})",
            params
        ).fetchone()
        breakdown[tname] = row[0]
    other = total - sum(breakdown.values())
    if other > 0:
        breakdown["other"] = other
    return jsonify({"ok": True, "total": total, "by_type": breakdown})


# ── Web UI ──

@app.route("/")
def index():
    return render_template("index.html")


# ── Main ─────────────────────────────────────────────────────────────────────

client: KoofrClient | None = None


def _get_client() -> KoofrClient:
    """Helper to assert client is configured."""
    c = client
    if c is None:
        raise RuntimeError("Koofr client not configured")
    return c


def background_refresh():
    """Periodic catalog refresh in background."""
    while True:
        time.sleep(config.refresh_interval)
        try:
            if config.configured and client:
                print(f"[{APP_NAME}] Background refresh...")
                refresh_catalog(client, get_db())
        except Exception as e:
            print(f"[{APP_NAME}] Background refresh failed: {e}")


def main():
    global client

    if not config.configured:
        print(f"WARNING: {APP_NAME} not configured. Set KOOFR_EMAIL and "
              "KOOFR_PASSWORD environment variables.",
              file=sys.stderr)

    if config.configured:
        config.validate()
        client = KoofrClient(config.api_base, config.email, config.password)

        # Test connection
        try:
            mount_name = client.check_connection()
            client._mount_name = mount_name
            print(f"[{APP_NAME}] Connected to mount: {mount_name}")
        except Exception as e:
            print(f"[{APP_NAME}] Connection failed: {e}", file=sys.stderr)
            if not config.dev:
                sys.exit(1)

        # Initial catalog refresh if stale or empty
        db = Database(config.db_path)
        db.init()
        last_refresh = db.get_meta("last_refresh", "0")
        now = int(time.time())
        stale = (now - int(last_refresh)) > config.refresh_interval if last_refresh != "0" else True
        file_count = db.count_files()

        if stale or file_count == 0:
            try:
                result = refresh_catalog(client, db)
                print(f"[{APP_NAME}] Catalog: {result['files']} files, "
                      f"{result['dirs']} dirs in {result['elapsed_s']}s")
            except Exception as e:
                print(f"[{APP_NAME}] Initial refresh failed: {e}", file=sys.stderr)
        else:
            print(f"[{APP_NAME}] Using cached catalog ({file_count} files, "
                  f"last refresh: {datetime.fromtimestamp(int(last_refresh)).isoformat()})")
        db.close()

        # Start background refresh thread
        t = threading.Thread(target=background_refresh, daemon=True)
        t.start()

    # Start web server
    print(f"[{APP_NAME}] Serving on http://{config.host}:{config.port}")
    print(f"[{APP_NAME}] Open web UI: http://localhost:{config.port}")
    app.run(host=config.host, port=config.port, debug=config.dev)


if __name__ == "__main__":
    main()
