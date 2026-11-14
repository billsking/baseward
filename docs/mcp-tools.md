# MCP tools

[English](mcp-tools.md) | [简体中文](mcp-tools.zh-CN.md)

The `datasource` parameter of every tool accepts an **exact name** or a **unique business-description fragment**: the datasource remark (the "Remark" field in the admin console) can serve as an alias, so when the remark is "示例业务库" you can pass `"示例业务库"` directly. When the value matches multiple datasources, the call returns `DATASOURCE_AMBIGUOUS` together with the candidate list.

Recommended call order: `list_datasources` → `describe_datasource` (or `search_objects`) → `execute_sql`, to avoid repeated rounds of probing.

### list_datasources

No parameters. Returns the enabled datasources the current token can access:

```json
{"request_id": "...", "count": 1,
 "datasources": [{"name": "demo", "description": "示例业务库", "default_database": "demo",
                  "is_read_only": true,
                  "databases": [{"name": "demo", "read_only": true}, {"name": "report", "read_only": false}]}]}
```

- `databases[]` is the token's **available databases** on that datasource (accessible ∩ authorized), and `read_only` is the **effective** policy, merged with the datasource master switch; it is `[]` when the token has no available databases on that datasource.
- `is_read_only` is retained but demoted to a **datasource aggregate field** (the master switch is read-only, or all available databases are read-only) and does not vary by token; to tell whether a single database is writable, look at `databases[].read_only`.

### describe_datasource

Returns table schemas in one call, avoiding the two rounds of "look up tables first, then columns":

```json
{"datasource": "示例业务库", "pattern": "批次 卡密", "include_columns": true, "limit": 50}
```

- `pattern` is optional (≤128 characters); space-separated words must all match. It matches table names, table comments, column names and column comments.
- `database` is optional and narrows the search to one database; when omitted, the search runs across **all available databases**. Passing a database that is not exposed returns `DATABASE_NOT_EXPOSED`; passing one that is exposed but for which the token has no authorization at all returns `DATABASE_DENIED` (neither returns an empty result, so it cannot be misread as "this database has no tables").
- `include_columns=false` returns only the table list, which suits large databases.
- `limit` caps the table count at 1-100 (default 50); each table returns at most 100 columns and the total column cap is 3000, with `truncated=true` when exceeded.
- Returns `description`, `default_database`, `is_read_only` (the aggregate field), `databases[]` (available databases with their effective `read_only`) and `tables[]`; each table carries `database/table/comment/columns[]`, and each column carries `name/data_type/nullable/primary_key/comment`.

### search_objects

```json
{"datasource": "demo", "pattern": "order", "object_type": "all", "database": null}
```

