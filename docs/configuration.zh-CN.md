# 环境变量

[English](configuration.md) | [简体中文](configuration.zh-CN.md)

| 变量 | 必填 | 默认 | 说明 |
|------|------|------|------|
| `GATEWAY_MASTER_KEY` | 是 | — | base64 32 字节；加密数据源密码、派生会话/脱敏密钥 |
| `GATEWAY_ADMIN_USER` | 首次启动 | — | 管理员用户名，1-64 字符 |
| `GATEWAY_ADMIN_PASSWORD` | 首次启动 | — | 管理员密码，至少 8 字符 |
| `GATEWAY_DB_PATH` | 否 | `data/gateway.db`（容器内 `/data/gateway.db`） | 未配置服务端管理库时使用的 SQLite 文件路径 |
| `GATEWAY_DB_URL` | 否 | — | 完整的 SQLAlchemy 异步 URL（如 `mysql+aiomysql://...`、`postgresql://...`；PostgreSQL 需安装 `asyncpg`）；配置后覆盖下列 MySQL 简写 |
| `GATEWAY_DB_HOST` | 否 | — | 外部 MySQL 管理库主机；配置后必须同时提供账号与密码；与 `GATEWAY_DB_URL` 都留空时使用 SQLite |
| `GATEWAY_DB_PORT` | 否 | `3306` | 管理库端口 |
| `GATEWAY_DB_USER` | 否 | — | 管理库账号 |
| `GATEWAY_DB_PASSWORD` | 否 | — | 管理库密码 |
| `GATEWAY_DB_NAME` | 否 | `db_gateway` | 管理库名 |
| `GATEWAY_COOKIE_SECURE` | 否 | `false` | HTTPS 下设为 `true`；同时启用 `Strict-Transport-Security` 响应头 |
| `GATEWAY_ADMIN_LOGIN_MAX_ATTEMPTS` | 否 | `5` | 同一用户名+客户端 IP 在窗口内允许的登录失败次数，超过后临时锁定（1-100） |
| `GATEWAY_ADMIN_LOGIN_WINDOW_SECONDS` | 否 | `300` | 管理员登录失败计数窗口（10-86400） |
| `GATEWAY_ADMIN_LOGIN_LOCK_SECONDS` | 否 | `900` | 登录失败过多后的临时锁定时长（10-86400）；计数在进程内，多 worker 各自限速 |
| `GATEWAY_ADMIN_SESSION_IDLE_MINUTES` | 否 | `30` | 管理台会话空闲超时（1-1440）分钟；有操作会自动续期，绝对有效期仍为 8 小时 |
| `GATEWAY_LOG_LEVEL` | 否 | `INFO` | 日志级别 |
| `GATEWAY_LOG_FORMAT` | 否 | `text` | `text` 为人类可读格式；`json` 让应用自身的日志每行输出一个 JSON 对象，含 `ts`、`level`、`logger`、`message`、`request_id`（uvicorn 自己的启动/访问日志仍为纯文本） |
| `GATEWAY_METRICS_ENABLED` | 否 | `false` | 在 `/metrics` 暴露 Prometheus 指标（HTTP、MCP 工具调用、在途调用数、连接池占用）；关闭时该路由不存在 |
| `GATEWAY_AUDIT_RETENTION_DAYS` | 否 | `180` | 首次建库的默认保留天数（1-3650） |
| `GATEWAY_AUDIT_REQUIRE_SERVER_BACKEND` | 否 | `false` | 为 `true` 时管理库使用 SQLite 将拒绝启动。审计写入是 fail-closed，而 SQLite 写操作串行，生产环境建议开启 |
| `GATEWAY_HOST` / `GATEWAY_PORT` | 否 | `0.0.0.0` / `8080` | 代码内配置项；Compose 用 `GATEWAY_PORT` 映射宿主端口 |
| `GATEWAY_DATASOURCE_POOL_SIZE` / `_MAX_OVERFLOW` | 否 | `5` / `5` | 每个业务数据源引擎的连接池大小（1-200 / 0-200）；单数据源最坏情况为两者之和 |
| `GATEWAY_DATASOURCE_POOL_RECYCLE_SECONDS` | 否 | `3600` | 业务连接超过该秒数后重建（0-86400）；`0` 表示不回收 |
| `GATEWAY_MYSQL_EXPORT_ENABLED` | 否 | `false` | 开启 MySQL 导出通道（[临时导出账号](export-credential.zh-CN.md)）；关闭时不监听、不加载协议层 |
| `GATEWAY_MYSQL_EXPORT_HOST` / `_PORT` | 否 | `0.0.0.0` / `3307` | 容器内监听地址/端口；端口不得与 `GATEWAY_PORT` 相同 |
| `GATEWAY_MYSQL_EXPORT_ALLOW_CIDRS` | 否 | 空 | 导出通道来源白名单的**首次引导值**；设置页保存过则以 DB 为准（[来源 IP 白名单](admin-ui.zh-CN.md#51-来源-ip-白名单)）。两边都空 = 拒绝所有导出连接，进程照常启动 |
| `GATEWAY_MYSQL_EXPORT_MAX_CONCURRENT` / `_MAX_ROWS` / `_MAX_SECONDS` | 否 | `2` / `1000000` / `600` | 并发、行数与时长上限（[配额与中止](export-credential.zh-CN.md#64-配额与中止)） |
| `GATEWAY_MYSQL_EXPORT_MAX_TTL_HOURS` | 否 | `72` | 签发凭证的有效期上限 |
| `GATEWAY_MYSQL_EXPORT_FETCH_BATCH` | 否 | `10000` | 每次从上游 `fetchmany` 的行数（100-1000000） |
| `GATEWAY_MYSQL_EXPORT_ADVERTISE_HOST` | 自动签发时必填 | 空 | 客户端可达的导出地址；留空且监听为通配地址时只截断不签发（6.6） |
| `GATEWAY_MYSQL_EXPORT_ADVERTISE_PORT` | 否 | `0` | `0` = 使用 `MYSQL_EXPORT_PORT`；端口映射与监听端口不一致时（如容器 3307、宿主 13307）必须显式配置 |
| `GATEWAY_EXPORT_BIND` | 否 | `127.0.0.1` | 仅 Compose 使用：导出端口绑定的宿主网卡 IP，必须显式填内网 IP |
| `GATEWAY_MCP_LANGUAGE` | 否 | `zh` | MCP 指令与工具描述的语言，`zh` 或 `en`；管理端语言跟随浏览器/Cookie，不受此项影响 |
| `GATEWAY_MCP_RESULT_MAX_ROWS` | 否 | `2000` | MCP `execute_sql` 返回给 AI 的行数软上限，`0` = 不限制（6.6） |
| `GATEWAY_MCP_RESULT_MAX_BYTES` | 否 | `2000000` | MCP 结果的 JSON 序列化 UTF-8 字节上限，`0` = 不限制 |
| `GATEWAY_MCP_QUERY_TIMEOUT_SECONDS` | 否 | `30` | 单条 MCP SQL 的实际执行时限（0-3600 秒，`0` = 不限制）；超时后上游语句被 KILL，连接被丢弃 |
| `GATEWAY_MCP_WRITE_MAX_AFFECTED_ROWS` | 否 | `1000` | UPDATE/DELETE 影响行数超过该值时整体回滚并返回 `WRITE_LIMIT_EXCEEDED`；`0` = 不限制 |
| `GATEWAY_MCP_MAX_CONCURRENT` | 否 | `32` | 网关整体在途 MCP 工具调用上限（1-1000）；超限立即返回 `TOO_MANY_CONCURRENT`，不排队 |
| `GATEWAY_MCP_MAX_CONCURRENT_PER_TOKEN` | 否 | `4` | 单个 Token 的在途 MCP 调用上限（0-1000）；`0` 表示只保留网关全局上限 |
| `GATEWAY_MCP_RATE_LIMIT_PER_MINUTE` | 否 | `120` | 单个 Token 在固定 60 秒窗口内的调用配额（0-100000）；超限返回 `RATE_LIMITED`；`0` 表示关闭 |
| `GATEWAY_MCP_EXPORT_AUTO_ISSUE` | 否 | `true` | 结果超限时自动签发临时导出凭证；导出通道未启用时该项不生效 |
| `GATEWAY_MCP_EXPORT_TTL_SECONDS` | 否 | `1800` | 自动签发凭证有效期（秒，60-86400），不得超过 `MYSQL_EXPORT_MAX_TTL_HOURS * 3600` |
| `GATEWAY_MCP_EXPORT_MAX_ACTIVE` | 否 | `3` | 同 Token + 数据源的在途自动凭证上限，超出时先撤销最旧的自动凭证；手工签发的凭证不受影响 |
| `GATEWAY_EXCHANGE_TTL_MINUTES` | 否 | `15` | 短期凭证缺省有效期（分钟，1-1440），见[短期凭证换发](exchange-credential.zh-CN.md) |
| `GATEWAY_EXCHANGE_MAX_TTL_MINUTES` | 否 | `60` | 换发请求可指定的有效期上限（分钟，1-1440）；不得小于缺省值 |
| `GATEWAY_MCP_ALLOW_CIDRS` | 否 | 空 | MCP 入口来源白名单的首次引导值；同样在设置页维护。**与导出通道口径相反**：DB 和环境变量都没配过时不限制来源（升级不会挡掉已接入的调用方），配过之后白名单外一律 403 |
| `GATEWAY_TRUSTED_PROXIES` | 否 | 空 | 逗号分隔的代理网段；只有 TCP 对端落在其中才采信 `X-Forwarded-For`，并自右向左取第一个非可信跳。直接对外暴露时留空，否则来源 IP 可被伪造 |

管理库的取值顺序：`GATEWAY_DB_URL` → `GATEWAY_DB_HOST` 等 MySQL 简写 → `GATEWAY_DB_PATH` 指向的 SQLite 文件。SQLite 只适合单实例、低并发；多副本或生产部署请使用外部 MySQL（或 PostgreSQL）。MySQL 管理库启动时按文件名顺序重放 `migrations/*.sql`，账号需要建表权限；其他后端由 ORM 元数据建表。已实测 MySQL 8.0（两种写法）与 PostgreSQL 16 + `asyncpg` 0.32。

`.env` 只放在部署机，权限建议 `chmod 600`，不要提交到 Git。
