# Engine compatibility

[English](compatibility.md) | [简体中文](compatibility.zh-CN.md)

BaseWard speaks the **MySQL wire protocol and the MySQL SQL dialect**. Anything that answers on
that protocol is a candidate upstream, so the scope is the MySQL family rather than one product.

Two claims are kept apart on purpose:

- **Protocol compatibility** — BaseWard can connect, read metadata and stream results.
- **Policy verification** — default-deny, four-level token scopes, masking and fail-closed audit
  have actually been executed against that engine and asserted in tests.

MySQL 8.0, MariaDB 11.8, TiDB v8.5 and OceanBase CE 4.4 satisfy both today, each backed by a
recorded run of the full integration suite. OceanBase needs one session setting raised first
(`ob_query_timeout`, see the findings); the rest are expected to work but unverified.

## Status

| Engine | Protocol | Policy verified | Status |
|---|---|---|---|
| MySQL 8.0 | yes | **yes** (8.0.46) | Reference engine. The integration suite runs green on it. |
| MariaDB 10.6+ / 11.x | yes (fork) | **yes** (11.8.9) | `information_schema` and `SHOW` stay close to MySQL. 10.x not executed. |
| TiDB (MySQL mode) | yes | **yes** (v8.5.0, single node) | Passes the suite, with the deltas below. Cluster topology is not covered. |
| OceanBase (MySQL mode) | yes | **yes** (4.4.2.1) | Passes the suite once `ob_query_timeout` is raised above its 10 s default. The pool forces `SET autocommit=0` at connect time, which this engine needs for the audit-rollback guarantee. See the record below. |
| MySQL 5.7, 8.4 | yes | no | Expected to work. Not executed. |
| Amazon Aurora MySQL, RDS MySQL | yes | no | Expected. Managed MySQL, same dialect. |
| Percona Server for MySQL | yes | no | Expected. Not executed. |
| PolarDB MySQL, PolarDB-X | yes | no | Expected, with the deltas below. No image available to execute against. |
| TDSQL (MySQL mode), GoldenDB, GaussDB(for MySQL) | yes | no | Expected, with the deltas below. No image available to execute against. |
| OceanBase (Oracle mode), openGauss / GaussDB, Dameng, KingBase | no | no | Not supported: different dialect and different metadata dictionary. |
| PostgreSQL, SQL Server, Oracle, ClickHouse, SQLite | no | no | Not supported. PostgreSQL is on the roadmap. |

