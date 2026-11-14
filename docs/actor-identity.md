# End-user (actor) identity contract

> Status: P1 (self-asserted). Verified identity is on the roadmap at the end.

## 1. Background: two identities

A credential (`gw_` token) authenticates the **bearer** (principal). When Zhang San
configures the token into an agent platform (OpenClaw / Hermes) and exposes database
queries to other people through a DingTalk bot, the real operator is the **actor**
(e.g. Li Si), not Zhang San. BaseWard separates the two explicitly in protocol and audit:

- `audit_log.user_name`: the credential owner (Zhang San).
- `audit_log.actor_*`: the actor (Li Si) and the trust level.

## 2. Request header contract

MCP (Streamable HTTP) requests carry the following headers; enforced only for `require`
tokens.

| Header | Required | Format |
|---|---|---|
| `X-BaseWard-Actor-Provider` | required for `require` | `^[a-z][a-z0-9-]{0,31}$`, e.g. `dingtalk`; no whitelist |
| `X-BaseWard-Actor-Id` | required for `require` | 1..128 visible ASCII chars (0x21–0x7E), e.g. DingTalk `senderStaffId` |
| `X-BaseWard-Actor-Name` | optional | base64url(UTF-8 nickname), padding optional; truncated to 128 chars after decoding |

### Encoding

HTTP headers are latin-1, so non-ASCII nicknames must be encoded: `base64url(UTF-8)`,
`=` padding optional. Example: nickname "李四" → `X-BaseWard-Actor-Name: 5p2O5Zub`.
Decode failures and control characters are rejected as invalid.

## 3. Token policy (actor_policy)

| Policy | Meaning |
|---|---|
| `ignore` (default) | Personal token. Identity headers are ignored; audit actor columns stay null; behavior unchanged |
| `require` | Agent token. Every call must carry valid identity headers, otherwise 403 + audit |

The policy is chosen when creating a token in the admin console and can be changed on
the token edit page (ignore↔require takes effect immediately; the change is audited).
Existing tokens default to `ignore` after upgrade.

Tokens also have a kind (`token_kind`): `access` (default, queries datasources) or
`exchange` (only mints short-lived credentials at `/api/exchange`, always `require`,
never queries data). See [exchange-credential.md](exchange-credential.md).

## 4. Error codes

| HTTP | body | audit deny_reason | trigger |
|---|---|---|---|
| 403 | `{"error":"actor_required"}` | `ACTOR_REQUIRED` | `require` token with no identity headers at all |
| 403 | `{"error":"actor_invalid"}` | `ACTOR_INVALID` | headers present but incomplete/invalid (missing id, bad provider, broken base64, oversize, control chars) |

Enforcement is uniform at the middleware layer across all 5 tools; the denial audit
`user_name` is the credential owner. Existing 401 (invalid token) and 403 (IP allowlist)
behavior is unchanged.

## 5. Trust level and spoofing boundary

P1 trust is always `asserted`: the gateway records who the platform **claims**, without
signatures or verification.

- The credential holder can claim any identity (including someone else's); `require`
  guarantees an actor is *present*, not *true*.
- A personal (`ignore`) token pasted into an agent platform without integration is
  indistinguishable from the owner's own use — the physical boundary of bearer
  credentials. Governance: issue only `require` tokens for bot scenarios + IP allowlists.

See SECURITY.md L18.

## 6. Authorization semantics (audit-only)

P1 authorization still evaluates the credential owner's `scope`; the actor does **not**
participate in authorization. End users effectively exercise the owner's data permissions.
Scope the agent token to the minimum data the bot should expose to all users. See
SECURITY.md L19.

## 7. Export channel

`require` tokens **cannot issue export credentials** (MCP explicit issuance, truncation
auto-issuance, and admin manual issuance are all rejected with `CREDENTIAL_ISSUE_DENIED`).
The MySQL export protocol has no per-query identity field, so export audit rows have null
actor columns. After a token switches to `require`, already-issued unexpired export
credentials are not retroactively revoked; they expire naturally via TTL.

## 8. Integration patterns

With stateless HTTP, prefer **one lightweight MCP client per end user with the identity
fixed in static headers**, or inject the headers per request in a platform
plugin/middleware hook. Runnable example: [../examples/actor_adapter/](../examples/actor_adapter/).

For agent platforms that should not hold a permanent data-access credential, use an
`exchange` token to mint short-lived, actor-bound credentials per end user instead; the
credential then carries the actor itself. See [exchange-credential.md](exchange-credential.md).

## 9. Audit fields

`audit_log` adds `actor_provider` / `actor_id` / `actor_name` / `actor_trust`, indexed by
`idx_audit_actor (actor_provider, actor_id, created_at)`. The admin audit list supports
filtering by actor (`actor` parameter, fuzzy match on id/name); the detail page shows both
identities and the trust badge.

## 10. Roadmap

- **P2**: registered agents + HMAC-signed assertions (defends against end-user spoofing).
- **P3**: DingTalk callback verification + per-actor authorization (scope intersection).
  Per-user token exchange is implemented as [short-lived credentials](exchange-credential.md);
  the actor it binds is still self-asserted, and authorization still follows the exchange
  token's scope.