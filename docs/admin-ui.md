# Admin console features

[English](admin-ui.md) | [简体中文](admin-ui.zh-CN.md)

- **Dashboard**: datasource count / enabled count, enabled token count, today's calls and denials, the 10 most recent audit records.
- **Datasources**: create / edit / delete, connection test; the connection must succeed before saving. **(v1.8)** Fill in the default database, then "Load databases" and check the accessible databases among the non-system schemas actually visible upstream (5.2).
- **Policy and masking**: datasource master switch plus per-database read/write switches (5.3); manual rules (database / table / column + `mask/hash/null/off`), where the database is a dropdown of that datasource's accessible databases and the table and column inputs support dropdown autocomplete and fuzzy search; notes on the automatic keywords.
- **Tokens**: create (kind: access or exchange; optional expiry; can be narrowed to datasource / database / table / column, plaintext shown only once), inspect expiry and last use, edit the granted scope (no plaintext rotation, effective immediately), disable (effective immediately), delete. Expired tokens and their derived credentials are rejected.
- **Short credentials**: inspect the short credentials minted by exchange tokens (actor, source IP, expiry, last use) and revoke them early (5.4).
- **Audit**: filter by time (entered in Asia/Shanghai), user, datasource, status and keyword, 50 entries per page; the list and the detail view carry a "Database" column (`—` when nothing was recorded), and the detail view carries the full SQL, the error and the denial reason.
- **Settings**: change the password (all old sessions are invalidated immediately), audit retention days, purge expired audit records now, source IP allowlist (5.1).
- **Language**: Chinese and English. The first visit selects a language from the browser's `Accept-Language`; the "中文 / EN" switch in the top right remembers the choice (cookie `gw_admin_lang`), and `?lang=en` switches explicitly. The MCP-side language is decided by `GATEWAY_MCP_LANGUAGE`.

Every logged-in POST is CSRF-checked; the session cookie is `HttpOnly`, `SameSite=Lax`, `Path=/admin`, valid for 8 hours.

## 5.1 Source IP allowlist

Changing the allowlist no longer requires editing environment variables and restarting. The settings page gives each of the two entry points its own text box (one IP or CIDR per line, comma-separated values are also accepted); saving replaces the whole block and takes effect on new connections within 5 seconds:

| Entry point | Where the value comes from | When neither is configured | Behavior outside the allowlist |
| --- | --- | --- | --- |
| MySQL export channel | The TCP peer address, **cannot be forged** | Deny all connections | Disconnected before the handshake, audited as `IP_NOT_ALLOWED` |
| MCP entry point (`/mcp`) | The first address in `X-Forwarded-For`, forgeable when the gateway is exposed directly | No source restriction | 403 `ip_not_allowed`, and **before** token validation |

- **The DB wins**: once a value has been saved on the settings page, that entry point reads only the DB value and the environment variable of the same name no longer applies. Below each text box the currently effective source and entry count are shown (saved on this page / environment variable `GATEWAY_*` / unrestricted or deny all), and `/healthz` exposes the same state through `allowlist_source` (`db` / `env` / `unset`) and `allowlist_entries`.
- **`/admin` is not restricted by the allowlist**: if you get it wrong you can still reach the settings page and change it back; this is the only manual fallback path.
- **An empty list means "deny", not "unrestricted"** (export channel): clearing it and saving = every export connection is denied, while the process keeps running as normal. There is therefore no longer a startup gate that "refuses to start when export is enabled with an empty allowlist", because a crash loop would lock operators out of the very page meant to fix it.
- **Saving validates against the strict contract**: a value with host bits set, such as `192.168.1.5/24`, is rejected with a prompt to rewrite it as the start address (historically the environment variables used a lenient contract that silently expanded the host bits to the whole /24, which quietly widened access). Invalid entries, more than 50 entries, or a single entry longer than 64 characters likewise block the save.
- **Every change is audited**: `event_type=setting_change`, with `deny_reason` recording `old ⇒ new`; re-saving a value exactly identical to the current one writes nothing to the DB and produces no audit record.
- When reading the DB fails, the last successfully loaded value is kept (deny if there is no cache), so "a database hiccup empties the allowlist" cannot happen.

