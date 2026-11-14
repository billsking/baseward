# 行为人身份参考适配器（示例）

一个可运行的示例：在调用 BaseWard MCP 工具时，断言机器人/代理平台（OpenClaw、Hermes、
钉钉机器人等）背后的**真实终端用户**。

> **仅为示例。** P1 的行为人身份是*自报*的：网关记录平台声称的人，不做密码学核验。
> 请先阅读下方信任边界与 [../../docs/actor-identity.zh-CN.md](../../docs/actor-identity.zh-CN.md)。

## 运行

```bash
python examples/actor_adapter/demo.py \
  --base-url http://127.0.0.1:8080/mcp/ \
  --token gw_xxx \
  --actor-id manager1234 \
  --actor-name 李四 \
  --datasource prod \
  --sql "SELECT 1"
```

`--token` 缺省读取环境变量 `GATEWAY_TOKEN`。不带 `--datasource` 时只调用 `list_datasources`。
Token 必须是 **require（代理）型**，身份头才会被强制；个人型会忽略身份头。

## 接入模式

网关的 MCP 传输是无状态 HTTP，每个请求各自携带头。关键模式：**每个终端用户一个轻量
MCP client，把该用户身份写死在静态头里**——不要求客户端支持动态头。

```python
from demo import actor_headers
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

# 把渠道身份（如钉钉 senderStaffId）映射为契约头。
headers = {
    "Authorization": f"Bearer {agent_token}",
    **actor_headers("dingtalk", message["senderStaffId"], message.get("senderNick")),
}
async with streamablehttp_client(url, headers=headers) as (read, write, _):
    async with ClientSession(read, write) as session:
        await session.initialize()
        await session.call_tool("execute_sql", {"datasource": "prod", "sql": sql})
```

对于有插件/中间件钩子的平台（OpenClaw 插件、Hermes 工具包装），改为在钩子里从渠道上下文
逐请求注入同样的头，而不必每用户建一个 client。

## 信任边界（生产使用前必读）

- 网关信任**凭证持有者**诚实上报身份。持有者可以声称是任何人；`require` 只保证行为人
  *存在*，不保证*真实*。
- 授权仍按凭证属主的授权范围（P1 仅审计）：每个终端用户实际行使属主的数据权限。请把
  代理 Token 收敛到机器人应对所有用户暴露的最小数据集。
- 可核验身份（注册代理签名、钉钉回调验签、按用户令牌换发）规划在 P2/P3，不在本示例内。
## 短期凭证换发（推荐用于机器人场景）

`exchange_demo.py` 演示完整链路：用**换发型**令牌换一张短期凭证，再用它查库。换发型令牌本身
不能查库，主机上不需要长期持有可查库凭证。

```bash
python examples/actor_adapter/exchange_demo.py \
  --base-url http://127.0.0.1:8080/mcp/ \
  --exchange-token gw_xxx \
  --actor-id manager1234 \
  --actor-name 李四 \
  --datasource prod \
  --sql "SELECT 1"
```

`--exchange-token` 缺省读取环境变量 `GATEWAY_EXCHANGE_TOKEN`；`--ttl-minutes` 可选（网关封顶）。
换发成功后，短期凭证自身携带行为人，MCP 请求**不再需要身份头**；若仍带头，provider+id 必须与
凭证绑定值一致，否则 403 `actor_mismatch`。

依赖：`mcp`、`httpx`。契约与错误码见 [../../docs/exchange-credential.zh-CN.md](../../docs/exchange-credential.zh-CN.md)。
OpenClaw 侧的独立桥插件见 [../../integrations/openclaw-baseward/](../../integrations/openclaw-baseward/)。
