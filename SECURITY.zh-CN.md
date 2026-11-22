# 安全策略

[English](SECURITY.md) | [简体中文](SECURITY.zh-CN.md)

本文档说明 BaseWard（库卫）的安全模型、当前已实现的控制、**已知限制**，以及漏洞报告方式。

如果你准备把网关接上真实数据，请先读完第 4 节「已知限制」和第 5 节「安全部署基线」。其中若干限制是设计取舍，不是待修复的缺陷；把它们当成默认安全来用会出问题。

## 1. 支持版本

| 版本 | 安全更新 |
| --- | --- |
| 最新小版本（当前 `v1.8.x`） | 支持 |
| 更早的小版本 | 不单独回补，请升级 |

网关是有状态服务（管理库 + 主密钥），升级前请阅读 README 的升级与迁移说明并备份管理库。

## 2. 报告漏洞

- 请通过仓库 **Security → Advisories → Report a vulnerability** 私密提交；不要开公开 Issue、不要在 Discussion 里贴利用细节。
- 若无法使用 GitHub 私密报告，可发送邮件至 **billsking@qq.com**，主题加前缀 `[BaseWard security]`；同样不要开公开 Issue、不要在 Discussion 里贴利用细节。
- 报告请包含：受影响版本、复现步骤、影响面（能否跨数据源/跨 Token 读写、能否绕过脱敏或审计）、以及你验证时使用的部署形态。
- 维护者目标：5 个工作日内确认收到，30 天内给出修复或缓解方案。修复发布后会在 Advisory 中致谢（除非你要求匿名）。

## 3. 威胁模型

### 3.1 受保护资产

- 业务库数据源密码（静态加密存储）
- MCP Token 与其四级授权范围
- 导出凭证（短时 MySQL 账号）
- 审计日志（含完整 SQL 字面量与请求来源）
- 管理员会话

### 3.2 信任边界

| 边界 | 说明 |
| --- | --- |
| AI 客户端 → `POST /mcp` | Bearer Token 认证；请求方被视为「可能被诱导、可能被攻陷」 |
| 网关 → 业务库 | 使用数据源自身账号；网关不是数据库权限的替代品 |
| 网关 → 管理库 | 存放加密后的凭证、Token 哈希、审计 |
| 浏览器 → `/admin` | 管理员会话 + CSRF |
| 导出客户端 → `:3307` | MySQL 协议 + 短时凭证；来源 IP 白名单 |

### 3.3 假定攻击者

- 持有合法但受限 Token 的客户端，试图越权读未授权库/表/列；
- 被提示注入或恶意插件控制的 AI 客户端，试图执行写操作、DDL 或批量导出；
- 能访问网关端口但没有 Token 的同网段主机；
- 拿到导出凭证、试图扩大可用库或延长有效期的客户端。

### 3.4 明确不在防护范围

- 业务库、管理库、宿主机或主密钥本身已被攻陷；
- 拥有数据库超级权限的 DBA 绕过网关直接取数；
- 管理员主动导出/泄露数据（审计只留痕，不阻止）；
- 业务库账号本身权限过大导致网关策略被绕过；
- 侧信道、供应链投毒、物理访问。

## 4. 已知限制（自证）

下表逐条列出**当前版本确实没有**的能力、代码证据与可行的缓解方式。判断依据是代码本身，不是文档承诺。

### L1. 网关自身不提供 TLS

- **现状**：MCP 与 HTTP 管理端由 uvicorn 以明文 HTTP 监听；仓库没有证书与 TLS 监听配置。导出通道 `:3307` 同样不配置 TLS，客户端连接示例显式使用 `ssl_disabled=True`（`app/mcp/pipeline.py:331`）。网关到业务库的连接也未启用 TLS（`app/datasources/pool.py:24`、`app/datasources/export_pool.py:121` 均无 `ssl` 参数）。
- **影响**：Token、SQL、查询结果、管理员会话 Cookie 明文经过网络；同链路可被窃听或中间人篡改。
- **缓解**：只部署在受控私有网络中；在网关前用反向代理终止 TLS，并设置 `GATEWAY_COOKIE_SECURE=true`；导出端口只绑定私有网卡并限制来源。
- **不在当前计划**：内置 TLS 终止。

### L2. 主密钥不支持轮换

