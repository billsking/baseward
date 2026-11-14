# 终端用户（行为人）身份契约

> 状态：P1（自报级）。可核验身份见文末路线。

## 1. 背景：两个身份

凭证（`gw_` Token）认证的是**持有者**（principal）。当张三把凭证配到 OpenClaw / Hermes
等代理平台，并经钉钉机器人把查库能力暴露给组织内其他人时，真实操作者是**行为人**（actor，
如李四），而非张三。BaseWard 在协议与审计中显式分开这两个身份：

- `audit_log.user_name`：凭证属主（张三）。
- `audit_log.actor_*`：行为人（李四）及其信任级别。

## 2. 请求头契约

MCP（Streamable HTTP）请求携带以下头；仅对 `require` 型 Token 强制。

| 头 | 必填 | 格式 |
|---|---|---|
| `X-BaseWard-Actor-Provider` | require 型必填 | `^[a-z][a-z0-9-]{0,31}$`，如 `dingtalk`；不做白名单 |
| `X-BaseWard-Actor-Id` | require 型必填 | 1..128 个可见 ASCII 字符（0x21–0x7E），如钉钉 `senderStaffId` |
| `X-BaseWard-Actor-Name` | 可选 | base64url(UTF-8 昵称)，padding 可省略；解码后截断 128 字符 |

### 编码规则

HTTP 头按 latin-1 传输，中文昵称必须编码：`base64url(UTF-8)`，`=` padding 可省略。
例：昵称「李四」→ `X-BaseWard-Actor-Name: 5p2O5Zub`。解码失败、含控制字符均判为非法。

## 3. 凭证策略（actor_policy）

| 策略 | 语义 |
|---|---|
| `ignore`（默认） | 个人凭证。忽略身份头，审计 actor 列为空；行为与升级前一致 |
| `require` | 代理凭证。每次调用必须携带合法身份头，否则 403 拒绝并写审计 |

策略在管理后台创建 Token 时选定，也可在 Token 编辑页修改（ignore↔require 即时生效，
变更写审计）。存量凭证升级后默认 `ignore`。

Token 另有类型（`token_kind`）：`access`（默认，可查库）与 `exchange`（只能在
`/api/exchange` 换发短期凭证，固定 `require`，不能查库）。见
[exchange-credential.zh-CN.md](exchange-credential.zh-CN.md)。

## 4. 错误码

| HTTP | body | 审计 deny_reason | 触发 |
|---|---|---|---|
| 403 | `{"error":"actor_required"}` | `ACTOR_REQUIRED` | require 型完全未携带身份头 |
| 403 | `{"error":"actor_invalid"}` | `ACTOR_INVALID` | 有头但不完整/非法（缺 id、provider 格式错、base64 损坏、超长、控制字符） |

拒绝在中间件层统一生效，覆盖全部 5 个工具；拒绝审计的 `user_name` 为凭证属主。
既有的 401（token 无效）、403（IP 白名单）行为不变。

## 5. 信任级别与伪造边界

P1 信任级别恒为 `asserted`（自报）：网关记录平台**声称**的行为人，不做签名/验签。

- 凭证持有者可以声称任意身份（包括他人）；`require` 只保证行为人*存在*，不保证*真实*。
- 个人（`ignore`）型凭证被贴进代理平台且不做对接时，与属主本人使用无法区分——这是
  bearer 凭证的物理边界。治理手段：机器人场景只发 `require` 型凭证 + IP 白名单。

详见 SECURITY 文档 L18。

## 6. 授权语义（仅审计）

P1 授权仍按凭证属主的 `scope` 评估，行为人**不参与授权**。终端用户实际行使属主的数据
权限。请把代理 Token 收敛到机器人应对所有用户暴露的最小数据集。详见 SECURITY 文档 L19。

## 7. 导出通道

`require` 型凭证**禁用导出签发**（MCP 显式签发、截断自动签发、管理员手动签发三条路径
统一拒绝，错误码 `CREDENTIAL_ISSUE_DENIED`）。MySQL 导出协议无逐查询身份字段，故导出
审计行 actor 列为空。Token 改为 `require` 后，已签发未过期的导出凭证不追溯吊销，随 TTL
自然失效。

## 8. 接入模式

无状态 HTTP 下，推荐**每个终端用户一个轻量 MCP client，身份写死在静态头**；或在平台
插件/中间件钩子里逐请求注入。可运行示例见 [../examples/actor_adapter/](../examples/actor_adapter/)。

若代理平台不应长期持有可查库的凭证，改用 `exchange` 型令牌为每个终端用户换发短期、
绑定行为人的凭证，由凭证自身携带行为人。见
[exchange-credential.zh-CN.md](exchange-credential.zh-CN.md)。

## 9. 审计字段

`audit_log` 新增 `actor_provider` / `actor_id` / `actor_name` / `actor_trust`，索引
`idx_audit_actor (actor_provider, actor_id, created_at)`。管理后台审计列表支持按行为人
筛选（`actor` 参数，模糊匹配 id/name），详情页展示双身份与信任徽标。

## 10. 路线

- **P2**：注册代理 + HMAC 签名断言（防终端用户伪造）。
- **P3**：钉钉回调验签 + 按行为人授权（scope 交集）。按用户令牌换发已实现为
  [短期凭证](exchange-credential.zh-CN.md)；其绑定的行为人仍为自报，授权仍按换发令牌的
  scope 评估。