---
title: "feat: Simple Self-Hosted Analytics"
type: feat
status: active
date: 2026-03-16
origin: docs/brainstorms/2026-03-16-simple-analytics-brainstorm.md
---

# Simple Self-Hosted Analytics

## Overview

A minimal, self-hosted web analytics system for a static site (~10k visitors/month). Three components: a JS tracker snippet, a Python collector server, and a SQLite database. Claude is the primary data consumer — no dashboard UI. The entire system should be trivially understandable by reading the source once.

(see brainstorm: docs/brainstorms/2026-03-16-simple-analytics-brainstorm.md)

## Problem Statement / Motivation

Google Analytics is overkill and opaque. Existing self-hosted alternatives (Plausible, Umami) are full applications with dashboards, databases, and deployment complexity. For a 10k visitors/month static site where Claude is the analyst, we need something that is:

- **Tiny** — ~100 lines of code total across JS + Python
- **Transparent** — Claude can read the entire source and reason about the data
- **Zero-ops** — systemd + Tailscale Funnel, no Docker, no Nginx, no cert management

## Proposed Solution

### Architecture

```
Static Site                    Local Machine
+-----------+     beacon      +------------------+     direct read
| JS snippet| -------------> | Python collector | <-------------- Claude
| (~20 LOC) |   sendBeacon   | (~80 LOC)        |   sqlite3 CLI
+-----------+   text/plain   +--------+---------+
                    |                  |
            Tailscale Funnel    +------+------+
            (auto HTTPS)        | SQLite DB   |
                                | (1 file)    |
                                +-------------+
```

### Components

| Component | File | Lines (approx) |
|-----------|------|-----------------|
| JS tracker | `tracker.js` | ~20 |
| Python collector | `collector.py` | ~80 |
| systemd unit | `loop-analytics.service` | ~15 |
| DB schema | Created by collector on first run | 1 table, 8 columns, 1 index |

### SQLite Schema

```sql
CREATE TABLE IF NOT EXISTS page_views (
    timestamp    INTEGER NOT NULL CHECK(timestamp > 0),
    path         TEXT NOT NULL CHECK(path != ''),
    referrer     TEXT,
    utm_source   TEXT,
    utm_medium   TEXT,
    utm_campaign TEXT,
    country      TEXT CHECK(country IS NULL OR length(country) = 2),
    session_id   TEXT
);

CREATE INDEX IF NOT EXISTS idx_page_views_timestamp ON page_views(timestamp);
```

**Data conventions:**
- Absent values are **NULL**, never empty string
- `referrer` is set to NULL client-side if same-origin (self-referral filtering)
- `path` is normalized: trailing slash stripped unless path is `/`
- `timestamp` is always UTC unix epoch
- CHECK constraints catch bugs at INSERT time (zero runtime cost)

## Technical Considerations

### sendBeacon and CORS

The JS snippet sends `JSON.stringify(payload)` as a plain string via `navigator.sendBeacon()`. This results in `Content-Type: text/plain`, which is a CORS-safelisted type — **no preflight OPTIONS request**.

**Critical: CORS headers don't protect sendBeacon.** The browser sends the POST regardless of CORS response headers — sendBeacon is fire-and-forget. The actual defense is **server-side Origin header validation:**

```python
origin = self.headers.get("Origin", "")
if origin and origin != ALLOWED_ORIGIN:
    self._respond(403)
    return
```

The collector also returns `Access-Control-Allow-Origin` on responses (to suppress browser console errors) but this is cosmetic, not protective.

### Python Structure

Module-level globals for shared state. For an 80-line single-threaded script, this is the simplest and most readable approach:

```python
ALLOWED_ORIGIN = "https://your-site.com"
DB_PATH = "/var/lib/loop-analytics/analytics.db"
MMDB_PATH = "/var/lib/loop-analytics/dbip-country-lite.mmdb"

conn = sqlite3.connect(DB_PATH)
reader = None  # set in main() if MMDB exists
last_prune = 0.0
```

