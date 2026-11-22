# Environment variables

[English](configuration.md) | [简体中文](configuration.zh-CN.md)

| Variable | Required | Default | Description |
|------|------|------|------|
| `GATEWAY_MASTER_KEY` | Yes | — | base64, 32 bytes; encrypts datasource passwords and derives the session/masking keys |
| `GATEWAY_ADMIN_USER` | First start | — | Admin username, 1-64 characters |
| `GATEWAY_ADMIN_PASSWORD` | First start | — | Admin password, at least 8 characters |
| `GATEWAY_DB_PATH` | No | `data/gateway.db` (`/data/gateway.db` inside the container) | Path of the SQLite file used when no server-side management database is configured |
| `GATEWAY_DB_URL` | No | — | A full SQLAlchemy async URL (such as `mysql+aiomysql://...`, `postgresql://...`; PostgreSQL needs `asyncpg` installed); when set, it overrides the MySQL shorthand below |
| `GATEWAY_DB_HOST` | No | — | Host of the external MySQL management database; when set, the account and password must be supplied as well; SQLite is used when both this and `GATEWAY_DB_URL` are left empty |
| `GATEWAY_DB_PORT` | No | `3306` | Management database port |
| `GATEWAY_DB_USER` | No | — | Management database account |
| `GATEWAY_DB_PASSWORD` | No | — | Management database password |
| `GATEWAY_DB_NAME` | No | `db_gateway` | Management database name |
| `GATEWAY_COOKIE_SECURE` | No | `false` | Set to `true` under HTTPS; this also enables the `Strict-Transport-Security` response header |
| `GATEWAY_ADMIN_LOGIN_MAX_ATTEMPTS` | No | `5` | Failed admin sign-ins for one username+client-IP inside the window before that pair is locked (1-100) |
| `GATEWAY_ADMIN_LOGIN_WINDOW_SECONDS` | No | `300` | Failure-counting window for admin sign-in (10-86400) |
| `GATEWAY_ADMIN_LOGIN_LOCK_SECONDS` | No | `900` | Temporary lock duration after too many failed admin sign-ins (10-86400); counters are per process, so multiple workers throttle independently |
| `GATEWAY_ADMIN_SESSION_IDLE_MINUTES` | No | `30` | Idle timeout for the admin session cookie (1-1440); activity slides it, and the absolute lifetime stays 8 hours |
| `GATEWAY_LOG_LEVEL` | No | `INFO` | Log level |
| `GATEWAY_LOG_FORMAT` | No | `text` | `text` keeps the human-readable format; `json` emits one JSON object per line for application loggers with `ts`, `level`, `logger`, `message`, `request_id` (uvicorn's own startup/access lines stay plain text) |
| `GATEWAY_METRICS_ENABLED` | No | `false` | Serve Prometheus metrics at `/metrics` (HTTP, MCP tool calls, in-flight calls, pool occupancy); disabled means the route does not exist |
| `GATEWAY_AUDIT_RETENTION_DAYS` | No | `180` | Default retention days when the database is first created (1-3650) |
| `GATEWAY_AUDIT_REQUIRE_SERVER_BACKEND` | No | `false` | When `true`, refuse to start if the management store is SQLite. Recommended for production because audit writes are fail-closed and SQLite serializes writers |
| `GATEWAY_HOST` / `GATEWAY_PORT` | No | `0.0.0.0` / `8080` | In-code settings; Compose uses `GATEWAY_PORT` to map the host port |
| `GATEWAY_DATASOURCE_POOL_SIZE` / `_MAX_OVERFLOW` | No | `5` / `5` | Connection pool sizing for each business datasource engine (1-200 / 0-200); the worst case per datasource is size + overflow |
| `GATEWAY_DATASOURCE_POOL_RECYCLE_SECONDS` | No | `3600` | Recycle pooled business connections older than this many seconds (0-86400); `0` means never recycle |
| `GATEWAY_MYSQL_EXPORT_ENABLED` | No | `false` | Enable the MySQL export channel ([short-lived export credentials](export-credential.md)); when disabled it does not listen and does not load the protocol layer |
| `GATEWAY_MYSQL_EXPORT_HOST` / `_PORT` | No | `0.0.0.0` / `3307` | Listen address/port inside the container; the port must not be the same as `GATEWAY_PORT` |
| `GATEWAY_MYSQL_EXPORT_ALLOW_CIDRS` | No | empty | The **initial bootstrap value** of the export channel's source allowlist; once saved on the settings page the DB wins ([source IP allowlist](admin-ui.md#51-source-ip-allowlist)). Empty on both sides = deny all export connections, while the process still starts as normal |
| `GATEWAY_MYSQL_EXPORT_MAX_CONCURRENT` / `_MAX_ROWS` / `_MAX_SECONDS` | No | `2` / `1000000` / `600` | Concurrency, row count and duration limits ([quotas and aborts](export-credential.md#64-quotas-and-aborts)) |
| `GATEWAY_MYSQL_EXPORT_MAX_TTL_HOURS` | No | `72` | Upper bound on the lifetime of issued credentials |
| `GATEWAY_MYSQL_EXPORT_FETCH_BATCH` | No | `10000` | Rows per `fetchmany` call against the upstream (100-1000000) |
| `GATEWAY_MYSQL_EXPORT_ADVERTISE_HOST` | Required for auto-issue | empty | Export address reachable by the client; if left empty while the listener is on a wildcard address, results are only truncated and no credential is issued (6.6) |
| `GATEWAY_MYSQL_EXPORT_ADVERTISE_PORT` | No | `0` | `0` = use `MYSQL_EXPORT_PORT`; must be set explicitly when the port mapping differs from the listen port (such as container 3307, host 13307) |
| `GATEWAY_EXPORT_BIND` | No | `127.0.0.1` | Compose only: the host NIC IP that the export port binds to; must be filled in explicitly with an internal IP |
| `GATEWAY_MCP_LANGUAGE` | No | `zh` | Language of the MCP instructions and tool descriptions, `zh` or `en`; the admin console language follows the browser/cookie and is not affected by this setting |
| `GATEWAY_MCP_RESULT_MAX_ROWS` | No | `2000` | Soft cap on the rows MCP `execute_sql` returns to the AI, `0` = unlimited (6.6) |
| `GATEWAY_MCP_RESULT_MAX_BYTES` | No | `2000000` | Cap on the JSON-serialized UTF-8 bytes of an MCP result, `0` = unlimited |
| `GATEWAY_MCP_QUERY_TIMEOUT_SECONDS` | No | `30` | Wall-clock limit for one MCP SQL statement (0-3600, `0` = no timeout); on expiry the statement is killed upstream and the connection is discarded |
| `GATEWAY_MCP_WRITE_MAX_AFFECTED_ROWS` | No | `1000` | An UPDATE/DELETE affecting more rows is rolled back and reported as `WRITE_LIMIT_EXCEEDED`; `0` = unlimited |
| `GATEWAY_MCP_MAX_CONCURRENT` | No | `32` | Cap on in-flight MCP tool calls across the gateway (1-1000); calls over the cap are rejected immediately with `TOO_MANY_CONCURRENT`, never queued |
| `GATEWAY_MCP_MAX_CONCURRENT_PER_TOKEN` | No | `4` | Cap on in-flight MCP tool calls per token (0-1000); `0` leaves only the gateway-wide cap |
| `GATEWAY_MCP_RATE_LIMIT_PER_MINUTE` | No | `120` | Per-token tool-call budget per fixed 60-second window (0-100000); over-budget calls return `RATE_LIMITED`; `0` disables it |
| `GATEWAY_MCP_EXPORT_AUTO_ISSUE` | No | `true` | Automatically issue a short-lived export credential when a result exceeds the limits; has no effect when the export channel is not enabled |
| `GATEWAY_MCP_EXPORT_TTL_SECONDS` | No | `1800` | Lifetime of auto-issued credentials (seconds, 60-86400); must not exceed `MYSQL_EXPORT_MAX_TTL_HOURS * 3600` |
| `GATEWAY_MCP_EXPORT_MAX_ACTIVE` | No | `3` | Cap on in-flight auto credentials for the same token + datasource; when exceeded, the oldest auto credential is revoked first; manually issued credentials are unaffected |
| `GATEWAY_EXCHANGE_TTL_MINUTES` | No | `15` | Default short-credential lifetime in minutes (1-1440); see [credential exchange](exchange-credential.md) |
| `GATEWAY_EXCHANGE_MAX_TTL_MINUTES` | No | `60` | Upper bound a request may ask for (minutes, 1-1440); must not be below the default |
| `GATEWAY_MCP_ALLOW_CIDRS` | No | empty | Initial bootstrap value of the MCP entry point's source allowlist; also maintained on the settings page. **Opposite semantics to the export channel**: when neither the DB nor the environment variable has ever been configured, sources are unrestricted (an upgrade does not cut off callers already connected); once configured, anything outside the allowlist gets 403 |
| `GATEWAY_TRUSTED_PROXIES` | No | empty | Comma-separated proxy CIDRs; `X-Forwarded-For` is trusted only when the TCP peer falls inside one of them, and the first non-trusted hop is taken from right to left. Leave it empty when exposing the service directly, otherwise the source IP can be forged |

Management database precedence: `GATEWAY_DB_URL` → the MySQL shorthand such as `GATEWAY_DB_HOST` → the SQLite file pointed to by `GATEWAY_DB_PATH`. SQLite only suits single-instance, low-concurrency use; for multiple replicas or production deployments use an external MySQL (or PostgreSQL). A MySQL management database replays `migrations/*.sql` in filename order at startup, so the account needs table-creation privileges; other backends create their tables from the ORM metadata. MySQL 8.0 (both forms) and PostgreSQL 16 + `asyncpg` 0.32 have been tested.

Keep `.env` on the deployment host only, with `chmod 600` permissions recommended, and do not commit it to Git.
