# BaseWard Bridge for OpenClaw

[English](README.md) | [简体中文](README.zh-CN.md)

An OpenClaw plugin that binds a BaseWard MCP server to the **trusted message requester**: for every
DingTalk user it exchanges a short-lived, actor-bound credential and hands it to the host as the
MCP connection's `Authorization`. The model never sees a credential and cannot choose whose
identity is used.

Verified against OpenClaw `2026.8.1` and `2026.11.3` (`api.registerMcpServerConnectionResolver`).

## How it works

```
DingTalk user → dingtalk-connector → OpenClaw run (SenderId = senderStaffId)
                                          │
                       resolver(ctx.requesterSenderId)   ← host-trusted, not model input
                                          │
                       POST /api/exchange  (Bearer gw_<exchange token> + actor headers)
                                          │
                       sc_<short credential>  → Authorization for this requester's MCP transport
```

- The exchange token (`token_kind=exchange`) **cannot query data**; it only mints credentials.
- Each short credential is bound to one actor and the caller's source IP, inherits the exchange
  token's scope, and expires (default 15 min).
- Credentials are cached per actor and re-exchanged `refreshSkewSeconds` before expiry; concurrent
  requests for the same actor share one exchange (single-flight).
- Fail closed: a run without a trusted sender id, or from another channel, gets **no** BaseWard
  connection. An exchange failure omits the server for that run and logs a warning.

## Prerequisites

1. BaseWard running, with an **exchange** token created in the admin console (Token kind =
   Exchange) and its scope narrowed to the data the bot may expose to everyone.
2. The agent host's egress IP in BaseWard's MCP allowlist — exchange and MCP use must come from the
   same IP, because short credentials are IP-bound.
3. OpenClaw ≥ 2026.8.1 with the official DingTalk connector installed and working.

## Install

```bash
# from a checkout of this repository (local path; --link keeps it editable)
openclaw plugins install ./integrations/openclaw-baseward --link
```

The entry point is TypeScript source, which OpenClaw loads through its runtime transformer for
local-path installs. To distribute it as a tarball, build/pack it first with `openclaw plugins
pack --root ./integrations/openclaw-baseward` and install the result via `npm-pack:`.

## Configure

Declare the MCP server (its tool surface stays static; the resolver only binds the transport):

```jsonc
// openclaw.json
{
  "mcp": {
    "servers": {
      "baseward": {
        "url": "http://127.0.0.1:8080/mcp/",   // overridden per requester by the resolver
        "transport": "streamable-http"
      }
    }
  },
  "plugins": {
    "entries": {
      "baseward-bridge": {
        "config": {
          "gatewayUrl": "http://127.0.0.1:8080",
          "exchangeToken": "gw_REPLACE_WITH_EXCHANGE_TOKEN",
          "serverName": "baseward",
          "provider": "dingtalk",
          "channel": "dingtalk-connector"
        }
      }
    }
  }
}
```

| Key | Default | Meaning |
|---|---|---|
| `gatewayUrl` | required | BaseWard origin |
| `exchangeToken` | required | `gw_` token of kind `exchange` |
| `serverName` | `baseward` | must match the declared MCP server name |
| `mcpPath` | `/mcp/` | MCP mount path |
| `channel` | `dingtalk-connector` | only resolve for this channel |
| `provider` | `dingtalk` | `X-BaseWard-Actor-Provider` |
| `ttlMinutes` | gateway default | optional requested lifetime (gateway caps it) |
| `refreshSkewSeconds` | `60` | re-exchange this long before expiry |

Restart the gateway after changing the config.

## Verify

Send a DingTalk message that triggers a query, then check BaseWard's admin audit: the row should
show `user_name` = the exchange token owner, `actor_id` = the DingTalk `senderStaffId`,
`actor_trust` = `asserted`, and `credential_account` = an `sc_` prefix. The admin "Short
credentials" page lists the live credential for that actor.

## Limitations

- **Actor is self-asserted.** Exchange adds no signature; a holder of the exchange token can claim
  any actor. See BaseWard `SECURITY.md` L18/L20.
- **Authorization is not per-actor.** Every user exercises the exchange token's scope.
- **No nickname.** The resolver context carries only the sender id, so `X-BaseWard-Actor-Name` is
  not sent; audit shows the id.
- **Revocation latency.** The host revalidates resolved connections at most every 5 minutes, so a
  revoked credential can stay in use until the next resolve. Expiry is handled locally by the skew.
- **No sender, no server.** Cron, subagent and heartbeat runs have no trusted requester, so the
  BaseWard server is absent there by design.

## Development

```bash
npm install
npm test          # vitest, 20 cases
npm run type-check
```

Runtime dependencies: none (Node ≥ 18 `fetch`). `openclaw` is an optional peer dependency; the
plugin declares only the structural types it uses in `src/types.ts`.