Shutdown via `try/finally` around `serve_forever()`:

```python
server = HTTPServer(("127.0.0.1", 8080), Handler)
try:
    server.serve_forever()
finally:
    conn.close()
    if reader:
        reader.close()
```

### SQLite Concurrency (collector writes, Claude reads)

```python
conn.execute("PRAGMA journal_mode=WAL")       # readers never block writers
conn.execute("PRAGMA busy_timeout=5000")       # retry for 5s on lock
conn.execute("PRAGMA synchronous=NORMAL")      # safe with WAL, faster
```

WAL mode persists in the DB file. `busy_timeout` must be set per connection. Claude should also set it: `sqlite3 analytics.db "PRAGMA busy_timeout=2000; SELECT ..."`.

### Client IP via Tailscale Funnel

Tailscale Funnel sets `X-Forwarded-For` with the real client IP. **Must verify at implementation time** — send a test request through Funnel and inspect headers.

**Take the rightmost IP** from `X-Forwarded-For` (the one Funnel appended, not a client-spoofed prefix):

```python
def get_client_ip(self) -> str:
    xff = self.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[-1].strip()
    return self.client_address[0]
```

### Geo-IP with DB-IP Lite

Single external dependency: `maxminddb` (pure Python, reads MMDB files). DB-IP Lite country MMDB is ~24MB, free (CC BY 4.0). Best free option for 2026 (no account required, unlike MaxMind GeoLite2).

Defensive lookup — type-check every intermediate value, catch malformed IPs:

```python
def ip_to_country(reader: maxminddb.Reader | None, ip: str) -> str | None:
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
```

**Graceful degradation:** If MMDB file is missing at startup, log a warning and leave `reader = None`. All rows get `country = NULL`. Don't crash.

**Monthly update:** Download new MMDB file, restart the collector.

### Bot Filtering

Simple denylist — only matches bots that execute JS (the only ones that can trigger sendBeacon). Non-JS tools (wget, curl) can't fire the beacon; Origin validation handles direct POST abuse.

```python
BOT_PATTERNS = ["bot", "crawl", "spider", "slurp", "mediapartners"]

def is_bot(user_agent):
    ua = user_agent.lower()
    return any(p in ua for p in BOT_PATTERNS)
```

Drop the request (204, no insert) if bot detected.

### Request Safety

- **Check `Content-Length` before reading body.** Reject > 4KB with 413. Read exactly `Content-Length` bytes (never `rfile.read()` without a length — blocks forever).
- **JSON parse failure:** Return 400
- **Missing `path` field or non-string fields:** Return 400
- **Parameterized queries only**

### Data Retention

Prune on startup, then hourly inline:

```python
def maybe_prune():
    global last_prune
    now = time.time()
    if now - last_prune < 3600:
        return
    last_prune = now
    cutoff = int(now) - (60 * 86400)
    conn.execute("DELETE FROM page_views WHERE timestamp < ?", (cutoff,))
    conn.commit()
```

At 60-day retention with 10k visitors/month, max ~21k rows. DELETE with index takes <100ms. No batching needed.

### Tailscale Funnel

```bash
sudo tailscale funnel --bg 8080  # one-time, persists across reboots
```

- Collector binds to **`127.0.0.1:8080`** (not 0.0.0.0 — prevents bypassing Funnel on LAN)
- Funnel terminates HTTPS, forwards to localhost
- Public URL: `https://<machine>.<tailnet>.ts.net`
- No custom domain support (Tailscale limitation)

### Accepted Data Loss

We explicitly accept losing data when:
- `navigator.sendBeacon()` returns `false` (rare)
- Collector is restarting (< 1 second)
- Visitor has JS disabled
- Ad blocker intercepts the beacon
- Tailscale Funnel is down

## Acceptance Criteria

### Phase 1: Collector Server (`collector.py`)

