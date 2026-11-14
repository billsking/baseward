# Temporary export credential (MySQL protocol)

[English](export-credential.md) | [简体中文](export-credential.zh-CN.md)

For large result-set exports by scripts and programs: an admin issues a temporary MySQL account in the admin console, the consumer connects a standard MySQL client directly to the gateway's export port, and the gateway fetches rows from upstream in batches and streams them back. When disabled (the default), no port is listened on and the protocol layer is not loaded.

## 6.1 Enabling

```bash
# .env (bootstrap values only; afterwards maintain them on the admin settings page, where the DB wins — see "Admin console > Source IP allowlist")
GATEWAY_MYSQL_EXPORT_ENABLED=true
GATEWAY_MYSQL_EXPORT_ALLOW_CIDRS=192.168.1.0/24,192.168.1.20
GATEWAY_EXPORT_BIND=192.168.1.10   # the host's private-network NIC IP; bind the export port to that NIC only
```

- **The allowlist is the only network-layer control** (no TLS in v1): the value comes from the TCP peer address and cannot be spoofed; an empty allowlist does not mean "unrestricted", it denies all export connections. The startup gate "refuse to start when export is enabled and the allowlist is empty" has been removed (see [Source IP allowlist](admin-ui.md#51-source-ip-allowlist)); the fallback moved to connect time.
- The compose port mapping must be written as `<private-nic-ip>:3307:3307`; **do not write `3307:3307`** (equivalent to `0.0.0.0:3307`, reachable from the public internet). The security group must also allow 3307 only from the consumer's network segment.
- The allowlist is an application-layer decision, while port binding and the security group are the network-layer exposure; both have to be tightened. This is a hard requirement when the production host has a public IP.
- Export uses upstream connections from the datasource connection pool and shares the same policy, masking and audit as MCP.

## 6.2 Issuing and connecting

In the admin console, "Credentials > Issue credential": select the token, datasource, **target database** and lifetime (no more than `GATEWAY_MYSQL_EXPORT_MAX_TTL_HOURS`), and confirm the "Allowed source IP". It defaults to the request IP of the current issue page; when the credential goes to a script on another machine, change it to that machine's egress IP. The account looks like `exp_a1b2c3d4e5f6`, the plaintext password is shown only once on the issue page, and only `auth_string` (a digest) is stored in the database.

- **Target database candidates = the databases usable by that token on that datasource** (accessible databases ∩ authorized databases). The datasource's default database is selected by default (if it is a usable database), otherwise the first usable database is selected automatically. With no candidates the dropdown is empty, submission is disabled and the page asks you to adjust the token's grants first; the server refuses to issue as well.
- **The target database is fixed at issue time**: the connection lands in that database, and changing the datasource's default database later does not affect credentials already issued. To switch databases, revoke and re-issue (editing and renewal are not offered).
- **Other usable databases** on the same datasource can be reached with `USE` after connecting (decisions and limits in 6.8); other datasources require separate credentials.
- When the target database is removed from the accessible set the credential is not revoked automatically, but the first statement after connecting returns `DATABASE_NOT_EXPOSED` (1235) and writes a denied audit record.

```bash
mysql --quick -h <gateway-host> -P 3307 -u exp_a1b2c3d4e5f6 -p'<plaintext-password>' <target-db>
```

```python
# SQLAlchemy + PyMySQL: stream_results is mandatory, otherwise the whole result piles up in client memory first
engine = create_engine("mysql+pymysql://exp_a1b2c3d4e5f6:<password>@<host>:3307/<target-db>?charset=utf8mb4")
with engine.connect() as conn:
    result = conn.execution_options(stream_results=True, yield_per=1000).exec_driver_sql(sql)
    for row in result:
        ...
```

```java
// JDBC: a server-side cursor is mandatory, otherwise the whole result piles up in client memory first
String url = "jdbc:mysql://<host>:3307/<target-db>?useSSL=false&allowPublicKeyRetrieval=true"
           + "&useCursorFetch=true&defaultFetchSize=1000";
```

```javascript
// mysql2 (Node.js): use a streaming query, do not await the whole result
const conn = mysql.createConnection({host, port: 3307, user, password, database: "<target-db>"});
conn.query('SELECT id, label FROM big_table').stream().on('data', (row) => { /* ... */ });
```

> The database name in the connection string is only a handshake hint: the gateway overrides the session's current database with the credential's target database (see 6.8), so **changing the database name in the URL does not switch the session**. To switch databases, use an explicit `USE`.

## 6.3 Clients must use a streaming cursor (`useCursorFetch`)

The gateway fetches rows from upstream according to `GATEWAY_MYSQL_EXPORT_FETCH_BATCH` and writes them to the client batch by batch, so server-side memory is bounded; but if the client driver does not enable a server-side cursor, it will **buffer the entire result in the client process**, and exporting a large table will OOM:

| Client | How to enable |
| --- | --- |
| MySQL CLI | Add `--quick` (by default the whole result set is buffered in client memory; do not skip this flag when exporting large tables) |
| JDBC / Connector/J | Add `useCursorFetch=true&defaultFetchSize=1000` to the URL and call `setFetchSize(1000)` on the Statement |
| PyMySQL | `cursor(pymysql.cursors.SSCursor)` (or `SSCursor(conn)`) |
| SQLAlchemy | `execution_options(stream_results=True, yield_per=1000)` |
| Node mysql2 | `conn.query(sql).stream()` |

The server supports prepared statements and batched `COM_STMT_FETCH` (including cursors that span batches); JDBC's `useCursorFetch` takes exactly this path.

## 6.4 Quotas and aborts

| Limit | Environment variable | Default | Behavior when exceeded |
| --- | --- | --- | --- |
| Concurrent exports | `GATEWAY_MYSQL_EXPORT_MAX_CONCURRENT` | 2 | 1040; extra requests are rejected outright, not queued |
| Max rows | `GATEWAY_MYSQL_EXPORT_MAX_ROWS` | 1000000 | 1105 `GATEWAY:EXPORT_ROW_LIMIT` |
| Max duration | `GATEWAY_MYSQL_EXPORT_MAX_SECONDS` | 600 | 1105 `GATEWAY:EXPORT_TIME_LIMIT` |
| Credential lifetime | `GATEWAY_MYSQL_EXPORT_MAX_TTL_HOURS` | 72 | 1045 after expiry |
| Client disconnect | - | - | Stops producing and runs `KILL QUERY` upstream, audit `interrupted` |

Every error packet carries `request_id=`, which can be matched against the admin console audit; raw database errors are written only to the audit detail and are not returned to the client.

## 6.5 Known behavior

1. **Single statement**: multiple statements return 1064; `SELECT ... INTO OUTFILE/DUMPFILE` is also 1064 (sqlglot cannot parse that syntax), and `SELECT ... INTO @var` returns 1235.
2. **Read-only allowlist**: only read statements such as SELECT/SHOW/DESCRIBE/EXPLAIN; write statements, DDL, GRANT and CALL are all 1235.
3. **Fixed target database**: after connecting, the session's current database is the credential's target database (fixed at issue time); the database name in the connection string/URL is overridden by the gateway, and changing it does not switch databases. The rules for `USE` on other databases are in 6.8.
4. **Metadata filtered by scope**: all six entry points, `SHOW TABLES`, `DESCRIBE`, `SHOW COLUMNS`, `SHOW DATABASES`, `INFORMATION_SCHEMA` and `COM_FIELD_LIST`, are filtered by the table/column allowlist captured at issue time; out-of-scope objects never appear.
5. **Masking follows the datasource policy**: masked columns have the column type `VAR_STRING` on the export channel, and their values match what MCP returns.
6. **Quotas and caps**: concurrency, rows, duration and TTL are in 6.4; buffered execution (no cursor enabled) is subject to the same row/duration caps.
7. **Disconnect, revocation and expiry**: a client disconnect stops production and cleans up the upstream query; after a credential is revoked or expires, new connections get 1045 and established connections are rejected on their next statement.
8. **No TLS, no KILL support**: v1 controls access with the source IP allowlist (6.1) plus credential source binding (6.7), and a client-sent `KILL` is rejected. Export runs in the same process as MCP, yielding the event loop every 50 rows and draining every 1000 rows so that MCP requests are not held up for long.

## 6.6 Automatic handoff for large MCP results (v1.5)

When a single read-only `execute_sql` query exceeds the return cap, the gateway **does not raise an error**: it truncates the result into a preview and (when the conditions are met) automatically issues a short-lived credential so the AI can write its own code to fetch the full result.

**Two caps**

| Cap | Setting | Implementation | When triggered |
|------|------|---------|--------|
| Rows | `GATEWAY_MCP_RESULT_MAX_ROWS` (default 2000) | Appends `LIMIT cap+1` at the SQL layer; the one extra row is used only to detect truncation | `truncated_reason="rows"` |
| Bytes | `GATEWAY_MCP_RESULT_MAX_BYTES` (default 2 MB) | Accumulated UTF-8 byte count of the per-row JSON serialization after masking | `truncated_reason="bytes"` |

- When both are hit, `"rows"` is recorded (rows are judged first); setting either to `0` disables that dimension, and with both set to `0` the behavior is exactly the same as v1.4.
- When the user's SQL already carries a smaller `LIMIT`, nothing is appended and truncation is not judged; statements other than `SELECT`/`UNION`, such as `SHOW`/`DESCRIBE`/`EXPLAIN`, get no appended `LIMIT` and are constrained only by the byte cap.
- The row cap is a **soft cap** (`LIMIT` is appended to the final query, so aggregate/subquery semantics can partly bypass it); the byte cap is the hard fallback. The export channel's own row/duration caps (6.4) are unrelated to this.

**Enabling the handoff** (all three must hold before a credential is issued)

```bash
GATEWAY_MYSQL_EXPORT_ENABLED=true
GATEWAY_MYSQL_EXPORT_ADVERTISE_HOST=192.0.2.12   # an address the client can reach; it cannot be inferred from 0.0.0.0
GATEWAY_MCP_EXPORT_AUTO_ISSUE=true              # on by default
```

**Explicit request**: when the result set is known to exceed the cap, or `execute_sql` would time out (so no truncated response is ever received), the agent can call
`issue_export_credential(datasource, sql, reason)` directly to get a credential first and then run its script. Before issuing, `sql` is pre-checked (not executed) against that token's datasource/table/column
grants and the read-only policy; write statements, out-of-scope access and a disabled channel are all rejected. The audit records are
`mcp_tool` (tool=`issue_export_credential`) plus `credential_lifecycle` (tool=`issue_explicit`).
The implicit truncation handoff is kept as the fallback when the agent does not call the tool itself.

**Returned fields** (the remaining fields match `execute_sql` in chapter 7)

```json
{
  "truncated": true,
  "truncated_reason": "rows",
  "hint": "结果已截断，请用下方 export 凭证通过 MySQL 协议取全量数据。",
  "export": {
    "protocol": "mysql",
    "host": "192.0.2.12",
    "port": 3307,
    "user": "exp_9f3a1c7b2d85",
    "password": "<shown only once in this response>",
    "database": "demo",
    "source_ip": "192.168.1.20",
    "expires_at": "2026-10-28T07:30:00Z",
    "sql": "SELECT id, phone, created_at FROM users",
    "notice": "该凭证仅用于本次导出，请勿写入代码仓库或配置文件；凭证绑定 source_ip（本次 MCP 请求被网关看到的来源 IP），取数脚本要从同一来源地址连接，否则握手会被拒绝；取数完成后可忽略，到期自动失效。",
    "sample_code": "import csv\nimport os\n\nimport pymysql\n..."
  }
}
```

- `export.sql` is the **user's original SQL** (without the gateway-appended `LIMIT` and without `SELECT *` expansion); the export channel re-runs policy, authorization rewriting and masking itself.
- `sample_code` streams to CSV with `pymysql`'s `SSCursor`; the password is read from the environment variable `GATEWAY_EXPORT_PASSWORD` and is not written into the generated file.
- The credential's capabilities are **strictly equal to its token's** (including the three-level allowlists), with a default lifetime of 30 minutes; when automatic credentials for the same token + datasource exceed `GATEWAY_MCP_EXPORT_MAX_ACTIVE`, the oldest one is revoked first. Credentials issued manually by an admin are never affected.
- `export.source_ip` is the source address this credential is bound to, and the fetching script must open its TCP connection from that same IP (see 6.7). It cannot be specified on the MCP side, only passed through as is.
- For queries that are not truncated (including a smaller user-supplied `LIMIT`, or both caps disabled), the four new fields are fixed at `false` / `null` / `null` / `null`.

**Only `hint` when issuance is not possible** (`export=null`, `execute_sql` still returns successfully)

| Case | `hint` |
|------|--------|
| `GATEWAY_MCP_EXPORT_AUTO_ISSUE=false` | 结果已截断（{上限}）。如需全量数据，请联系管理员签发临时导出凭证，或缩小查询范围（加 WHERE / 调小 LIMIT）。 |
| Export channel not enabled | 结果已截断。导出通道未启用，请缩小查询范围后重试，或联系管理员。 |
| `ADVERTISE_HOST` not configured (or the listener is a wildcard address) | 结果已截断。导出通道地址未配置，请缩小查询范围后重试，或联系管理员。 |
| Issuance failed (token/datasource disabled, out of the authorized scope, management database write failed) | 结果已截断。自动签发导出凭证失败（{code}），请缩小查询范围后重试，或联系管理员。 |

The audit holds two records: one `mcp_tool` for the main query (`success`, `row_count` is the number of rows actually returned) and one `credential_lifecycle` for the issuance (`tool=issue_auto`, `deny_reason` of the form `mcp:gw_xxxx req=<主请求 request_id>`, which can be used to correlate them). The credential plaintext is never stored in the database, never logged and never audited; it appears only in this one response body.

## 6.7 Credential source IP binding (v1.6)

Every credential carries a `source_ip`, which is compared at handshake with the source address of the TCP connection; **a mismatch returns 1045 immediately** (the same code as the other credential rejections, with the specific reason going only into the audit as `CREDENTIAL_IP_MISMATCH`). All three issue paths bind it:

| Issue path | Bound value |
| --- | --- |
| Automatic handoff on MCP truncation | The source IP of this MCP request |
| MCP `issue_export_credential` | The source IP of this MCP request (the caller cannot specify it) |
| Admin console issuance | The form's "Allowed source IP"; when left empty, the request IP of the issue page |

- The contract is **exact match**, with no CIDR tolerance; `::ffff:192.0.2.1` and `192.0.2.1` are normalized before comparison, so a dual-stack listener does not reject by mistake.
- This is a second layer alongside the allowlist in 6.1: the allowlist governs which network segments may reach the export port (intercepted at the network layer, before authentication), while `source_ip` governs which single address may use this credential (revocation/tightening granularity down to one credential).
- When the requesting IP cannot be obtained, issuance is **refused** (fail-closed); it never degrades to an unbound credential.
- Credentials that predate the migration have `source_ip` NULL: they are treated as unbound and their source is not checked, and they converge naturally as they expire or are revoked (bounded by `GATEWAY_MYSQL_EXPORT_MAX_TTL_HOURS`). No permanent toggle is kept.
- **Deployment note**: the MCP side records the source IP as the gateway sees it. If a reverse proxy sits in front of MCP and does not pass `X-Forwarded-For`, the recorded address is the proxy's, and a script connecting from elsewhere is rejected. Troubleshooting order: the `source_ip` in the issue response / admin console list, then whether the reverse proxy forwards `X-Forwarded-For`, then the actual egress IP of the machine running the script. Admin console issuance lets you set `source_ip` manually to the script's egress IP.
- Even on the same machine you must use **the same address**: connecting to `127.0.0.1:3307` makes the gateway see `127.0.0.1` as the source, while connecting to the LAN IP `:3307` makes it see that NIC's IP. If the MCP request goes to `http://127.0.0.1:8000/mcp` but the fetching script connects to the LAN address, the two sources differ and the connection is rejected (the same applies to the smoke script's `--export-host`).

## 6.8 Multi-database sessions and `USE` (v1.8)

- After the credential logs in, the session's current database = the target database. **The database name in the connection string is overridden**: mysql-mimic first writes that name into the session during the handshake, then `init()` overrides it with the credential's target database. Changing the name in a JDBC URL or in PyMySQL `database=` looks like it took effect, but it did not. There are only two ways to switch databases: select the target database at issue time, or run an explicit `USE` after connecting.
- `USE <target-db>`: only requires that the database is still in the datasource's **accessible databases** list (token grants were already validated at issue time; object-level grants are still evaluated one by one for every SQL statement).
- `USE <other-db>`: must satisfy both **exposed** and **authorized for that token**, otherwise it is rejected:
  - The database has been removed from the accessible list: 1235 `DATABASE_NOT_EXPOSED`
  - Exposed, but the token has no grant at all on that database: 1235 `DATABASE_DENIED`
- After a successful `USE`, SQL with unqualified table names, policy and authorization decisions, masking rules, upstream execution and audit are all computed against this current database; metadata (`SHOW TABLES` / `DESCRIBE` / `COM_FIELD_LIST`) follows the current database, and the schema cache is rebuilt per "datasource + database".
- If the target database was already removed from the accessible list at connect time: login succeeds, but the next statement returns 1235 `DATABASE_NOT_EXPOSED` (the denied audit records that database); the session is never silently switched to another database.
- The export channel is always read-only, regardless of the per-database read/write policy.
