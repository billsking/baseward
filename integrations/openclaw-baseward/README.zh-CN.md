# BaseWard OpenClaw 桥插件

[English](README.md) | [简体中文](README.zh-CN.md)

把 BaseWard MCP 服务绑定到**宿主可信的消息发送者**：为每个钉钉用户换发一张短期、绑定行为人的
凭证，并作为该请求者 MCP 连接的 `Authorization` 交给宿主。模型全程看不到凭证，也无法选择用谁的身份。

已对 OpenClaw `2026.8.1` 与 `2026.11.3` 校验契约（`api.registerMcpServerConnectionResolver`）。

## 工作原理

```
钉钉用户 → dingtalk-connector → OpenClaw 运行（SenderId = senderStaffId）
                                      │
                   resolver(ctx.requesterSenderId)   ← 宿主可信，非模型输入
                                      │
                   POST /api/exchange（Bearer gw_<换发令牌> + 行为人身份头）
                                      │
                   sc_<短期凭证> → 该请求者 MCP 连接的 Authorization
```

- 换发令牌（`token_kind=exchange`）**不能查库**，只能铸造凭证。
- 每张短期凭证绑定单一行为人与调用方来源 IP，继承换发令牌的授权范围，并到期失效（默认 15 分钟）。
- 按行为人缓存，到期前 `refreshSkewSeconds` 重新换发；同一行为人的并发请求共用一次换发（单飞）。
- 失败即关闭：没有可信发送者、或来自其他渠道的运行，**不会**建立 BaseWard 连接；换发失败则该轮
  省略此服务并记一条警告。

## 前置条件

1. BaseWard 已运行，并在管理端创建了**换发型** Token（令牌类型 = 换发），授权范围收敛到机器人
   应对所有人暴露的数据。
2. 代理主机出口 IP 已加入 BaseWard 的 MCP 白名单——短期凭证绑定 IP，换发与使用必须同一出口。
3. OpenClaw ≥ 2026.8.1，官方钉钉连接器已安装并正常工作。

## 安装

```bash
# 在本仓库检出目录下（本地路径安装；--link 保持可编辑）
openclaw plugins install ./integrations/openclaw-baseward --link
```

入口是 TypeScript 源码，本地路径安装时由 OpenClaw 的运行时转换器加载。若要打成 tarball 分发，
先用 `openclaw plugins pack --root ./integrations/openclaw-baseward` 构建打包，再用 `npm-pack:` 安装。

## 配置

声明 MCP 服务（工具面保持静态，resolver 只绑定传输）：

```jsonc
// openclaw.json
{
  "mcp": {
    "servers": {
      "baseward": {
        "url": "http://127.0.0.1:8080/mcp/",   // 会被 resolver 按请求者覆盖
        "transport": "streamable-http"
      }
    }
  },
  "plugins": {
    "entries": {
      "baseward-bridge": {
        "config": {
          "gatewayUrl": "http://127.0.0.1:8080",
          "exchangeToken": "gw_替换为换发令牌",
          "serverName": "baseward",
          "provider": "dingtalk",
          "channel": "dingtalk-connector"
        }
      }
    }
  }
}
```

| 配置项 | 默认 | 含义 |
|---|---|---|
| `gatewayUrl` | 必填 | BaseWard 源地址 |
| `exchangeToken` | 必填 | `exchange` 型 `gw_` 令牌 |
| `serverName` | `baseward` | 必须与声明的 MCP 服务名一致 |
| `mcpPath` | `/mcp/` | MCP 挂载路径 |
| `channel` | `dingtalk-connector` | 只为该渠道解析 |
| `provider` | `dingtalk` | `X-BaseWard-Actor-Provider` |
| `ttlMinutes` | 网关默认 | 可选请求有效期（网关封顶） |
| `refreshSkewSeconds` | `60` | 到期前多久重新换发 |

改完配置需重启网关。

## 验证

发一条会触发查库的钉钉消息，然后看 BaseWard 管理端审计：该行应为 `user_name` = 换发令牌属主、
`actor_id` = 钉钉 `senderStaffId`、`actor_trust` = `asserted`、`credential_account` = `sc_` 前缀。
管理端「短期凭证」页能看到该行为人在途的凭证。

## 边界

- **行为人仍是自报**：换发不引入签名，换发令牌持有者可声称任意行为人。见 BaseWard `SECURITY.zh-CN.md` L18/L20。
- **授权不按行为人区分**：所有用户行使换发令牌的同一份 scope。
- **不带昵称**：resolver 上下文只有发送者 id，因此不发送 `X-BaseWard-Actor-Name`，审计只显示 id。
- **吊销有延迟**：宿主最多每 5 分钟重新校验已解析连接，被吊销的凭证可能用到下一次解析为止；
  到期由本地 skew 提前处理。
- **无发送者即无服务**：cron、子代理、心跳运行没有可信请求者，按设计不会出现 BaseWard 服务。

## 开发

```bash
npm install
npm test          # vitest，20 条用例
npm run type-check
```

运行时依赖：无（Node ≥ 18 自带 `fetch`）。`openclaw` 为可选 peer 依赖；插件只在 `src/types.ts`
声明它用到的结构化类型。