- [x] Single Python file, stdlib `http.server` + `sqlite3`, one external dep (`maxminddb`)
- [x] Module-level globals for conn, reader, last_prune
- [x] Binds to `127.0.0.1:8080`
- [x] Creates `page_views` table with CHECK constraints on first run
- [x] Enables WAL mode, busy_timeout, synchronous=NORMAL at startup
- [x] Checks Content-Length before reading, rejects > 4KB
- [x] Parses JSON from text/plain body, validates `path` present and fields are strings
- [x] Server-side Origin validation (403 if mismatched)
- [x] Looks up country from X-Forwarded-For (rightmost IP) via MMDB (NULL if fails)
- [x] Filters bots by simple UA denylist (5 patterns)
- [x] Returns 204 with CORS header on success
- [x] Prunes on startup and hourly
- [x] `try/finally` closes connection and MMDB reader on exit

### Phase 2: JS Tracker (`tracker.js`)

- [x] ~20 lines, no dependencies, no build step
- [x] Collects: `pathname` (trailing slash stripped), `referrer` (NULL if same-origin), UTM params
- [x] Sends via `navigator.sendBeacon(url, JSON.stringify(data))` — text/plain, no preflight
- [x] No cookies (sessions are future work)
- [x] Fires on `DOMContentLoaded`

### Phase 3: Deployment

- [ ] Create system user: `useradd --system --no-create-home --shell /usr/sbin/nologin loop-analytics`
- [ ] Create `/var/lib/loop-analytics/` (owned by loop-analytics, 750)
- [x] systemd unit: `User=loop-analytics`, `Restart=always`, `RestartSec=5`, `After=network.target tailscaled.service`, `Environment="PYTHONUNBUFFERED=1"`
- [ ] Tailscale Funnel: `tailscale funnel --bg 8080`
- [ ] MMDB file at `/var/lib/loop-analytics/dbip-country-lite.mmdb`
- [x] `.gitignore` (exclude `*.db`, `*.mmdb`, `__pycache__/`)

### Phase 4: Verification

- [ ] Send test beacon through Funnel → row appears in SQLite
- [ ] Country lookup works (X-Forwarded-For verified)
- [ ] Wrong Origin → 403
- [ ] Bot UA → 204, no row inserted
- [ ] Malformed payload → 400, no row inserted
- [ ] Claude can query: `sqlite3 /var/lib/loop-analytics/analytics.db "PRAGMA busy_timeout=2000; SELECT COUNT(*) FROM page_views;"`

## Session Model (Designed, Not Implemented)

The `session_id` column exists from day one (nullable). To enable sessions later:

1. JS generates a random ID via `crypto.randomUUID()`
2. JS stores it in a cookie on the **static site's domain** (`_s=<id>`, 30-min sliding expiry, `SameSite=Lax; Secure; Path=/`)
3. JS includes `session_id` in the beacon **payload** (not as a cookie header — different origins)
4. Collector reads it from the POST body

**Note:** Enabling sessions may require cookie consent under GDPR/ePrivacy. Evaluate before enabling.

## Dependencies & Risks

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Tailscale Funnel X-Forwarded-For behavior differs from expected | Low | Test during Phase 4; degrade to NULL country |
| MMDB file becomes stale | Medium | Country data changes slowly; stale is still useful |
| SQLite corruption | Very low | WAL + single writer; acceptable to start fresh |
| Bot traffic inflates numbers | Medium | UA denylist + query-time filtering by Claude |
| Cross-origin beacon abuse | Low | Server-side Origin validation |

## File Tree

```
loop/                           # Git repo
  collector.py                  # Python HTTP server + SQLite writer
  tracker.js                    # JS snippet for static site
  loop-analytics.service        # systemd unit file
  .gitignore
  docs/
    brainstorms/...
    plans/...
```

Runtime files (not in repo):
```
/var/lib/loop-analytics/
  analytics.db                  # SQLite database
  dbip-country-lite.mmdb        # Geo-IP database
```

