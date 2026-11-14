# Security policy

[English](SECURITY.md) | [简体中文](SECURITY.zh-CN.md)

This document describes BaseWard's security model, the controls implemented today, its **known limitations**, and how to report a vulnerability.

If you are about to point the gateway at real data, read section 4 "Known limitations" and section 5 "Secure deployment baseline" first. Several of these limitations are design trade-offs, not defects waiting to be fixed; treating them as safe by default will cause problems.

## 1. Supported versions

| Version | Security updates |
| --- | --- |
| Latest minor release (currently `v1.8.x`) | Supported |
| Earlier minor releases | No separate backports; upgrade |

The gateway is a stateful service (management database + master key). Before upgrading, read the upgrade and migration notes in the README and back up the management database.

## 2. Reporting a vulnerability

- Report privately through the repository's **Security → Advisories → Report a vulnerability**; do not open a public issue and do not post exploit details in a Discussion.
- If GitHub private reporting is unavailable, email **billsking@qq.com** with the subject prefix `[BaseWard security]`; the same discretion applies — no public issues, no exploit details in Discussions.
- Please include: affected versions, reproduction steps, impact (whether reads/writes can cross datasources or tokens, whether masking or audit can be bypassed), and the deployment shape you used to verify it.
- Maintainer targets: acknowledge receipt within 5 business days, and provide a fix or mitigation within 30 days. Once the fix ships you are credited in the advisory (unless you ask to remain anonymous).

## 3. Threat model

### 3.1 Protected assets

- Upstream datasource passwords (encrypted at rest)
- MCP tokens and their four-level authorization scopes
- Export credentials (short-lived MySQL accounts)
- Audit logs (including full SQL literals and request origin)
- Admin sessions

### 3.2 Trust boundaries

| Boundary | Notes |
| --- | --- |
| AI client → `POST /mcp` | Bearer token authentication; the caller is treated as "possibly manipulated, possibly compromised" |
| Gateway → upstream database | Uses the datasource's own account; the gateway is not a replacement for database privileges |
| Gateway → management database | Stores encrypted credentials, token hashes and audit records |
| Browser → `/admin` | Admin session + CSRF |
| Export client → `:3307` | MySQL protocol + short-lived credentials; source IP allowlist |

### 3.3 Assumed attackers

- A client holding a valid but restricted token, trying to read unauthorized databases/tables/columns;
- An AI client controlled by prompt injection or a malicious plugin, trying to run writes, DDL or bulk exports;
- A host on the same network segment that can reach the gateway's ports but holds no token;
- A client that obtained an export credential and tries to widen the usable databases or extend its validity.

### 3.4 Explicitly out of scope

- The upstream database, management database, host machine or master key itself is already compromised;
- A DBA with database superuser privileges bypassing the gateway to read data directly;
- An admin deliberately exporting or leaking data (audit only leaves a trace, it does not prevent it);
- Gateway policy being bypassed because the upstream account itself is over-privileged;
- Side channels, supply-chain poisoning, physical access.

## 4. Known limitations (with code evidence)

The entries below list, one by one, the capabilities the **current version genuinely does not have**, the code evidence, and workable mitigations. The basis for each judgement is the code itself, not a documentation guarantee.

### L1. The gateway itself does not provide TLS

- **Status**: MCP and the HTTP admin console are served by uvicorn over plaintext HTTP; the repository has no certificate or TLS listener configuration. The export channel `:3307` is likewise not configured for TLS, and the client connection example explicitly uses `ssl_disabled=True` (`app/mcp/pipeline.py:331`). Connections from the gateway to the upstream database do not enable TLS either (`app/datasources/pool.py:24` and `app/datasources/export_pool.py:121` both pass no `ssl` parameter).
- **Impact**: Tokens, SQL, query results and admin session cookies cross the network in plaintext; anything on the same path can eavesdrop or tamper as a man in the middle.
- **Mitigation**: Deploy only inside a controlled private network; terminate TLS in a reverse proxy in front of the gateway and set `GATEWAY_COOKIE_SECURE=true`; bind the export port to a private interface only and restrict sources.
- **Not currently planned**: built-in TLS termination.

### L2. The master key cannot be rotated

