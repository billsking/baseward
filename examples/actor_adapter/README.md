# Actor identity reference adapter (example)

A runnable example of calling BaseWard's MCP tools while asserting the **real end user**
behind a bot/agent platform (OpenClaw, Hermes, a DingTalk bot, …).

> **Example only.** P1 actor identity is *self-asserted*: the gateway records who the
> platform claims, without cryptographic verification. See the trust boundary below and
> [../../docs/actor-identity.md](../../docs/actor-identity.md).

## Run

```bash
python examples/actor_adapter/demo.py \
  --base-url http://127.0.0.1:8080/mcp/ \
  --token gw_xxx \
  --actor-id manager1234 \
  --actor-name 李四 \
  --datasource prod \
  --sql "SELECT 1"
```

`--token` defaults to the `GATEWAY_TOKEN` environment variable. Without `--datasource`
the example only calls `list_datasources`. The token must be a **require** (agent) token
for the actor headers to be enforced; personal tokens ignore them.

## The integration pattern

The gateway's MCP transport is stateless HTTP, so each request carries its own headers.
The key pattern: **one lightweight MCP client per end user, with that user's identity
fixed in static headers** — no dynamic-header support is required from the client.

```python
from demo import actor_headers
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

# Map the channel identity (e.g. DingTalk senderStaffId) to the contract headers.
headers = {
    "Authorization": f"Bearer {agent_token}",
    **actor_headers("dingtalk", message["senderStaffId"], message.get("senderNick")),
}
async with streamablehttp_client(url, headers=headers) as (read, write, _):
    async with ClientSession(read, write) as session:
        await session.initialize()
        await session.call_tool("execute_sql", {"datasource": "prod", "sql": sql})
```

For platforms with a plugin/middleware hook (OpenClaw plugins, Hermes tool wrappers),
inject the same headers per request from the channel context instead of creating a
client per user.

## Trust boundary (read before production use)

- The gateway trusts the **credential holder** to assert identities honestly. A holder
  can claim to be anyone; `require` only guarantees an actor is present, not true.
- Authorization still uses the credential owner's scope (audit-only in P1): every end
  user effectively exercises the owner's data permissions. Scope the agent token to the
  minimum data the bot should expose.
- Verified identity (registered-agent signatures, DingTalk callback verification,
  per-user token exchange) is planned for P2/P3 and is not part of this example.
## Short-lived credential exchange (recommended for bots)

`exchange_demo.py` shows the full chain: exchange an **exchange-kind** token for a short-lived
credential, then query with it. The exchange token cannot query data itself, so the host never
holds a permanent data-access credential.

```bash
python examples/actor_adapter/exchange_demo.py \
  --base-url http://127.0.0.1:8080/mcp/ \
  --exchange-token gw_xxx \
  --actor-id manager1234 \
  --actor-name 李四 \
  --datasource prod \
  --sql "SELECT 1"
```

`--exchange-token` defaults to `GATEWAY_EXCHANGE_TOKEN`; `--ttl-minutes` is optional (the gateway
caps it). Once exchanged, the short credential carries the actor, so MCP requests **no longer need
identity headers**; if you still send them, provider+id must match the bound values or the gateway
returns 403 `actor_mismatch`.

Dependencies: `mcp`, `httpx`. Contract and error codes:
[../../docs/exchange-credential.md](../../docs/exchange-credential.md). The standalone OpenClaw
bridge plugin lives in [../../integrations/openclaw-baseward/](../../integrations/openclaw-baseward/).