The management database (BaseWard's own metadata store) is a separate choice from the engines
behind it: it defaults to a local SQLite file, and MySQL 8.0 and PostgreSQL 16 are the tested
external backends (see [configuration.md](configuration.md)).

## Verification record

"Verified" here means: the integration suite in `tests/integration/` was executed against that
engine and passed, asserting AST default deny, four-level token scopes, result masking and
fail-closed audit. Each row below is one run.

| Date | Engine | Result | Environment |
|---|---|---|---|
| 2026-11-09 | MySQL 8.0.46 | 243 passed | `mysql:8.0` container, Linux bridge networking |
| 2026-11-09 | MariaDB 11.8.9 | 243 passed | `mariadb:11` container |
| 2026-11-09 | TiDB v8.5.0 | 243 passed | `pingcap/tidb` container, `--store unistore` single node |
| 2026-11-09 | OceanBase CE 4.4.2.1 | 243 passed | `oceanbase/oceanbase-ce` container, single zone, MySQL mode, tenant `test`; `ob_query_timeout` raised from its 10 s default to 600 s for the run, `ob_trx_timeout` left at its default |

All four runs are the same suite at the same commit, executed one engine at a time on an otherwise
idle host. The suite is 243 tests; it grew from 242 when `test_pool_session_has_autocommit_off` was
added alongside the connect-time fix below. Earlier runs on the same day scored OceanBase 240/242 —
the two failures were the autocommit gap (now fixed) and the 10 s statement quota cutting the
200 000-row seeding fixture (now raised for the run, and still a real default worth knowing about).
The recorded commit predates the system-schema exclusion fix described below; the suite has not been
re-run on these four engines since that change.

Scope limits that a green run does not cover:

- TiDB ran in **unistore standalone mode**: the SQL layer, transactions and metadata behave as
  documented, but real PD/TiKV topology, region-level errors and multi-node DDL are not exercised.
- Single-container runs do not exercise replication, failover, or an engine's own privilege and
  tenant model beyond what the test account happens to hold.
- The run provisions BaseWard's management schema on the target engine because the suite needs a
  disposable server. That is **not** a support statement for the management database, whose tested
  backends are SQLite, MySQL 8.0 and PostgreSQL 16 — not TiDB or OceanBase.
- Streaming fixtures once seeded rows with MariaDB's `seq_1_to_N` pseudo-table, which MySQL rejects.
  They now generate the series with plain `INSERT ... SELECT`, so this suite is engine-neutral and
  a failure on a non-MySQL engine is a compatibility finding rather than a test bug.

## Findings from the runs

**OceanBase leaves autocommit on, which broke the audit-rollback guarantee (fixed in the pool).**
BaseWard executes a write inside `engine.begin()` (`app/mcp/pipeline.py:258-263`): if the audit row
cannot be finalized, the exception escaping that block is what rolls the write back. MySQL 8.0.46,
MariaDB 11.8.9 and TiDB v8.5.0 report `@@autocommit = 0` on a connected session, because the client
requests autocommit-off in the handshake. OceanBase CE 4.4.2.1 reports `@@autocommit = 1` — it does
not honour that flag — so every statement commits the moment it finishes, the rollback becomes a
no-op, and a failed audit finalization leaves the write in place while the audit row says `error`
and the caller gets `AUDIT_UNAVAILABLE`. Measured directly: after an `UPDATE` plus a forced rollback
the stored value is `orig` on MySQL and TiDB and `changed` on OceanBase.

This was a gap in one advertised guarantee, not a policy bypass: the statement was still parsed,
scoped, allowed and audited. The fix is now in the pool — every business connection opens with
`SET autocommit=0` as its `init_command` (`app/datasources/pool.py:114-119`), a no-op on engines that
already honour the handshake flag. Both `init_command` and a connect-time event produced
`rolled_back=True` in the same OceanBase probe, and the recorded 243-test OceanBase run passes
`test_pool_session_has_autocommit_off`. A datasource switched to writable
(`app/datasources/service.py:248`) therefore goes through the same fail-closed audit rollback there.

**OceanBase's default statement quota cuts long work.** `ob_query_timeout` defaults to 10 s
(`10000000` µs) and aborts with error `4012`, so a statement BaseWard would happily run all the way
through is stopped by the engine: it cut the 200 000-row seeding fixture in the streaming test. Set
`ob_query_timeout` (and check `ob_trx_timeout`) on the datasource account or in the session before
pointing large exports or heavy metadata reads at OceanBase, and remember BaseWard's own
`GATEWAY_MYSQL_EXPORT_MAX_SECONDS` is a different, independent limit.

**System-schema exclusion was case-sensitive and engine-blind**, as predicted in delta 1 below: the
real code path returned `INFORMATION_SCHEMA`, `METRICS_SCHEMA`, `PERFORMANCE_SCHEMA` and `test` on
TiDB v8.5.0, and `oceanbase` plus `test` on OceanBase CE 4.4.2.1, with nothing on MySQL 8.0.46 or
MariaDB 11.8.9. Metadata noise, not access: token scopes still applied to anything listed there.
Since fixed — one lowercase list including `metrics_schema` and `oceanbase`, compared
case-insensitively — but the fix is asserted by unit tests only. The four engine runs above predate
it, so "the noise is gone on TiDB and OceanBase" is not yet a measured claim.

## Why "unverified" is not the same as "unsafe"

Policy is decided on the parsed AST, never on string matching, and the parse is pinned to the
MySQL dialect:

- `sqlglot.parse(sql, read="mysql")` — `app/policy/engine.py:134`; the rewritten statement is
  generated back with `dialect="mysql"` in `app/query/pipeline.py:405` and `:419`.
- Unparseable SQL is denied (`app/policy/engine.py:135-137`).
- Anything the parser degrades into a raw command is denied: `exp.Command` is a member of
  `_FORBIDDEN_NODES` (`app/policy/engine.py:33`), and only statements that resolve to
  `SELECT` / `UNION` / `DESCRIBE <table>` / `INSERT` / `UPDATE` / `DELETE` are allowed through
  (`app/policy/engine.py:157-162`).

Consequence for an engine that accepts a vendor-specific statement MySQL does not: BaseWard
denies it. On a non-reference engine the risk is **narrowed functionality, not a policy bypass**.
`REPLACE` is the one deliberate exception — it parses to `Command` in the MySQL dialect, so
`app/policy/engine.py:77` reparses it as `INSERT` and runs it through the same write checks;
if the reparse fails the statement is denied.

## Deltas to check before trusting an engine

Each item is a concrete place where a MySQL-family engine can differ, with the code that assumes
MySQL.

1. **System-schema list is one lowercase set** — `mysql`, `information_schema`,
   `performance_schema`, `sys`, `metrics_schema`, `oceanbase`
   (`app/datasources/databases.py:34`). Every `information_schema` read builds its predicate with
   `exclude_system_schemas()` (`app/datasources/databases.py:51`), which compares
   `LOWER(table_schema)` against that set; the Python-side database list lowercases names too.
   Engine-internal schemas outside the set — `__recycle_bin__` and similar — are still not
   excluded, so they can surface in `search_objects`, `describe_datasource` and admin
   autocomplete. Token scopes still gate access, so this is exposure noise rather than a
   privilege leak, but it is the first thing to verify per engine.
   Measured with the real code path (`app/datasources/databases.py:list_upstream_databases`) against
   a server with no user databases, **before the fix**: TiDB v8.5.0 returned `INFORMATION_SCHEMA`,
   `METRICS_SCHEMA`, `PERFORMANCE_SCHEMA` and `test`, because TiDB names its system schemas in
   uppercase and the exclusion was an exact, case-sensitive match; OceanBase CE 4.4.2.1 returned
   `oceanbase` and `test`, neither of which was on the list. MySQL 8.0.46 and MariaDB 11.8.9
   returned an empty list for the same server. A genuinely user-created `test` database is correct
   to show; the engine-internal schemas are the noise. `tests/unit/test_system_schemas.py` now
   asserts the list, the predicate and the filtering; re-measure on your engine before exposing it.
2. **In-session database switching uses `USE \`db\``** (`app/datasources/databases.py:307`,
   called from `app/mcp/pipeline.py`). All MySQL-protocol engines accept `USE`, but tenant
   semantics on OceanBase and logical-database mapping on PolarDB-X need a live check.
3. **Metadata reads use `information_schema` plus `INSTR`, `CONCAT` and `table_comment` /
   `column_comment`** (`app/mcp/introspect.py:172-358`, `app/policy/scope.py:78`,
   `app/mysql/session.py:305-336`). Comment columns are where Chinese-labelled schemas become
   searchable; engines that leave them empty degrade `search_objects` to name matching only.
   On large distributed clusters the metadata queries themselves may be slow.
4. **The export channel uses `aiomysql` directly** — unbuffered `SSCursor`
   (`app/datasources/export_pool.py:169`) and `KILL QUERY` for aborts
   (`app/datasources/export_pool.py:196`). Distributed engines differ on session-id and kill
   semantics; verify that an aborted export actually stops upstream work, otherwise the row and
   time quotas are the only brake.
5. **Error handling keys off MySQL errno** (`1146` table-not-found and friends). An engine that
   returns a different errno produces a coarser message, not a weaker check — the denial happens
   in the policy layer before execution.
6. **Charset assumptions**: pools connect with `charset=utf8mb4`
   (`app/datasources/pool.py:26-32`). Collation differences change ordering and comparison, which
   can change which rows a masked query returns. The server defaults observed in the runs above
   already differ: `utf8mb4_0900_ai_ci` (MySQL 8.0.46), `utf8mb4_uca1400_ai_ci` (MariaDB 11.8.9),
   `utf8mb4_bin` (TiDB v8.5.0) and `utf8mb4_general_ci` (OceanBase CE 4.4.2.1). `utf8mb4_bin`
   compares byte-wise, so case and accent differences change which rows a filter or a mask matches;
   pin the collation in the schema DDL when that matters rather than relying on the server default.
7. **Privilege of the upstream account.** Application-layer policy is not a replacement for
   database grants. On every engine, the datasource account should hold only `SELECT` (plus DML on
   explicitly writable databases). Engines with role or tenant models beyond MySQL's grant
   vocabulary need their own minimum-privilege review.
8. **`VERSION()` is not what it looks like.** BaseWard stores no engine field (`app/db/models.py`
   keeps only host and port), so nothing in the gateway or its UI distinguishes engines — and the
   engines themselves report a MySQL version, not their own: `8.0.11-TiDB-v8.5.0` and
   `5.7.25-OceanBase_CE-v4.4.2.1` were the two strings observed in the runs above. Plan CTE, JSON and
   window-function support against the *reported* MySQL version, and check the engine's own docs.
   OceanBase also requires `user@tenant` as the connection username.
9. **Transaction semantics assume autocommit is off.** The rollback that backs fail-closed audit
   depends on `engine.begin()` actually opening a transaction. Check `SELECT @@autocommit` on a
   connected session — `0` on MySQL, MariaDB and TiDB, `1` on OceanBase — and check isolation too
   (`REPEATABLE-READ` on the first three, `READ-COMMITTED` on OceanBase). See the findings above.
10. **Engine-side statement quotas.** `max_execution_time` exists and reads `0` (unlimited) on
    MySQL 8.0.46, TiDB v8.5.0 and OceanBase CE 4.4.2.1, and is an unknown variable on MariaDB 11.8.9
    (errno `1193`) — so a checklist item written for one engine's name does not transfer. OceanBase
    adds `ob_query_timeout`, which defaults to **10 s** and aborts with error `4012`; that is an
    engine quota, not a BaseWard limit. They are independent of BaseWard's own brake
    (`GATEWAY_MYSQL_EXPORT_MAX_SECONDS`, `app/config.py:66`), so a stream can be cut by the engine
    while the gateway still reports its own quota as the reason.

## How to verify an engine

Verification is running the existing suite against the engine, not writing new assertions:

```bash
cp .env.test.example .env.test      # TEST_DB_HOST/PORT/USER/PASSWORD against the target engine
pytest -q -m integration            # needs CREATE/DROP DATABASE on the test account
```

The suite asserts the four invariants that the product promise rests on: AST default deny,
four-level token scopes, result masking, and fail-closed audit. Engines that pass get moved from
"expected" to "verified" in the table above, and the row is backed by the test run that proves it.

`tests/integration/conftest.py` provisions fixtures with MySQL DDL (`AUTO_INCREMENT`,
`ENGINE=InnoDB`, `utf8mb4 COLLATE`), so an engine that rejects that DDL fails at setup. Treat a
setup failure as a compatibility finding, not a test bug.

Two environment prerequisites are worth checking before blaming the engine:

- **A monotonic clock.** OceanBase's election and timer wheel refuse to run when the clock steps
  backwards: the observer logged `OB_CLOCK_OUT_OF_ORDER` (`-6201`) and the sys tenant could never
  elect a leader, which surfaces as `Server is initializing` forever rather than as a clear error.
  Under WSL2, `systemd-timesyncd` was stepping the clock back every poll (364 `Time jumped
  backwards` kernel messages); disabling it made the same image boot in 50 s. Check `dmesg` for
  backwards jumps before concluding an engine is incompatible.
- **Engine-side statement quotas.** The streaming fixtures seed 200 000 rows in one statement,
  which OceanBase's default 10 s `ob_query_timeout` aborts. Raise it for the run and record the
  value, so a green run is not silently bought by changing the engine's defaults.

The unit suite (`pytest -q -m "not integration"`) needs no database and runs identically on every
engine, so it is not evidence of engine compatibility.

## What is not claimed

- No claim that a non-MySQL engine has passed policy, masking or audit verification until it is
  listed as verified above.
- No claim of feature parity: a MySQL-protocol engine may reject statements BaseWard allows, or
  accept ones it denies. Denials are audited, so the mismatch is visible in the audit trail.
- No cross-dialect support of any kind. Adding PostgreSQL is not a driver swap — dialect literals,
  `information_schema` queries, `USE` semantics and the errno axis all have to be abstracted
  first.
