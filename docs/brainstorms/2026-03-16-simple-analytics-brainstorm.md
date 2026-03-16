# Brainstorm: Simple Self-Hosted Analytics

**Date:** 2026-03-16
**Status:** Ready for planning

## What We're Building

A minimal, self-hosted web analytics system for a static site (~10k visitors/month). The primary consumer of the data is Claude, not a human dashboard. The system should be trivially understandable — the kind of thing where reading the source once gives you the complete mental model.

### Components

1. **Tracker snippet** (~20 lines of JS) — embedded on the static site, sends a beacon on each page view
2. **Collector server** (~80 lines of Python) — receives beacons, writes to SQLite
3. **SQLite database** (1 file, 8 columns) — the entire data store

### Data captured per page view

| Column | Type | Source |
|--------|------|--------|
| `timestamp` | INTEGER (unix) | Server clock |
| `path` | TEXT | JS beacon |
| `referrer` | TEXT | `document.referrer` |
| `utm_source` | TEXT | URL query param |
| `utm_medium` | TEXT | URL query param |
| `utm_campaign` | TEXT | URL query param |
| `country` | TEXT (2-letter) | IP → country lookup |
| `session_id` | TEXT (nullable) | First-party cookie (NULL until sessions are enabled) |

### How Claude accesses the data

Claude runs on the same machine as the collector. It reads the SQLite file directly — no network, no API, no SCP. Just `sqlite3 /path/to/analytics.db "SELECT ..."`.

Later: wrap in an MCP tool for convenience (thin local wrapper, not needed on day one).

## Why This Approach

**SQLite over JSONL:** JSONL feels simpler but pushes complexity to query time. "Top pages this week" is one SQL query vs. a Python script. At 10k visitors/month, SQLite's overhead is negligible and the schema self-documents the data model.

**No dashboard UI:** Claude is the analyst. A dashboard is code to maintain that adds no value when Claude can run arbitrary SQL. If a human wants to glance at stats, Claude can generate a one-off HTML chart.

**Local machine + Tailscale Funnel:** The collector runs locally, exposed to the public internet via Tailscale Funnel (automatic HTTPS, no reverse proxy). Claude also runs on this machine, reading the DB directly.

**Python for the collector:** sqlite3 is in the stdlib. No compilation, no build step. The entire server fits in one file.

## Session Model (Future, Designed Now)

The session model is designed to be additive — zero changes to existing code, just one new column and a cookie.

### How it works

1. **JS snippet** generates a random session ID, stores it in a cookie on the **static site's domain** (30-minute sliding expiry, refreshed on each page view)
2. **JS sends the session_id as a field in the beacon payload** — not as a cookie header. This avoids all cross-origin cookie issues since the collector is on a different origin (Tailscale Funnel).
3. **Collector** reads `session_id` from the POST body and stores it in the column
4. **No background jobs** — sessions are implicit. A "session" is all page views sharing the same `session_id`

### Why this is easy to reason about

- No separate sessions table. No open/close lifecycle. No cron to "finalize" sessions.
- Session analysis is just `GROUP BY session_id`:
  ```sql
  SELECT session_id, COUNT(*) as pages,
         MIN(timestamp) as started, MAX(timestamp) as ended
  FROM page_views
  GROUP BY session_id;
  ```
- The 30-minute expiry is handled entirely by the browser cookie. Server has zero session state.

### When to add it

The `session_id` column exists from day one (nullable). Enabling sessions is just a 3-line JS change — add the cookie logic. No migration, no backfill. Old rows simply have `NULL` session_id.

## Deliberately Not Tracking

Browser, OS, screen size, device type, user agent. These are tempting to add "for free" but each one expands the schema and analysis surface. Start without them — add only if a real question requires the data.

## Key Decisions

- **SQLite** for storage, not JSONL — structured queries outweigh raw simplicity
- **No dashboard** — Claude queries SQL directly
- **First-party cookie** for sessions — simple sliding-window approach, no server-side session state
- **Country from IP** — DB-IP Lite (~5MB, free), updated monthly via a cron or script
- **2-month retention** — a cron job or the collector itself prunes rows older than 60 days
- **Single-file collector** — the entire backend is one script + one SQLite file
- **Tailscale Funnel** for public HTTPS — no reverse proxy, no cert management
- **systemd** for process management — auto-restart on crash, starts on boot
- **Basic bot filtering** — check User-Agent for known bots server-side
- **Claude runs locally** — reads the SQLite file directly on disk, no network needed
- **`navigator.sendBeacon()`** for the tracker — fire-and-forget, survives page unload
- **CORS locked to specific domains** — configured allowlist, not open to any origin
- **Session ID in payload, not cookies** — avoids cross-origin cookie issues entirely

## Open Questions

None — ready for planning.
