#!/usr/bin/env python3
"""Minimal analytics collector: HTTP server + SQLite writer."""

import json
import sqlite3
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

try:
    import maxminddb
except ImportError:
    maxminddb = None

# --- Configuration ---
# TODO: Set this to your static site's origin before deploying.
ALLOWED_ORIGIN = "https://example.com"
DB_PATH = "/var/lib/loop-analytics/analytics.db"
MMDB_PATH = "/var/lib/loop-analytics/dbip-country-lite.mmdb"
MAX_BODY = 4096
RETENTION_DAYS = 60
PRUNE_INTERVAL = 3600

BOT_PATTERNS = ["bot", "crawl", "spider", "slurp", "mediapartners"]

# --- Module-level state ---
conn = None
reader = None
last_prune = 0.0


def init_db():
    global conn
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS page_views (
            timestamp    INTEGER NOT NULL CHECK(timestamp > 0),
            path         TEXT NOT NULL CHECK(path != ''),
            referrer     TEXT,
            utm_source   TEXT,
            utm_medium   TEXT,
            utm_campaign TEXT,
            country      TEXT CHECK(country IS NULL OR length(country) = 2),
            session_id   TEXT
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_page_views_timestamp ON page_views(timestamp)"
    )
    conn.commit()


def init_mmdb():
    global reader
    if maxminddb is None:
        print("WARNING: maxminddb not installed, country lookup disabled")
        return
    try:
        reader = maxminddb.open_database(MMDB_PATH)
    except Exception as e:
        print(f"WARNING: could not open MMDB ({e}), country lookup disabled")


def ip_to_country(ip):
    if reader is None:
        return None
    try:
        result = reader.get(ip)
    except (ValueError, maxminddb.InvalidDatabaseError):
        return None
    if not isinstance(result, dict):
        return None
    country = result.get("country")
    if not isinstance(country, dict):
        return None
    iso_code = country.get("iso_code")
    return iso_code if isinstance(iso_code, str) and len(iso_code) == 2 else None


def is_bot(user_agent):
    ua = user_agent.lower()
    return any(p in ua for p in BOT_PATTERNS)


def maybe_prune():
    global last_prune
    now = time.time()
    if now - last_prune < PRUNE_INTERVAL:
        return
    last_prune = now
    cutoff = int(now) - (RETENTION_DAYS * 86400)
    conn.execute("DELETE FROM page_views WHERE timestamp < ?", (cutoff,))
    conn.commit()


def str_or_none(val):
    return val if isinstance(val, str) and val else None


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        origin = self.headers.get("Origin", "")
        if origin and origin != ALLOWED_ORIGIN:
            self._respond(403)
            return

        if is_bot(self.headers.get("User-Agent", "")):
            self._respond(204, origin)
            return

        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > MAX_BODY:
            self._respond(413 if length > MAX_BODY else 400)
            return

        try:
            data = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._respond(400)
            return

        path = data.get("path")
        if not isinstance(path, str) or not path:
            self._respond(400)
            return

        xff = self.headers.get("X-Forwarded-For", "")
        client_ip = xff.split(",")[-1].strip() if xff else self.client_address[0]
        country = ip_to_country(client_ip)

        conn.execute(
            "INSERT INTO page_views VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                int(time.time()),
                path,
                str_or_none(data.get("referrer")),
                str_or_none(data.get("utm_source")),
                str_or_none(data.get("utm_medium")),
                str_or_none(data.get("utm_campaign")),
                country,
                str_or_none(data.get("session_id")),
            ),
        )
        conn.commit()
        maybe_prune()
        self._respond(204, origin)

    def _respond(self, code, origin=""):
        self.send_response(code)
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.end_headers()


def main():
    init_db()
    init_mmdb()
    maybe_prune()
    server = HTTPServer(("127.0.0.1", 8080), Handler)
    print("Collector listening on 127.0.0.1:8080")
    try:
        server.serve_forever()
    finally:
        conn.close()
        if reader:
            reader.close()


if __name__ == "__main__":
    main()
