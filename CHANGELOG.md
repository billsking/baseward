# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- API tokens accept an optional expiry at creation and expose expiry and last use in the admin
  console. Expired tokens are rejected for MCP authentication, their outstanding short/export
  credentials are rejected, and they cannot issue new derived credentials; `last_used_at` writes
  are coalesced over 60 seconds.
- The compose file and `.env.example` now cover every runtime setting, with a unit test preventing
  future configuration drift.
- `requirements.txt` is generated from `pyproject.toml` by `scripts/sync_requirements.py`; CI checks
  the generated file before running the dependency audit.
- `GATEWAY_AUDIT_REQUIRE_SERVER_BACKEND` refuses to start when the management store (and the
  fail-closed audit log) is SQLite; a prominent warning is logged when SQLite is used.
- Retention cleanup now records its last successful run and performs a startup catch-up when
  overdue, so processes that restart more often than once per day still enforce retention.
- `GATEWAY_MCP_QUERY_TIMEOUT_SECONDS` (default 30) bounds every MCP statement: on expiry the
  upstream query is killed and the connection is discarded.
- `GATEWAY_MCP_WRITE_MAX_AFFECTED_ROWS` (default 1000) rolls back an UPDATE/DELETE that
  affects more rows than the cap and reports `WRITE_LIMIT_EXCEEDED`.
- UPDATE/DELETE whose WHERE clause is always true (`WHERE 1=1`, `WHERE TRUE`) are denied as
  `NO_WHERE`.
- MCP admission control: `GATEWAY_MCP_MAX_CONCURRENT` (default 32) and
  `GATEWAY_MCP_MAX_CONCURRENT_PER_TOKEN` (default 4) cap in-flight tool calls, and
  `GATEWAY_MCP_RATE_LIMIT_PER_MINUTE` (default 120) caps a token's calls per 60-second
  window. Over-limit calls fail fast with `TOO_MANY_CONCURRENT` / `RATE_LIMITED` instead of
  queueing.
- Business datasource pool sizing is configurable via `GATEWAY_DATASOURCE_POOL_SIZE`,
  `GATEWAY_DATASOURCE_POOL_MAX_OVERFLOW` and `GATEWAY_DATASOURCE_POOL_RECYCLE_SECONDS`
  (previously hard-coded to 5/5/3600).
- Every HTTP response carries `X-Request-ID` (inbound ids are validated and reused), and the id
  is attached to log records and reused for the call's audit rows.
- `GATEWAY_LOG_FORMAT=json` emits one JSON object per line for log shippers.
- `/readyz` reports management-store readiness without the export-channel details carried by
  `/healthz`.
- `GATEWAY_METRICS_ENABLED` serves Prometheus metrics at `/metrics`: HTTP count/latency, MCP
  tool outcomes, in-flight calls and datasource pool occupancy. It is off by default.
- Admin sign-in is throttled per username+client IP: `GATEWAY_ADMIN_LOGIN_MAX_ATTEMPTS`
  (default 5) failures inside `GATEWAY_ADMIN_LOGIN_WINDOW_SECONDS` (default 300) lock that
  pair for `GATEWAY_ADMIN_LOGIN_LOCK_SECONDS` (default 900) and return 429. Successful
  sign-ins clear the counter, and every success, failure and locked rejection is audited as
  `admin_login` with the client IP and request id.
- Every HTTP response now carries `X-Content-Type-Options`, `X-Frame-Options`,
  `Referrer-Policy` and a strict `Content-Security-Policy` whose per-request nonce allows the
  admin's single inline script; `Strict-Transport-Security` is added when
  `GATEWAY_COOKIE_SECURE=true`.
- Admin HTML pages are sent with `Cache-Control: no-store`, `Pragma: no-cache` and
  `Cross-Origin-Opener-Policy: same-origin`, and every response carries a restrictive
  `Permissions-Policy`. Admin sessions now idle out after
  `GATEWAY_ADMIN_SESSION_IDLE_MINUTES` (default 30) without activity; browser activity
  silently renews the signed cookie, while the absolute lifetime stays 8 hours. Cookies
  issued before this change lack the activity timestamp and are invalidated once, so
  admins sign in again after upgrading.
- The compose deployment now runs with a read-only root filesystem (only `/data` and `/tmp`
  are writable), `no-new-privileges`, all Linux capabilities dropped, `init`, and a 30-second
  graceful shutdown. The runtime image no longer installs `curl`; its healthcheck uses the
  bundled Python, shrinking the image and its vulnerability surface.

### Security

- `aiomysql` is now required at `>=0.3` (fixes PYSEC-2026-1110, where a malicious MySQL
  server could read arbitrary files from the gateway host) and `cryptography` at `>=50`
  (PKCS#7 oracle, certificate path-building and bundled OpenSSL fixes).
- CI runs `pip-audit --strict -r requirements.txt`, so a known vulnerability in a runtime
  dependency blocks merges instead of waiting for the next manual review.

### Changed

- MCP authentication and admission-control errors and `/api/exchange` errors now use the same
  `{"error": {"code", "message", "request_id"}}` structure as tool errors, reusing the inbound or
  generated `X-Request-ID` instead of returning inconsistent payloads or blank request ids.

### Fixed

- Masking rules now follow column lineage through derived tables, CTEs and `UNION`
  branches, so aliasing a sensitive column (for example
  `SELECT * FROM (SELECT phone AS p FROM users) x`) no longer bypasses masking.
  Mixing a named projection with `*` (`SELECT phone AS p, * FROM users`) is covered
  as well.

## [0.0.1] - 2026-11-14

Initial open-source release.

### Added

- MCP-native database gateway (Streamable HTTP, stateless) with five tools:
  `list_datasources`, `describe_datasource`, `search_objects`, `execute_sql`,
  `issue_export_credential`.
- Four-level token scopes (datasource → database → table → column); `SELECT *` is
  rewritten to the allowed columns.
- AST-based default-deny SQL policy: DDL, `GRANT`, `CALL`, multi-statement, locking
  reads, `INTO OUTFILE` and writes without `WHERE` are blocked.
- Column masking: automatic keyword detection plus manual `mask/hash/null/off` rules.
- Fail-closed audit: intent is recorded before execution and queries are denied when
  the audit store is unavailable.
- MySQL wire-protocol export channel with short-lived, source-IP-bound credentials and
  automatic handoff for oversized MCP results.
- Dual-identity audit (principal + self-asserted actor) for agent platforms, with
  per-token actor policy.
- Short-lived credential exchange and an OpenClaw bridge plugin
  (`integrations/openclaw-baseward`).
- Bilingual (English / Chinese) admin console, MCP texts and documentation.
- SQLite management store by default; external MySQL or PostgreSQL optional.
- Docker Compose deployment with a non-root container image.