- **现状**：`GATEWAY_MASTER_KEY` 用于 AES-256-GCM 加密数据源密码，并通过 HKDF-SHA256 派生脱敏与会话子密钥（`app/security/crypto.py`、`app/services.py:53`）。代码中没有重新加密/切换主密钥的路径；README「已知限制」也写明一期不支持轮换。
- **影响**：主密钥一旦泄露，所有已存数据源密码都应视为已泄露；恢复只能重置主密钥并重新录入所有数据源，同时所有会话与导出凭证失效。主密钥丢失则已存密码不可恢复。
- **缓解**：离线保管、与数据库备份分离、按最小人数分发；定期验证备份可恢复。
- **修复方向**：引入密钥版本号与惰性重加密（尚未实现）。

### L3. 应用层策略不能替代数据库最小权限

- **现状**：SQL 策略引擎基于 AST 默认拒绝（`app/policy/engine.py`），但业务库仍使用数据源自己的账号。
- **影响**：如果数据源账号有 `DROP`/`GRANT`/跨库权限，任何策略绕过（解析器缺陷、未覆盖语法）都会直接落到真实权限上。
- **缓解**：只读源使用仅 `SELECT/SHOW` 的账号；可写源只授予必要表的 DML；不要用高权限账号接入。

### L4. 脱敏血缘仅限已解析的查询结构

- **现状**：脱敏会沿嵌套派生表、CTE、`UNION` 分支、别名以及命名列与 `*` 混合投影追踪列血缘（`app/masking/engine.py`）。它不解析存储过程或函数的内部逻辑；支持范围之外的表达式仍依赖保守规则合并或自动关键词。
- **影响**：复杂表达式语义可能导致规则选择不符合预期，保守兜底也可能过度脱敏；网关不能替代数据库例程内部的控制。
- **缓解**：对敏感表优先使用「自动关键词 + 手动规则」双保险；上线前用真实 SQL 回归验证，并保持数据库账号最小权限。

### L5. 无行级权限

- **现状**：Token 授权是数据源/库/表/列四级白名单（`app/tokens/`、`app/policy/scope.py`），没有行级过滤。
- **影响**：有表权限即可读取该表全部行；需要按租户/区域隔离时，网关层面无法表达。
- **缓解**：为不同人群建视图或独立数据源账号；必要时在业务库侧做行级安全。

### L6. 每个用户仅一个未过期的启用 Token，且不支持自动轮换

- **现状**：Token 以 SHA-256 存储，停用立即生效。创建时可设置可选的 `expires_at`；过期 Token 会被拒绝，其短期凭证与导出凭证也会被拒绝。管理端列表展示过期时间与 `last_used_at`（写入按 60 秒合并）。未设置过期时间的 Token 会长期有效，且不支持自动轮换或同一用户并行使用多个有效 Token。
- **影响**：未设置过期时间的 Token 泄露后，在停用或删除前一直有效；无法为同一用户并行签发多个用途的 Token。
- **缓解**：按用途设置合理的过期时间，把 Token 当作密码管理；怀疑泄露立即停用并重建；不要写进代码仓库或日志。

### L7. 限流未覆盖所有认证路径

- **现状**：管理端登录按用户名 + 客户端 IP 统计连续失败并临时锁定。MCP 有全局/单 Token 并发上限和单 Token 固定窗口限流；导出通道有连接、查询与并发配额。MCP Bearer 认证与导出 MySQL 认证没有独立的失败登录锁定。
- **影响**：这些认证路径仍可能遭到在线猜测；MCP 固定窗口限流也不是跨多进程的分布式限流器。
- **缓解**：保持来源 IP 白名单启用，面向公网的相邻链路在反向代理/网络层增加 IP 限流，并用审计的 `duration_ms`、`row_count` 做异常告警。

### L8. HSTS 取决于部署配置

- **现状**：响应已包含带每请求 nonce 的 `Content-Security-Policy`、`X-Frame-Options`、`X-Content-Type-Options`、`Referrer-Policy` 与 `Permissions-Policy`；管理端 HTML 也禁用缓存。只有 `GATEWAY_COOKIE_SECURE=true` 时才发送 `Strict-Transport-Security`，且网关自身仍不提供 TLS 监听。
- **影响**：已经终止 HTTPS 但仍设置 `GATEWAY_COOKIE_SECURE=false` 的部署不会从网关获得 HSTS，也无法安全使用 Secure 管理端 Cookie。
- **缓解**：在可信反向代理终止 TLS 并设置 `GATEWAY_COOKIE_SECURE=true`；管理端只对可信网络开放。

### L9. 导出密码只显示一次

- **现状**：导出凭证密码 192 位随机，只在签发响应中出现一次；服务端仅保存 `mysql_native_password` 的 `SHA1(SHA1(password))` 十六进制（`app/security/crypto.py:101`、`app/credentials/service.py:400`）。
- **影响**：密码丢失后无法找回，只能吊销并重新签发；已连接的会话在凭证过期/吊销前仍可继续。
- **缓解**：签发后立即写入调用方的密钥管理；不要把连接串落到日志或工单。