- **Status**: `GATEWAY_MASTER_KEY` encrypts datasource passwords with AES-256-GCM and derives masking and session subkeys through HKDF-SHA256 (`app/security/crypto.py`, `app/services.py:53`). There is no code path for re-encrypting or switching the master key; the README's "Known limitations" also states that rotation is not supported in the first phase.
- **Impact**: Once the master key leaks, every stored datasource password must be treated as leaked; recovery means resetting the master key and re-entering every datasource, which also invalidates all sessions and export credentials. If the master key is lost, the stored passwords are unrecoverable.
- **Mitigation**: Keep it offline, separate from database backups, and distribute it to the smallest possible number of people; periodically verify that the backup can be restored.
- **Fix direction**: introduce a key version number and lazy re-encryption (not implemented yet).

### L3. Application-layer policy is not a replacement for database least privilege

- **Status**: The SQL policy engine is AST-based default deny (`app/policy/engine.py`), but the upstream database is still accessed with the datasource's own account.
- **Impact**: If the datasource account holds `DROP`/`GRANT`/cross-database privileges, any policy bypass (a parser defect, syntax not covered) lands directly on those real privileges.
- **Mitigation**: Use an account with only `SELECT/SHOW` for read-only sources; grant DML on the necessary tables only for writable sources; do not connect with a high-privilege account.

### L4. Masking has no full column lineage

- **Status**: Masking uses a conservative AST-based mapping plus automatic keywords (`app/masking/`); it does not track full column provenance across multiple layers of subqueries/CTEs. When parsing fails, it falls back to the strictest rule or the automatic keywords.
- **Impact**: Extremely nested SQL may be over-masked (reduced usability) rather than under-masked; but custom rules that depend on column aliases/expressions can still behave differently than expected.
- **Mitigation**: For sensitive tables, prefer the double safety net of "automatic keywords + manual rules"; regression-test with real SQL before going live.

### L5. No row-level permissions

- **Status**: Token authorization is a four-level allowlist over datasource/database/table/column (`app/tokens/`, `app/policy/scope.py`); there is no row-level filtering.
- **Impact**: Table access means access to every row in that table; isolation by tenant or region cannot be expressed at the gateway layer.
- **Mitigation**: Create views or separate datasource accounts for different groups; implement row-level security on the upstream database where needed.

### L6. One enabled token per user, with no expiry

- **Status**: Tokens are stored as SHA-256 hashes and disabling one takes effect immediately; there is no TTL and no automatic rotation.
- **Impact**: A leaked token stays valid until it is disabled; you cannot issue several tokens for different purposes to the same user in parallel.
- **Mitigation**: Manage tokens like passwords; disable and recreate immediately on suspected leakage; never write them into the code repository or logs.

### L7. No built-in rate limiting or brute-force protection

- **Status**: Admin console login, MCP requests and export connections have no rate limiting and no lockout after failed attempts (there is no rate-limiting implementation in the code).
- **Impact**: Authentication endpoints can be brute-forced online; a compromised token can query at high frequency and put load on the database.
- **Mitigation**: Apply IP rate limiting and login protection at the reverse proxy/gateway layer; use the audit fields `duration_ms` and `row_count` for anomaly alerting.

### L8. No security response headers

- **Status**: Response headers such as `Content-Security-Policy`, `X-Frame-Options` and `Strict-Transport-Security` are not set.
- **Impact**: The admin console lacks browser-side defense in depth (a larger surface for clickjacking and inline script injection).
- **Mitigation**: Inject security response headers centrally at the reverse proxy; expose the admin console only to trusted networks.

### L9. The export password is shown only once

- **Status**: The export credential password is 192 random bits and appears only once, in the issuance response; the server keeps only the hexadecimal `SHA1(SHA1(password))` used by `mysql_native_password` (`app/security/crypto.py:101`, `app/credentials/service.py:400`).
- **Impact**: A lost password cannot be recovered; the only option is to revoke and reissue. Sessions already connected continue until the credential expires or is revoked.
- **Mitigation**: Write it into the caller's secret management immediately after issuance; do not let connection strings end up in logs or tickets.

### L10. Export credentials bind to an exact source IP

- **Status**: A credential can be bound to a source IP, and the comparison is an exact match (`app/credentials/`).
- **Impact**: Authentication fails when the client goes through NAT or its egress IP changes.
- **Mitigation**: Bind the client's fixed egress IP, or leave the binding empty and rely on a network-layer allowlist.

