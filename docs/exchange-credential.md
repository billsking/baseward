# Short-lived credential exchange (agent platforms / OpenClaw)

[English](exchange-credential.md) | [简体中文](exchange-credential.zh-CN.md)

> Status: implemented. The actor trust level is still `asserted` (self-reported); see the [actor identity contract](actor-identity.md).

## 1. Two token kinds

| Kind | `token_kind` | Allowed | Forbidden |
|---|---|---|---|
| Access token | `access` (default) | Query datasources within its scope; `actor_policy` may be ignore/require | — |
| Exchange token | `exchange` | Only `POST /api/exchange` to mint short credentials | No data queries, no export credential issuance, no direct MCP tool calls |

An exchange token's actor policy is **fixed to `require`**: choosing ignore at creation is overridden, and it cannot be downgraded afterwards (both the admin UI and the service reject it). Its scope is the **ceiling** for every short credential it mints.

The short credential (`sc_` prefix) is what actually queries data over MCP: bound to one actor, bound to one source IP, carrying a snapshot of the exchange token's scope, and expiring automatically.

## 2. Why split them

Separating "can query data" from "can mint credentials" means the agent host (OpenClaw + DingTalk bot) only holds an exchange token long-term, while each end user gets a minute-scale credential bound to their IP and identity. If the host is compromised, the exposure drops from "N permanent tokens" to "one exchange token plus a few in-flight short credentials" — and the exchange token itself cannot read any data.

## 3. Exchange endpoint

```
POST /api/exchange
Authorization: Bearer gw_<exchange token>
X-BaseWard-Actor-Provider: dingtalk
X-BaseWard-Actor-Id: manager1234
X-BaseWard-Actor-Name: 5p2O5Zub        # optional, base64url(UTF-8)
Content-Type: application/json

{"ttl_minutes": 15}                     # optional, defaults to GATEWAY_EXCHANGE_TTL_MINUTES
```