### L10. 导出凭证绑定精确来源 IP

- **现状**：凭证可绑定来源 IP，比对为精确匹配（`app/credentials/`）。
- **影响**：客户端经 NAT 或出口 IP 变化时会认证失败。
- **缓解**：绑定客户端固定出口 IP，或留空绑定并依赖网络层白名单。

### L11. 多库仅限同一 MySQL 实例

- **现状**：一个数据源对应一台主机 + 一套账号，多库靠可访问库清单表达（`app/datasources/databases.py`）。
- **影响**：跨实例查询需要在网关外聚合，或建多个数据源并分别授权。
- **缓解**：按实例拆分数据源；Token 可同时授权多个数据源。

### L12. MCP SELECT 没有上游硬行数上限

- **现状**：MCP 语句有墙钟超时（`GATEWAY_MCP_QUERY_TIMEOUT_SECONDS`，默认 30 秒），超限的 UPDATE/DELETE 会回滚（`GATEWAY_MCP_WRITE_MAX_AFFECTED_ROWS`，默认 1000）。MCP 结果按行数/字节上限截断并可转导出凭证（`GATEWAY_MCP_RESULT_MAX_ROWS`、`GATEWAY_MCP_RESULT_MAX_BYTES`）；导出通道有行数、时长与并发配额。网关不会注入 `LIMIT`，也不限制 SELECT 在上游实际处理的行数。
- **影响**：大 SELECT 仍可能让业务库在返回首批行前完成大量工作；写行数上限也只能在语句返回受影响行数后判定。任一 MCP 上限设为 `0` 都会关闭对应保护。
- **缓解**：保持 MCP 超时与写行数上限启用，在支持的引擎上设置 `max_execution_time`/`max_statement_time`，监控审计字段，并保留独立的导出配额。

### L13. 审计可被管理员清理

- **现状**：审计默认保留 180 天，每日自动清理，管理员也可在管理端手动清理。
- **影响**：拥有管理员权限的人可以删除痕迹（包括自己的操作记录）。
- **缓解**：把审计库日志同步到独立的只追加存储；限制管理员账号数量。

### L14. 管理端无多因素认证

- **现状**：管理员使用用户名 + 密码登录，会话固定 8 小时，改密使旧会话失效。
- **缓解**：管理端只在内网/跳板机后开放；使用独立强密码；结合 SSO 反代做二次认证。

### L15. 单进程、无内置高可用

- **现状**：MCP、管理端、导出通道共享一个 uvicorn 进程；连接池按数据源隔离。
- **影响**：进程崩溃或主密钥配置错误会同时影响所有通道；多副本部署需要额外考虑导出端口与审计写入。
- **缓解**：由编排系统负责重启与健康检查；升级前做管理库备份。

### L16. MySQL 协议族的策略行为只在部分引擎上实测过

- **现状**：SQL 解析固定为 MySQL 方言（`app/policy/engine.py:134`），系统库排除清单是单一小写集合 `mysql` / `information_schema` / `performance_schema` / `sys` / `metrics_schema` / `oceanbase`（`app/datasources/databases.py:34`），所有 information_schema 读取都经 `exclude_system_schemas()` 做大小写不敏感比较（`app/datasources/databases.py:51`），元数据查询依赖 `information_schema` 与 `table_comment` / `column_comment`。默认拒绝是** fail-closed 的**：无法解析（`app/policy/engine.py:135-137`）与退化成原始命令的语句（`exp.Command` 在 `_FORBIDDEN_NODES`，`app/policy/engine.py:33`）一律拒绝。集成测试已在 MySQL 8.0.46、MariaDB 11.8.9、TiDB v8.5.0、OceanBase CE 4.4.2.1 上全量通过（各 243/243，逐条环境与命令见 [docs/compatibility.zh-CN.md](docs/compatibility.zh-CN.md) 的实测记录）。OceanBase 的两项部署前提见下面 L17；MySQL 5.7 / 8.4、Percona、Aurora·RDS、PolarDB、TDSQL、GoldenDB、GaussDB(for MySQL) 仍未实测。
- **影响**：已实测引擎上的风险方向是**功能收窄**（厂商专有语句被拒）与**元数据噪音**，不是策略绕过。其中一条差异已取证并已修复：修复前 TiDB v8.5.0 的 `list_upstream_databases()` 返回大写的 `INFORMATION_SCHEMA`、`PERFORMANCE_SCHEMA`、`METRICS_SCHEMA`（当时清单按精确大小写匹配，故未被过滤），OceanBase CE 4.4.2.1 返回 `oceanbase`；这些库会出现在对象搜索、`describe_datasource` 与管理端联想里，但令牌作用域仍然生效，所以是暴露噪音而非越权。清单现已纳入这两个引擎自有系统库并改为大小写不敏感，回归覆盖是 `tests/unit/test_system_schemas.py`；**四引擎集成套件未在该修复后重跑**，所以「噪音在真实引擎上消失」还不是实测结论。清单之外的引擎内部库（如 `__recycle_bin__`）仍不排除。未实测引擎的逐条差异要按 compatibility 文档自证，未验证不等于已验证。
- **缓解**：接入非主参考引擎前，先把集成套件指向目标引擎跑一遍（需 `CREATE/DROP DATABASE` 权限），并按 [docs/compatibility.zh-CN.md](docs/compatibility.zh-CN.md) 的检查清单逐条核对，通过后写进该文档的状态表。系统库噪音的修复已落地（大小写不敏感 + 纳入引擎自有系统库，引擎侧未重跑取证），接入新引擎前仍应按数据源实际授予最小权限。业务库账号仍按最小权限授予。