### L11. Multiple databases are limited to one MySQL instance

- **Status**: One datasource maps to one host plus one set of credentials; multiple databases are expressed through the accessible-databases list (`app/datasources/databases.py`).
- **Impact**: Cross-instance queries have to be aggregated outside the gateway, or you create several datasources and authorize them separately.
- **Mitigation**: Split datasources per instance; a token can be authorized for several datasources at once.

### L12. Query execution itself has no hard row-count/time limit

- **Status**: By current design, SQL execution enforces no row-count or execution-time limit; the MCP channel truncates at result row/byte caps and hands off to an export credential (`GATEWAY_MCP_RESULT_MAX_ROWS`, `GATEWAY_MCP_RESULT_MAX_BYTES`), and the export channel has row, time and concurrency quotas.
- **Impact**: Large result sets and slow queries hold connections, memory and the event loop; several concurrent slow queries can slow down the whole gateway.
- **Mitigation**: Set `max_execution_time`/`max_statement_time` on the upstream database; monitor with the audit fields; limit concurrency separately for the export channel.

### L13. Audit records can be purged by an admin

- **Status**: Audit records are kept for 180 days by default and purged automatically every day; an admin can also purge them manually in the admin console.
- **Impact**: Anyone with admin privileges can delete traces, including the records of their own actions.
- **Mitigation**: Ship the audit store's logs to separate append-only storage; limit the number of admin accounts.

### L14. No multi-factor authentication for the admin console

- **Status**: Admins log in with username + password; sessions are fixed at 8 hours, and changing the password invalidates old sessions.
- **Mitigation**: Expose the admin console only on an internal network or behind a jump host; use a separate strong password; add a second factor through an SSO reverse proxy.

### L15. Single process, no built-in high availability

- **Status**: MCP, the admin console and the export channel share one uvicorn process; connection pools are isolated per datasource.
- **Impact**: A process crash or a misconfigured master key affects all channels at once; multi-replica deployments need extra thought about the export port and audit writes.
- **Mitigation**: Let the orchestration system handle restarts and health checks; back up the management database before upgrading.

### L16. Policy behavior across the MySQL protocol family has been measured on only some engines

- **Status**: SQL parsing is pinned to the MySQL dialect (`app/policy/engine.py:134`); the system-schema exclusion list is a single lowercase set `mysql` / `information_schema` / `performance_schema` / `sys` / `metrics_schema` / `oceanbase` (`app/datasources/databases.py:34`); every information_schema read goes through `exclude_system_schemas()` for a case-insensitive comparison (`app/datasources/databases.py:51`); metadata queries depend on `information_schema` and `table_comment` / `column_comment`. Default deny is **fail-closed**: unparseable SQL (`app/policy/engine.py:135-137`) and statements that degrade into a raw command (`exp.Command` is in `_FORBIDDEN_NODES`, `app/policy/engine.py:33`) are always denied. The integration suite has passed in full on MySQL 8.0.46, MariaDB 11.8.9, TiDB v8.5.0 and OceanBase CE 4.4.2.1 (243/243 each; the per-engine environments and commands are in the verification record of [docs/compatibility.md](docs/compatibility.md)). OceanBase's two deployment prerequisites are in L17 below; MySQL 5.7 / 8.4, Percona, Aurora/RDS, PolarDB, TDSQL, GoldenDB and GaussDB(for MySQL) are still not measured.
- **Impact**: On the engines that have been measured, the risk runs toward **narrowed functionality** (vendor-specific statements denied) and **metadata noise**, not a policy bypass. One of those deltas has been evidenced and fixed: before the fix, `list_upstream_databases()` on TiDB v8.5.0 returned uppercase `INFORMATION_SCHEMA`, `PERFORMANCE_SCHEMA` and `METRICS_SCHEMA` (the list matched case exactly at the time, so they were not filtered out), and OceanBase CE 4.4.2.1 returned `oceanbase`; those databases show up in object search, `describe_datasource` and admin console autocomplete, but token scopes still apply, so this is exposure noise rather than a privilege leak. The list now includes those two engines' own system schemas and compares case-insensitively; regression coverage is `tests/unit/test_system_schemas.py`; **the four-engine integration suite has not been re-run since that fix**, so "the noise is gone on real engines" is not yet a measured conclusion. Engine-internal schemas outside the list (such as `__recycle_bin__`) are still not excluded. The per-engine deltas on engines that have not been measured must be evidenced against the compatibility document; unverified does not mean verified.
- **Mitigation**: Before connecting a non-reference engine, point the integration suite at the target engine and run it once (needs `CREATE/DROP DATABASE` privileges), then check every item on the checklist in [docs/compatibility.md](docs/compatibility.md); once it passes, record it in that document's status table. The system-schema noise fix has landed (case-insensitive comparison plus the engines' own system schemas, with no re-run on the engines for evidence); before connecting a new engine you should still grant least privilege according to what the datasource actually needs. Upstream accounts are still granted least privilege.