- `pattern` is 1-128 characters; space-separated words must all match. It matches table names, column names, table comments and column comments (`% _ \` are escaped).
- `object_type` takes `all/table/column`.
- `database` is optional, with the same semantics and denial contract as `describe_datasource`: when omitted, all available databases are searched; not exposed / not authorized return `DATABASE_NOT_EXPOSED` / `DATABASE_DENIED` respectively. The response echoes `database` and `databases[]`.
- Results carry `comment` and are sorted by relevance (exact name match > prefix > substring > comment), at most 200 entries, with `truncated=true` when there are more.

### execute_sql

```json
{"datasource": "demo", "sql": "SELECT id, phone FROM users WHERE id = 1", "database": "report"}
```

- `database` is optional: the **effective default database** for this execution. It only decides which database a table written without a database name lands in, and it **does not widen the authorization scope**; when omitted, the datasource default database is used. Every `db.table` appearing in the SQL still passes the exposure and authorization checks one by one.
- Passing a database that is not exposed returns `DATABASE_NOT_EXPOSED`; passing one that is exposed but not authorized for this token returns `DATABASE_DENIED`. Both messages contain only the database name, never table schemas.
- When the upstream returns 1146 (table does not exist) and the SQL contains a table without a database qualifier, the gateway looks for a table of the same name in the other authorized available databases and appends the candidate database names to the end of the error message; it **does not switch databases automatically**.

Rows are returned as an array of arrays (so duplicate column names are not lost):

```json
{
  "request_id": "1b2d...",
  "datasource": "demo",
  "database": "report",
  "statement_type": "Select",
  "columns": ["id", "phone"],
  "rows": [[1, "138****1234"]],
  "row_count": 1,
  "duration_ms": 12,
  "masked_columns": ["phone"],
  "truncated": false,
  "truncated_reason": null,
  "hint": null,
  "export": null
}
```

Write operations return `columns=[]` and `rows=[]`, `row_count` is the number of affected rows, and the same four fields such as `truncated=false` are included; for truncation when results exceed the limit, and for `hint` and `export`, see 6.6.

Errors are returned with MCP `isError=true`, and the text is JSON:

```json
{"error": {"code": "WRITE_ON_READONLY", "message": "数据源 demo 为只读，禁止写操作", "request_id": "..."}}
```

### issue_export_credential

```json
{"datasource": "demo", "sql": "SELECT id, phone FROM users WHERE ...", "reason": "导出某商户 2026-10 订单", "database": "report"}
```

When you already know the result set will exceed the return limit, or that `execute_sql` will time out, exchange it for a short-lived read-only MySQL credential before running the script:

```json
{
  "request_id": "9c1e...",
  "protocol": "mysql",
  "host": "192.0.2.12",
  "port": 3307,
  "user": "exp_9f3a1c7b2d85",
  "password": "<shown only once in this response>",
  "database": "demo",
  "source_ip": "192.168.1.20",
  "expires_at": "2026-10-28T07:30:00Z",
  "sql": "SELECT id, phone FROM users WHERE ...",
  "notice": "该凭证仅用于本次导出...",
  "sample_code": "import csv\nimport os\n..."
}
```

- `sql` is required: before issuance it is pre-checked against the token's authorization scope and read-only policy, but not executed; write statements, DDL and out-of-scope access are rejected outright.
- `reason` is required (≤200 characters): written to the audit, and used to distinguish a deliberate request from the truncation fallback.
- `database` is optional: the credential's **target database**, taken from the datasource default database when omitted; the returned `database` is the target database actually in effect, fixed once the credential is issued.
- The lifetime is fixed at `GATEWAY_MCP_EXPORT_TTL_SECONDS`; active credentials for the same token + datasource are limited by `GATEWAY_MCP_EXPORT_MAX_ACTIVE`.
- `source_ip` is the source IP the gateway sees for this MCP request, and the fetching script must connect from that same address (6.7); the MCP side does not allow the caller to specify the bound value.
- Do not write the returned password into code or configuration files; the script reads it from the `GATEWAY_EXPORT_PASSWORD` environment variable (see `sample_code`).
- When the channel is not enabled or `ADVERTISE_HOST` is not configured, the call returns `EXPORT_UNAVAILABLE`; invalid parameters return `INVALID_ARGUMENT`.

### Token scopes

- **Four-level allowlist**: datasource → database → table → column. An empty level means no restriction at that level; selecting a lower level must include its parent; when a table is selected but no columns are checked, that table's columns are unrestricted.
- "All" at any level never goes beyond the datasource's accessible databases; datasource-level "all tables" is equivalent to setting "all tables" on every accessible database.
- The database level has three states: all tables / specified tables / not allowed. Database-level "all tables" entries are capped at 512; beyond that, a prompt suggests using datasource-level "all tables" instead.
- When columns are restricted, `execute_sql` checks the projection, `WHERE/JOIN/GROUP BY/HAVING/ORDER BY`, and the `SET/WHERE/INSERT` columns of write statements; `SELECT *` / `t.*` are automatically expanded to the allowed columns before execution. When column ownership cannot be determined or reading the table schema fails, the statement is rejected outright, not let through.
- `search_objects` / `describe_datasource` return only tables and columns within the token's available databases.
- Editing the authorization scope takes effect immediately and does not rotate the token plaintext; the behavior of disabling or deleting a token is unchanged.
- Authorization is only a datasource/database/table/column allowlist, with no row-level filtering (no WHERE condition is injected automatically).

### Error codes

`missing_token` / `invalid_token` (HTTP 401), `auth_unavailable` (HTTP 503), `INVALID_ARGUMENT`, `DATABASE_NOT_EXPOSED`, `DATABASE_DENIED`, `DATASOURCE_NOT_FOUND`, `DATASOURCE_AMBIGUOUS`, `DATASOURCE_DISABLED`, `DATASOURCE_DENIED`, `TABLE_DENIED`, `FIELD_DENIED`, `FIELDS_REQUIRED`, `FIELD_SCOPE_CHECK_FAILED`, `EMPTY_SQL`, `SQL_PARSE_ERROR`, `MULTI_STATEMENT`, `DDL_BLOCKED`, `UNSUPPORTED_STATEMENT`, `LOCKING_READ`, `INTO_OUTFILE`, `WRITE_ON_READONLY`, `NO_WHERE`, `EXPORT_UNAVAILABLE`, `CREDENTIAL_ISSUE_DENIED`, `AUDIT_UNAVAILABLE`, `DB_UNAVAILABLE`, `DB_ERROR`, `INTERNAL_ERROR`.

The messages of `DATABASE_NOT_EXPOSED` and `DATABASE_DENIED` contain only the database name, not table schemas: the former says an admin has not checked that database into the datasource's accessible databases, the latter says the database is exposed but the token has no authorization for it.