The identity headers follow the same contract as [actor-identity.md](actor-identity.md#2-request-header-contract) (provider required, matching `^[a-z][a-z0-9-]{0,31}$`; id is 1..128 visible ASCII characters; nickname base64url). The source IP allowlist is shared with the MCP edge.

Success `200`:

```json
{
  "credential": "sc_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "expires_at": "2026-11-13T12:34:56.789Z",
  "ttl_seconds": 900,
  "actor": {"provider": "dingtalk", "id": "manager1234", "name": "李四"}
}
```

The plaintext appears only in this response; the store keeps a SHA-256 digest.

### Error codes

| HTTP | body `error` | Audit deny_reason | Trigger |
|---|---|---|---|
| 403 | `ip_not_allowed` | `IP_NOT_ALLOWED` | Source outside the MCP allowlist |
| 401 | `missing_token` / `invalid_token` | `AUTH_INVALID` | Missing/malformed Bearer, unknown or disabled token |
| 403 | `exchange_token_required` | `EXCHANGE_TOKEN_REQUIRED` | An access token or short credential was used to exchange |
| 403 | `actor_required` | `ACTOR_REQUIRED` | No identity headers at all |
| 403 | `actor_invalid` | `ACTOR_INVALID` | Incomplete or malformed identity headers |
| 400 | `invalid_request` | `EXCHANGE_INVALID` | `ttl_minutes` out of range, or source IP undeterminable |
| 503 | `auth_unavailable` | — | Management store unavailable |

## 4. Using a short credential

```
POST /mcp/
Authorization: Bearer sc_<short credential>
```

- **Identity headers are optional**: the actor was bound at exchange time and the gateway carries it into the audit, so dual identity is still recorded.
- **Headers must agree**: if `X-BaseWard-Actor-*` are sent, their provider+id must match the bound values, otherwise `403 actor_mismatch` (audit `ACTOR_MISMATCH`). The bound nickname is authoritative.
- Authorization uses the scope snapshot inside the credential. Changing the exchange token's scope afterwards does **not** widen already-issued credentials; they converge as they expire.

Invalid credentials always return `401 invalid_token`; the audit distinguishes the cause:

| Audit deny_reason | Meaning |
|---|---|
| `SHORT_CREDENTIAL_EXPIRED` | Past its expiry |
| `SHORT_CREDENTIAL_REVOKED` | Revoked (including superseded by a newer credential) |
| `SHORT_CREDENTIAL_IP_MISMATCH` | Source IP differs from the exchange caller |
| `PARENT_TOKEN_DISABLED` | The parent exchange token was disabled or deleted |

Clients (the bridge plugin) should re-exchange on 401 rather than retry the same credential.

## 5. Lifecycle

- **TTL**: 15 minutes by default, 60 minutes maximum, both configurable (§8). A request may ask for a shorter `ttl_minutes`.
- **Supersede**: exchanging again for the same exchange token + actor immediately revokes the previous active credential (audit `exchange_supersede`), so each actor has at most one active credential at a time.
- **Revoke**: the admin "Short credentials" page revokes early, effective immediately.
- **Cleanup**: expired rows are deleted daily at 03:00 (Asia/Shanghai) alongside audit cleanup, writing `exchange_expire_cleanup`.
- **Cascade**: deleting an exchange token deletes its short credentials.

## 6. Audit

Every call made with a short credential records both identities:

- `user_name` / `token_id` / `token_prefix`: the **exchange token owner** (the bot operator).
- `actor_provider` / `actor_id` / `actor_name` / `actor_trust`: the end user; `actor_trust` is always `asserted`.
- `credential_account`: the short credential prefix (`sc_…`), to locate the exact credential.

Exchange-chain events: `credential_lifecycle` (`exchange_issue` / `exchange_supersede` / `exchange_revoke` / `exchange_expire_cleanup`) and `exchange` (rejected exchanges). The admin audit list can filter by actor.

## 7. Trust boundary

- The actor is still **self-asserted**: the exchange token holder may claim any end user. Exchange adds no signature or DingTalk verification, and `actor_trust` is unchanged.
- Authorization still follows the **exchange token's scope**, not per-actor grants. Every end user exercises the same data permissions, so narrow the exchange token to the smallest dataset the bot should expose to everyone.
- Short credentials are bound to the source IP: exchange and use must come from the same egress IP. Multiple egresses or NAT changes on the agent platform cause `SHORT_CREDENTIAL_IP_MISMATCH`.
- The exchange token is a **high-value credential**: it can mint credentials for any actor. Hand it only to trusted agent platforms, pair it with the IP allowlist, and monitor `exchange_issue` frequency in the admin console.

## 8. Configuration

| Variable | Default | Description |
|---|---|---|
| `GATEWAY_EXCHANGE_TTL_MINUTES` | `15` | Default lifetime in minutes (1-1440) |
| `GATEWAY_EXCHANGE_MAX_TTL_MINUTES` | `60` | Upper bound a request may ask for (minutes, 1-1440); must not be below the default |

## 9. OpenClaw integration

The official DingTalk connector hands `senderStaffId` (falling back to `senderId`) to the host as `SenderId`; OpenClaw's `api.registerMcpServerConnectionResolver` passes it to plugins as the trusted `requesterSenderId`. The bridge plugin then:

1. calls `/api/exchange` with the exchange token plus that actor's identity headers;
2. returns the resulting `sc_` credential as `Authorization`, binding it to that requester's MCP connection;
3. caches per actor and re-exchanges on expiry or a 401.

The model never sees a credential and cannot choose which one is used — prompt injection cannot change the actor. A runnable example lives in [../examples/actor_adapter/](../examples/actor_adapter/); the standalone plugin package in [../integrations/openclaw-baseward/](../integrations/openclaw-baseward/).

## 10. Roadmap

- Implemented: exchange tokens / short credentials / IP and actor binding / TTL / supersede and revoke / dual-identity audit.
- Not implemented: registered-agent HMAC assertions (P2), DingTalk callback verification and per-actor authorization (P3).