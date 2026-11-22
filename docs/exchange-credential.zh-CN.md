# 短期凭证换发（代理平台 / OpenClaw）

[English](exchange-credential.md) | [简体中文](exchange-credential.zh-CN.md)

> 状态：已实现。行为人信任级别仍为 `asserted`（自报），见 [行为人身份契约](actor-identity.zh-CN.md)。

## 1. 两种令牌

| 类型 | `token_kind` | 能做什么 | 不能做什么 |
|---|---|---|---|
| 常规令牌 | `access`（默认） | 按授权范围访问数据源；`actor_policy` 可选 ignore/require | — |
| 换发令牌 | `exchange` | 只能调用 `POST /api/exchange` 换发短期凭证 | 不能查库、不能签发导出凭证、不能直接调 MCP 工具 |

换发令牌的行为人策略**固定为 `require`**：创建时即使选 ignore 也会被强制为 require，创建后不可降级（管理端与服务端同样拒绝）。它的授权范围（scope）就是它换发出的所有短期凭证的**权限上限**。

短期凭证（`sc_` 前缀）才是真正用于 MCP 查库的凭证：绑定单一行为人、绑定来源 IP、继承换发令牌的 scope 快照、到期自动失效。

## 2. 为什么这样分

把"能查库的凭证"与"能发凭证的凭证"分开，代理平台（OpenClaw + 钉钉机器人）主机上只长期持有换发令牌；每个终端用户拿到的是分钟级、绑定 IP 与行为人的短期凭证。主机被入侵时泄露面从"N 个永久令牌"降为"1 个换发令牌 + 若干在途短期凭证"，且换发令牌本身查不了任何数据。

## 3. 换发端点

```
POST /api/exchange
Authorization: Bearer gw_<换发令牌>
X-BaseWard-Actor-Provider: dingtalk
X-BaseWard-Actor-Id: manager1234
X-BaseWard-Actor-Name: 5p2O5Zub        # 可选，base64url(UTF-8)
Content-Type: application/json

{"ttl_minutes": 15}                     # 可选，缺省用 GATEWAY_EXCHANGE_TTL_MINUTES
```