## 5.2 Multi-database datasources and accessible databases (v1.8)

A datasource still corresponds to one host plus one connection account, but it can expose several databases on the same instance:

1. When creating or editing a datasource, fill in the **default database** first and click "Load databases": the gateway lists only databases that actually exist upstream and are not in the system-schema list. That list is `mysql` / `information_schema` / `performance_schema` / `sys` / `metrics_schema` / `oceanbase`, compared case-insensitively (TiDB uses uppercase database names, OceanBase has system schemas of its own); engine-internal databases outside the list still appear, and the per-engine differences are in [compatibility.md](compatibility.md).
2. Check the **accessible databases** and save. The default database must be among the accessible databases; there is no upper limit on the number of accessible databases, and the list supports filtering by name and select all / clear.
3. The edit page shows the saved list and its read/write policy (it does not depend on the upstream being online); refreshing reports "N new upstream / M no longer present", and databases that no longer exist are marked in red and can be removed.

**Semantics**

- **Accessible databases are a gateway-level exposure surface**: an unchecked database is neither returned nor executed by the gateway even if the database account has privileges on it. Metadata, `execute_sql`, the export channel, masking rules and admin-console autocomplete are all invisible (fail-closed).
- **Usable databases = accessible databases ∩ databases authorized for the token**, computed by the same function; `databases[]` in `list_datasources` / `describe_datasource`, the candidate databases on the issue page and the error messages all share this result.
- **Upgrade compatibility**: datasources that already existed before the upgrade and have no database list written yet are treated as "expose the default database only", with the same behavior as v1.7; saving the list once in the admin console moves them into multi-database mode.
- When loading the database list fails, the create form cannot be saved, and there is no entry point for typing database names by hand (so typos are not carried into the configuration).
- Removing a database from the accessible databases takes effect immediately: the token's grant configuration for that database is kept but becomes invalid at once, an export credential whose target database is that database is denied on its next statement, and the database-level policy row is deleted along with the database; re-adding that database defaults to read-only and does not inherit the historical policy.

## 5.3 Per-database read/write policy (v1.8)

- The datasource master switch `is_read_only` is kept as an **overriding forced read-only**: when the master switch is read-only, no database can be written. **Effective write access = master switch writable AND database-level writable**.
- Each accessible database is set to read-only or writable on its own; newly created, newly added or re-added databases default to read-only.
- Read-only databases forbid INSERT/UPDATE/DELETE/REPLACE; when a write statement has several target databases, it is denied if any of them is read-only, and the message names the first read-only target database. Reading from a read-only database (such as `INSERT ... SELECT`) is unaffected: the read/write policy constrains only **write targets**.
- Changing any database or the master switch from read-only to writable requires ticking a second confirmation and takes effect immediately after saving; tightening to read-only needs no confirmation.
- Every actual change writes one `policy_change` audit record, with multi-database changes merged into one, in the form `总开关：只读 ⇒ 可写；report：只读 ⇒ 可写`; re-saving unchanged values writes nothing to the DB and produces no audit record.
- The badge in the datasource list shows "All read-only" or "N writable databases"; when the master switch is read-only, a database-level writable setting is still saved (as pre-configuration) and the page shows "Read-only (master switch)".

## 5.4 Token kinds and short credentials

- **Token kind**: choose "Access (datasource queries)" or "Exchange (mint short credentials only)" when creating a token. The kind cannot be changed afterwards; the list and edit pages show a kind badge.
- **Exchange constraints**: the actor policy is fixed to `require` — choosing ignore at creation is overridden, and changing it back to ignore on the edit page is rejected with a message. Exchange tokens cannot query data or issue export credentials; their scope is the ceiling for every short credential they mint.
- **Short credentials page**: lists the credential prefix, parent exchange token, actor (id / nickname / provider), source IP, expiry, last use and status (active / expired / revoked). Early revocation takes effect immediately and is audited.
- **Automatic supersede and cleanup**: exchanging again for the same exchange token + actor immediately revokes the previous active credential; expired rows are purged daily at 03:00 alongside audit cleanup.
- See [credential exchange](exchange-credential.md) for the contract and endpoint details.