### L17. OceanBase 忽略握手里的 autocommit 关闭标志（已由连接池修复）

- **现状**：写入走 `async with engine.begin()`（`app/mcp/pipeline.py:258-263`），审计落库失败时靠这个块抛异常回滚上游写入。MySQL 8.0.46、MariaDB 11.8.9、TiDB v8.5.0 的连接会话 `@@autocommit = 0`（客户端在握手阶段请求关闭自动提交），回滚有效；OceanBase CE 4.4.2.1 实测 `@@autocommit = 1`——它不接受握手里的该标志，语句执行完即提交，`ROLLBACK` 成为空操作。修复前同一条 `UPDATE` + 强制回滚的对照实验里，MySQL 与 TiDB 结果仍是原值，OceanBase 已是新值。
- **影响**：修复前，在 OceanBase 上若审计结果更新失败，调用方会拿到 `AUDIT_UNAVAILABLE`、审计行状态为 `error`，**但写入已经落到业务库**。策略判定本身没有被绕过（语句仍经过解析、作用域与脱敏），失效的是「审计不可用则写入不生效」这条承诺。修复后该承诺在 OceanBase 上恢复。
- **缓解**：连接池在建连时执行 `SET autocommit=0`（`app/datasources/pool.py` 的 `init_command`），对其余三个引擎是无操作。回归覆盖是 `tests/integration/test_pipeline_failclosed.py::test_pool_session_has_autocommit_off` 与 `tests/unit/test_datasource_pool.py`；修复后 OceanBase 重跑完整集成套件 243 passed。第二项前提是 OceanBase 默认 10s 的 `ob_query_timeout`：大结果集读取会被它掐断，接入时需按数据源调高（实测时为 600s，见 [docs/compatibility.zh-CN.md](docs/compatibility.zh-CN.md)）。

### L18. 终端用户（行为人）身份为自报，未经核验

- **现状**：P1 的行为人身份来自 MCP 入口解析的 `X-BaseWard-Actor-*` 请求头（`app/security/mcp_auth.py`），信任级别固定为 `asserted`（`app/security/actor.py:23,31`）。没有签名，也没有钉钉回调验签。短期凭证换发（`app/exchange/`）会把自报的行为人绑定到凭证上，但绑定不等于核验。
- **影响**：凭证持有者（如机器人运营者）可以声称任意终端用户身份（包括他人）；审计记录的是平台*声称*的人，而非密码学核验的行为人。`require` 型凭证只保证行为人*存在*，不保证*真实*。个人（`ignore`）型凭证被贴进机器人平台且不做对接时，与属主本人使用无法区分。
- **缓解**：只向可信中间平台签发 `require` 型凭证；把 `asserted` 审计行视为"可追溯到平台"而非"不可抵赖"。机器人平台优先用 `exchange` 型令牌，使主机不长期持有可查库凭证（见 L20）。可核验身份（注册代理 HMAC、钉钉回调验签）规划在 P2/P3，本期未实现。见 [docs/actor-identity.zh-CN.md](docs/actor-identity.zh-CN.md) 与 [docs/exchange-credential.zh-CN.md](docs/exchange-credential.zh-CN.md)。

### L19. 行为人身份仅用于审计；授权仍按凭证属主的授权范围