### L17. OceanBase ignores the autocommit-off flag in the handshake (fixed in the connection pool)

- **Status**: Writes run inside `async with engine.begin()` (`app/mcp/pipeline.py:258-263`); when the audit write fails, the exception escaping that block is what rolls back the upstream write. Connected sessions on MySQL 8.0.46, MariaDB 11.8.9 and TiDB v8.5.0 report `@@autocommit = 0` (the client requests autocommit-off during the handshake), so the rollback works; OceanBase CE 4.4.2.1 was measured at `@@autocommit = 1`: it does not accept that flag in the handshake, each statement commits as soon as it finishes, and `ROLLBACK` becomes a no-op. In the before-fix comparison experiment running the same `UPDATE` plus a forced rollback, MySQL and TiDB still held the original value while OceanBase already held the new one.
- **Impact**: Before the fix, if the audit result update failed on OceanBase, the caller received `AUDIT_UNAVAILABLE` and the audit row status was `error`, **but the write had already landed in the upstream database**. The policy decision itself was not bypassed (the statement still went through parsing, scoping and masking); what failed was the guarantee that "if audit is unavailable, the write does not take effect". After the fix, that guarantee holds again on OceanBase.
- **Mitigation**: The connection pool runs `SET autocommit=0` at connect time (the `init_command` in `app/datasources/pool.py`), which is a no-op on the other three engines. Regression coverage is `tests/integration/test_pipeline_failclosed.py::test_pool_session_has_autocommit_off` and `tests/unit/test_datasource_pool.py`; after the fix, the full integration suite was re-run on OceanBase with 243 passed. The second prerequisite is OceanBase's default 10s `ob_query_timeout`: large result-set reads are cut off by it, so raise it per datasource when connecting (it was 600s for the measured run; see [docs/compatibility.md](docs/compatibility.md)).

### L18. End-user (actor) identity is self-asserted, not verified

- **Status**: P1 actor identity comes from the `X-BaseWard-Actor-*` request headers parsed at the MCP edge (`app/security/mcp_auth.py`); the trust level is fixed to `asserted` (`app/security/actor.py:23,31`). There is no signature and no DingTalk callback verification. Short-lived credential exchange (`app/exchange/`) binds an asserted actor to a credential, but binding is not verification.
- **Impact**: The credential holder (for example the bot operator) can assert any end-user identity, including someone else's; the audit records who the platform *claimed*, not a cryptographically verified actor. A `require` token guarantees an actor is *present*, not that it is *true*. A personal (`ignore`) token pasted into a bot platform without integration is indistinguishable from the owner's own use.
- **Mitigation**: Issue `require` tokens only to trusted intermediary platforms, and treat `asserted` audit rows as accountable-to-the-platform rather than non-repudiable. Prefer `exchange` tokens for bot platforms so the host holds no permanent data-access credential (see L20). Verified identity (registered-agent HMAC or DingTalk callback signature) is planned for P2/P3 and is not implemented here. See [docs/actor-identity.md](docs/actor-identity.md) and [docs/exchange-credential.md](docs/exchange-credential.md).

### L19. Actor identity is audit-only; authorization still uses the credential owner's scope

- **Status**: Every policy decision evaluates `principal.scope` (`app/mcp/pipeline.py:117`); the actor is written to the audit but never participates in authorization, and `Principal` carries no per-actor permissions (`app/security/principal.py`).
- **Impact**: An end user behind a bot effectively exercises the credential owner's data permissions. The audit can answer "who queried", but cannot prevent "Li Si querying data that Zhang San may access but Li Si should not".
- **Mitigation**: Scope the credential owner's token to the minimum data the bot should expose to all of its users. Per-actor authorization (actor-to-user mapping with scope intersection) is a P3 item and is not implemented here. Short-lived credentials inherit the exchange token's scope snapshot, so they narrow lifetime and exposure but do not differentiate permissions per actor.