身份头契约与 [actor-identity.zh-CN.md](actor-identity.zh-CN.md#2-请求头契约) 完全一致（provider 必填且匹配 `^[a-z][a-z0-9-]{0,31}$`，id 为 1..128 个可见 ASCII 字符，昵称 base64url）。来源 IP 白名单与 MCP 入口共用同一份配置。

成功响应 `200`：

```json
{
  "credential": "sc_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "expires_at": "2026-11-13T12:34:56.789Z",
  "ttl_seconds": 900,
  "actor": {"provider": "dingtalk", "id": "manager1234", "name": "李四"}
}
```

明文只在响应里出现一次，库里只存 SHA-256 摘要。

### 错误码

错误与 MCP 鉴权、工具错误使用相同响应结构，`request_id` 与响应头 `X-Request-ID` 一致：

```json
{"error": {"code": "invalid_token", "message": "...", "request_id": "..."}}
```

| HTTP | body `error.code` | 审计 deny_reason | 触发 |
|---|---|---|---|
| 403 | `ip_not_allowed` | `IP_NOT_ALLOWED` | 来源不在 MCP 白名单 |
| 401 | `missing_token` / `invalid_token` | `AUTH_INVALID` | 缺 Bearer、格式错、令牌无效、已停用或已过期 |
| 403 | `exchange_token_required` | `EXCHANGE_TOKEN_REQUIRED` | 用了常规令牌或短期凭证来换发 |
| 403 | `actor_required` | `ACTOR_REQUIRED` | 完全未携带身份头 |
| 403 | `actor_invalid` | `ACTOR_INVALID` | 身份头不完整/非法 |
| 400 | `invalid_request` | `EXCHANGE_INVALID` | `ttl_minutes` 越界，或无法确定来源 IP |
| 503 | `auth_unavailable` | — | 管理库不可用 |

## 4. 使用短期凭证

```
POST /mcp/
Authorization: Bearer sc_<短期凭证>
```

- **身份头可省略**：行为人已在换发时绑定，网关从凭证带出，审计照样记录双身份。
- **带头必须一致**：若仍携带 `X-BaseWard-Actor-*`，其 provider+id 必须与凭证绑定值一致，否则 `403 actor_mismatch`（审计 `ACTOR_MISMATCH`）。昵称以凭证绑定值为准。
- 授权范围按凭证里的 scope 快照评估；换发后修改换发令牌的 scope **不影响已签发的短期凭证**（随 TTL 自然收敛）。

凭证失效时统一返回 `401 invalid_token`，审计区分具体原因：

| 审计 deny_reason | 含义 |
|---|---|
| `SHORT_CREDENTIAL_EXPIRED` | 已过期 |
| `SHORT_CREDENTIAL_REVOKED` | 已吊销（含被新凭证取代） |
| `SHORT_CREDENTIAL_IP_MISMATCH` | 来源 IP 与换发时不一致 |
| `PARENT_TOKEN_DISABLED` | 所属换发令牌已停用、删除或过期 |

客户端（桥插件）遇到 401 应重新换发，而不是重试同一凭证。

## 5. 生命周期

- **TTL**：默认 15 分钟，上限 60 分钟，均可配置（见 §8）。请求可传更短的 `ttl_minutes`。
- **取代**：同一换发令牌 + 同一行为人再次换发时，先前仍有效的短期凭证立即被吊销（审计 `exchange_supersede`），因此每个行为人在任一时刻最多一个有效凭证。
- **吊销**：管理端「短期凭证」页可提前吊销，立即生效。
- **清理**：每日 03:00（Asia/Shanghai）随审计清理一起删除已过期行，并写 `exchange_expire_cleanup` 审计。
- **级联**：删除换发令牌会级联删除其短期凭证。

## 6. 审计

短期凭证的每次调用都记录双身份：

- `user_name` / `token_id` / `token_prefix`：**换发令牌属主**（机器人运营方）。
- `actor_provider` / `actor_id` / `actor_name` / `actor_trust`：终端用户，`actor_trust` 恒为 `asserted`。
- `credential_account`：短期凭证前缀（`sc_…`），用于定位具体凭证。

换发链路事件：`credential_lifecycle`（`exchange_issue` / `exchange_supersede` / `exchange_revoke` / `exchange_expire_cleanup`）与 `exchange`（换发被拒）。管理端审计列表可按行为人筛选。

## 7. 信任边界

- 行为人身份仍是**自报**：换发令牌持有者可以声称任意终端用户。换发不引入签名或钉钉验签，`actor_trust` 不变。
- 授权仍按**换发令牌的 scope**，不是按行为人差异化授权。所有终端用户行使同一份数据权限，请把换发令牌收敛到机器人应对所有人暴露的最小数据集。
- 短期凭证绑定来源 IP：换发与使用必须来自同一出口 IP。代理平台多出口或 NAT 变化会导致 `SHORT_CREDENTIAL_IP_MISMATCH`。
- 换发令牌是**高价值凭证**：它能为任意行为人铸造短期凭证。只交给可信代理平台，配合 IP 白名单，并在管理端监控 `exchange_issue` 频率。

## 8. 配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `GATEWAY_EXCHANGE_TTL_MINUTES` | `15` | 缺省有效期（分钟，1-1440） |
| `GATEWAY_EXCHANGE_MAX_TTL_MINUTES` | `60` | 请求可指定的上限（分钟，1-1440）；不得小于缺省值 |

## 9. OpenClaw 接入

官方钉钉连接器把 `senderStaffId`（缺失时退化为 `senderId`）作为 `SenderId` 交给宿主；OpenClaw 的 `api.registerMcpServerConnectionResolver` 会把它作为可信的 `requesterSenderId` 传给插件。桥插件据此：

1. 用换发令牌 + 该行为人身份头调 `/api/exchange`；
2. 把返回的 `sc_` 凭证作为 `Authorization` 返回给宿主，绑定到该请求者的 MCP 连接；
3. 按行为人缓存，过期或收到 401 时重新换发。

模型全程看不到凭证，也无法选择用哪个凭证——提示词注入改不了行为人。可运行示例见 [../examples/actor_adapter/](../examples/actor_adapter/)，独立插件包见 [../integrations/openclaw-baseward/](../integrations/openclaw-baseward/)。

## 10. 路线

- 已实现：换发令牌 / 短期凭证 / IP 与行为人绑定 / TTL / 取代与吊销 / 双身份审计。
- 未实现：注册代理 HMAC 验签（P2）、钉钉回调验签与按行为人差异化授权（P3）。