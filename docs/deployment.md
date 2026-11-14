# Deployment and security

[English](deployment.md) | [简体中文](deployment.zh-CN.md)

- **Least privilege for upstream database accounts**: read-only datasources use an account with only `SELECT/SHOW`; writable sources are granted DML only on the tables that need it. The policy engine is not a substitute for database privileges.
- **HTTPS is mandatory in production**: the gateway itself only listens on HTTP, so terminate TLS in a reverse proxy on the internal network in front of it and set `GATEWAY_COOKIE_SECURE=true`. Tokens and SQL cross the network; plaintext HTTP is only acceptable in an isolated internal-network debugging environment.
- **Master key backup**: once `GATEWAY_MASTER_KEY` is lost, the stored datasource passwords cannot be recovered; do not keep it in the same place as the database backups.
- **Audit records contain full SQL literals**: the audit database is sensitive data, so restrict database access and encrypt backups. Audit retention defaults to 180 days and is cleaned up automatically every day at 03:00 (Asia/Shanghai); cleanup can also be run manually in the admin console.
- **Logs**: application logs do not record Authorization, plaintext tokens, passwords, decrypted datasource passwords or query result values; raw database errors are written only to the audit detail, which is visible to admins only.
- **Single process**: MCP and the admin console share one uvicorn process; connection pools are isolated per datasource (pool size 5 + 5 overflow).
- **The source allowlists are not equally strong**: the export channel takes the TCP peer address, which cannot be spoofed; the MCP entry takes the first `X-Forwarded-For` address, and when the gateway is directly exposed that header is written by the caller itself and can be spoofed. It only holds when clients can reach the gateway exclusively through the reverse proxy. Both allowlists can be maintained on the settings page ([Source IP allowlist](admin-ui.md#51-source-ip-allowlist)); `/admin` is not allowlist-restricted, so a misconfiguration can always be corrected.
- **Threat model, security boundaries and known limitations**: see [SECURITY.md](../SECURITY.md).

## First-deployment acceptance

1. `docker compose up -d --build`, and the gateway container runs normally (by default it uses the in-container SQLite management database; production should switch to an external MySQL, see [Environment variables](configuration.md)).
2. `curl http://127.0.0.1:8080/healthz` returns `{"status":"ok"}`.
3. Admin console login succeeds; a wrong password is rejected and no session is created.
4. Create a test datasource (it is saved only when the connection succeeds).
5. Create a token and run `search_objects` and `SELECT 1` with an MCP client.
6. Run an `INSERT` against a read-only source and confirm it is denied and the audit status is `denied`.
7. Query a table that has a `phone` column and confirm the results are masked; the request can be found in the audit records.

## Known limitations

- By design, **query result row counts and execution time are not limited**. Large result sets can consume memory and slow queries can stretch out responses; setting `max_execution_time` / `max_statement_time` on the upstream database is recommended, along with monitoring through the audit fields `duration_ms` and `row_count`.
- Masking is a conservative mapping over the SQL AST and does not track full column lineage across multiple levels of subqueries/CTEs; when parsing fails it falls back to the strictest rule or automatic keyword detection, preferring to over-mask.
- The internal logic of stored procedures/functions is not parsed; database account privileges remain the last boundary.
- Each user can have only one enabled token at a time; tokens have no expiry, and disabling one takes effect immediately.
- Token authorization is a datasource/database/table/column allowlist, not row-level permissions; when columns are restricted, SQL whose column ownership cannot be resolved is denied (deny rather than relax).
- One datasource still maps to one host plus one set of connection credentials; multiple databases are expressed through the accessible-databases list. There is no logical aggregation across instances (split cross-instance access into multiple datasources; a token can be authorized for several at once).
- The authorization selector caps the table list at 2000 and the column list at 5000; beyond that only the first N items are shown (the API returns `truncated=true`), and the objects already selected can be kept directly on the edit page.
- Admin console sessions are fixed at 8 hours; changing the password immediately invalidates all old sessions.
- Master key rotation is not supported in this first release.
- The default SQLite management database only suits single-instance, low-concurrency use: writes are serialized and multiple replicas would each hold their own copy of the data; switch to an external MySQL in production. Non-MySQL management databases build their tables from ORM metadata, and upgrades only add missing columns instead of replaying migrations.
