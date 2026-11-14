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
| `GATEWAY_COOKIE_SECURE` | No | `false` | Set to `true` under HTTPS |
| `GATEWAY_LOG_LEVEL` | No | `INFO` | Log level |
| `GATEWAY_AUDIT_RETENTION_DAYS` | No | `180` | Default retention days when the database is first created (1-3650) |
| `GATEWAY_HOST` / `GATEWAY_PORT` | No | `0.0.0.0` / `8080` | In-code settings; Compose uses `GATEWAY_PORT` to map the host port |
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
| `GATEWAY_MCP_EXPORT_AUTO_ISSUE` | No | `true` | Automatically issue a short-lived export credential when a result exceeds the limits; has no effect when the export channel is not enabled |
| `GATEWAY_MCP_EXPORT_TTL_SECONDS` | No | `1800` | Lifetime of auto-issued credentials (seconds, 60-86400); must not exceed `MYSQL_EXPORT_MAX_TTL_HOURS * 3600` |
| `GATEWAY_MCP_EXPORT_MAX_ACTIVE` | No | `3` | Cap on in-flight auto credentials for the same token + datasource; when exceeded, the oldest auto credential is revoked first; manually issued credentials are unaffected |
| `GATEWAY_EXCHANGE_TTL_MINUTES` | No | `15` | Default short-credential lifetime in minutes (1-1440); see [credential exchange](exchange-credential.md) |
| `GATEWAY_EXCHANGE_MAX_TTL_MINUTES` | No | `60` | Upper bound a request may ask for (minutes, 1-1440); must not be below the default |
| `GATEWAY_MCP_ALLOW_CIDRS` | No | empty | Initial bootstrap value of the MCP entry point's source allowlist; also maintained on the settings page. **Opposite semantics to the export channel**: when neither the DB nor the environment variable has ever been configured, sources are unrestricted (an upgrade does not cut off callers already connected); once configured, anything outside the allowlist gets 403 |
| `GATEWAY_TRUSTED_PROXIES` | No | empty | Comma-separated proxy CIDRs; `X-Forwarded-For` is trusted only when the TCP peer falls inside one of them, and the first non-trusted hop is taken from right to left. Leave it empty when exposing the service directly, otherwise the source IP can be forged |

Management database precedence: `GATEWAY_DB_URL` → the MySQL shorthand such as `GATEWAY_DB_HOST` → the SQLite file pointed to by `GATEWAY_DB_PATH`. SQLite only suits single-instance, low-concurrency use; for multiple replicas or production deployments use an external MySQL (or PostgreSQL). A MySQL management database replays `migrations/*.sql` in filename order at startup, so the account needs table-creation privileges; other backends create their tables from the ORM metadata. MySQL 8.0 (both forms) and PostgreSQL 16 + `asyncpg` 0.32 have been tested.

Keep `.env` on the deployment host only, with `chmod 600` permissions recommended, and do not commit it to Git.