## What We Deliberately Left Out

These were all considered during planning (some by 11 parallel review agents) and rejected. Don't add them back without revisiting the reasoning.

| Idea | Why rejected |
|------|-------------|
| **Dashboard UI** | Claude is the analyst. A dashboard is code to maintain that adds no value when Claude can run arbitrary SQL. |
| **Rate limiting** | Behind Tailscale Funnel with ~350 req/day. The threat is theoretical. Origin validation handles direct POST abuse. |
| **`_metadata` table** | Claude can compute `COUNT(*)`, `MIN/MAX(timestamp)` in 1ms. A metadata table adds write logic for marginal convenience. |
| **SQL views** (`daily_summary`, `page_views_readable`) | Claude generates SQL effortlessly. Pre-made views save typing `datetime(timestamp, 'unixepoch')` but Claude does that without thinking. |
| **Query cookbook file** | Claude generates better queries than a static file. By the second session it will have outgrown the cookbook. |
| **`analytics-context.md`** (accumulated knowledge) | Claude's own memory system handles this. Adding a separate file is a workflow that may never be needed. |
| **Factory pattern** (`CollectorState` dataclass + `make_handler()`) | Better engineering, but for an 80-line single-threaded script, module globals are simpler and more immediately readable. |
| **Compiled regex for bot filtering** | Extended patterns (wget, curl, headless chrome, etc.) catch things that can't happen — these tools don't execute JS, so they can't trigger `sendBeacon`. The 5-pattern list catches the only bots that matter: those that render JS. |
| **`DELETE ... LIMIT` batching** | Max ~21k rows at 60-day retention. DELETE with an index takes <100ms. The LIMIT protects against a scenario that can't happen at this scale. |
| **Overriding `log_message`** | The collector binds to 127.0.0.1. All connections show as 127.0.0.1. Default logging doesn't touch `X-Forwarded-For`. No privacy leak. |
| **Explicit 404/405 routing** | Non-beacon requests fail JSON parsing and return 400 naturally. Explicit routing is tidier but adds ~4 lines for no practical benefit. |
| **`PRAGMA integrity_check`** | If corruption is detected, the plan says "start fresh." You'd discover corruption on the first failed query anyway. |
| **`PRAGMA auto_vacuum = INCREMENTAL`** | At 50-100MB, disk usage doesn't matter. Optimizes for a non-problem. |
| **systemd hardening** (`ProtectSystem`, `ProtectHome`, `NoNewPrivileges`) | Good practice for production services, but overkill for a page view counter on a personal machine. |
| **Field length limits** | The 4KB body size limit already caps all field lengths. Adding per-field limits is defense-in-depth against a threat the body limit already handles. |
| **Browser/OS/device tracking** | Tempting to add "for free" but each column expands the schema and analysis surface. Add only if a real question requires the data. |
| **`ThreadingHTTPServer`** | At 0.004 req/s, request overlap is essentially impossible. Threading adds thread-safety concerns for SQLite writes with zero benefit. |

## Growth Threshold

Split collector.py into modules if it exceeds ~200 LOC.

## Sources & References

- **Origin brainstorm:** [docs/brainstorms/2026-03-16-simple-analytics-brainstorm.md](docs/brainstorms/2026-03-16-simple-analytics-brainstorm.md)
- Python `http.server`: https://docs.python.org/3/library/http.server.html
- SQLite WAL: https://sqlite.org/wal.html
- sendBeacon: https://developer.mozilla.org/en-US/docs/Web/API/Navigator/sendBeacon
- sendBeacon CORS (text/plain avoids preflight): https://requestmetrics.com/building/using-the-beacon-api
- DB-IP Lite: https://db-ip.com/db/download/ip-to-country-lite
- maxminddb: https://pypi.org/project/maxminddb/
- Tailscale Funnel: https://tailscale.com/kb/1223/funnel
- GoatCounter design philosophy: https://www.goatcounter.com/design