### L20. An exchange token can mint credentials for any actor

- **Status**: `POST /api/exchange` accepts any valid actor assertion from the holder of an `exchange` token and mints a short credential bound to that actor (`app/exchange/routes.py`, `app/exchange/service.py`). The exchange token itself is rejected at the MCP edge (`403 exchange_only`) and cannot issue export credentials.
- **Impact**: Whoever holds the exchange token can impersonate any end user in the audit and obtain that user's short credential. The data reachable is still capped by the exchange token's scope, so this is an audit-integrity and credential-proliferation risk rather than a privilege escalation beyond that scope.
- **Mitigation**: Each short credential is bound to the exchange caller's source IP and to one actor, inherits a scope snapshot, expires (default 15 minutes, max 60), and is superseded when the same actor exchanges again; expired rows are purged daily. Keep the exchange token on the trusted agent host only, enforce the MCP IP allowlist, and monitor `exchange_issue` volume in the admin audit. Revoking or disabling the exchange token immediately invalidates its outstanding short credentials (`PARENT_TOKEN_DISABLED`).

## 5. Secure deployment baseline

Minimum requirements:

1. **Network**: Keep the gateway, management database and upstream databases in a controlled private network; do not expose the admin console to the public internet; bind the export port to a private interface only (`GATEWAY_EXPORT_BIND`) and do not publish it as `0.0.0.0:3307`.
2. **TLS**: TLS termination (a reverse proxy) in front of the gateway is mandatory; set `GATEWAY_COOKIE_SECURE=true`.
3. **Credentials**: Inject `GATEWAY_MASTER_KEY` and `GATEWAY_ADMIN_PASSWORD` from secret management; do not use default values and do not write them into images or the repository; keep an offline backup of the master key.
4. **Database accounts**: Grant the upstream account least privilege; grant the management database account access to the `db_gateway` schema only.
5. **Source restrictions**: Configure source IP allowlists for MCP and export; when the gateway faces the internet directly, do not rely on `X-Forwarded-For` unless you have confirmed that only a controlled reverse proxy can set that header.
6. **Audit**: Treat the audit store as sensitive data, restrict access and encrypt backups; set the retention period to your compliance requirements.
7. **Upgrades**: Back up the management database before upgrading; after upgrading, check the migration version and `healthz`.

## 6. Security invariants

Breaking any of the following behaviors is treated as a security vulnerability:

- Data from unauthorized databases/tables/columns must never appear in MCP responses or export results;
- `SELECT *` must be rewritten to the set of columns the token allows;
- SQL that cannot be parsed, or whose ownership cannot be determined, must be denied rather than allowed through;
- When the audit write fails, the query must be blocked (`AUDIT_UNAVAILABLE`); "execute first, backfill the audit later" is not allowed;
- Plaintext tokens, datasource passwords and export passwords must never appear in application logs;
- Once an export credential expires or is revoked, new connections must be rejected immediately.
- An `exchange` token must never reach a datasource query or an export issuance;
- Once a short credential expires, is revoked, is used from a different source IP, or loses its parent token, requests must be rejected immediately.

## 7. Testing and scanning status

- The repository contains 794 test cases: 537 unit tests (no MySQL required) and 257 integration tests (a dedicated test database required); they cover the policy engine, four-level authorization, masking, fail-closed audit, export credentials, short-credential exchange and the protocol patches.
- There is currently **no** SAST, dependency vulnerability scanning, fuzzing or penetration test report configured. CI only runs tests; that does not amount to a completed security audit.
- The export channel depends on `mysql-mimic==3.0.5` and carries two protocol-layer patches; the related behavior has dedicated tests (see `tests/integration/test_export_*.py`).

## 8. Disclosure policy

- Coordinated disclosure: details are published after the fixed version ships; the reporter gets 90 days by default, extendable by agreement between both sides.
- If a vulnerability is already exploited in the wild, or its impact covers all deployments (an authentication bypass, for example), the maintainers will release mitigations as soon as possible and announce them ahead of time.
- Do not publish PoCs, scan results or affected customer information before the fix is released.