- **现状**：所有策略判定都评估 `principal.scope`（`app/mcp/pipeline.py:117`）；行为人只写入审计、不参与授权，`Principal` 不携带任何按行为人的权限（`app/security/principal.py`）。
- **影响**：机器人背后的终端用户实际行使的是凭证属主的数据权限。审计能回答"谁查的"，但无法阻止"李四查了张三能查、但李四不该查的数据"。
- **缓解**：把凭证属主的 Token 授权范围收敛到机器人应对所有用户暴露的最小数据集。按行为人授权（行为人→用户映射 + 授权交集）是 P3 事项，本期未实现。短期凭证继承换发令牌的 scope 快照，只收敛有效期与暴露面，不按行为人差异化授权。

### L20. 换发令牌可为任意行为人铸造凭证

- **现状**：`POST /api/exchange` 接受换发令牌持有者提交的任意合法行为人断言，并铸造绑定该行为人的短期凭证（`app/exchange/routes.py`、`app/exchange/service.py`）。换发令牌本身在 MCP 入口被拒（`403 exchange_only`），也不能签发导出凭证。
- **影响**：持有换发令牌者可以在审计里冒充任意终端用户，并取得该用户的短期凭证。可达数据仍被换发令牌的 scope 封顶，因此这是审计完整性与凭证扩散风险，而非超出该 scope 的提权。
- **缓解**：每个短期凭证绑定换发来源 IP 与单一行为人、继承 scope 快照、到期失效（默认 15 分钟、上限 60 分钟），同一行为人再次换发时旧凭证立即被取代；过期行每日清理。换发令牌只放在可信代理主机上，启用 MCP 来源 IP 白名单，并在管理端审计里监控 `exchange_issue` 频率。停用、删除或过期换发令牌会立即使其在途短期凭证失效（`PARENT_TOKEN_DISABLED`）。

## 5. 安全部署基线

最低要求：

1. **网络**：网关、管理库、业务库位于受控私有网络；管理端不对公网开放；导出端口只绑定私有网卡（`GATEWAY_EXPORT_BIND`），不要发布为 `0.0.0.0:3307`。
2. **TLS**：网关前必须有 TLS 终止（反向代理），并设置 `GATEWAY_COOKIE_SECURE=true`。
3. **凭据**：`GATEWAY_MASTER_KEY` 与 `GATEWAY_ADMIN_PASSWORD` 通过密钥管理注入，不使用默认值、不写进镜像与仓库；主密钥离线备份。
4. **数据库账号**：业务库账号按最小权限授予；管理库账号只授权 `db_gateway` 库。
5. **来源限制**：配置 MCP 与导出的来源 IP 白名单；网关直接对外时不要依赖 `X-Forwarded-For`，除非确认只有受控反代能写该头。
6. **审计**：审计库视为敏感数据，限制访问并加密备份；保留期按合规要求设置。
7. **升级**：升级前备份管理库；升级后核对迁移版本与 `healthz`。

## 6. 安全不变量

以下行为如果被破坏，按安全漏洞处理：

- 未授权库/表/列的数据不得出现在 MCP 响应或导出结果中；
- `SELECT *` 必须被改写为 Token 允许的列集合；
- 无法解析或无法确定归属的 SQL 必须拒绝，而不是放行；
- 审计写入失败时查询必须被阻断（`AUDIT_UNAVAILABLE`），不得「先执行后补审计」；
- Token、数据源密码、导出密码明文不得出现在应用日志中；
- 导出凭证过期或吊销后，新连接必须立即被拒绝。
- 换发型令牌不得到达任何数据源查询或导出签发；
- 短期凭证过期、吊销、来源 IP 变化或所属换发令牌失效后，请求必须立即被拒绝。

## 7. 测试与扫描现状

- 仓库当前收集到 883 个测试用例：非集成 625 个（不依赖 MySQL）、集成 258 个（需要专用测试库）；覆盖策略引擎、四级授权、脱敏、审计 fail-closed、导出凭证、短期凭证换发与协议补丁。
- CI 会对从 `pyproject.toml` 生成的运行依赖执行 `pip-audit --strict`，并校验生成文件没有漂移。当前仍**没有**配置 SAST、模糊测试或渗透测试报告；测试与依赖扫描不代表完成安全审计。
- 导出通道依赖 `mysql-mimic==3.0.5` 并带有两处协议层补丁，相关行为有专项测试（见 `tests/integration/test_export_*.py`）。

## 8. 披露政策

- 采用协调披露：修复版本发布后公开细节；默认给报告者 90 天，双方可协商延长。
- 若漏洞已被在野利用，或影响面覆盖所有部署（例如认证绕过），维护者会尽快发布缓解措施并提前公告。
- 请勿在修复发布前公开 PoC、扫描结果或受影响的客户信息。
