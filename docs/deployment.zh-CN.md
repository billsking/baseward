# 部署与安全

[English](deployment.md) | [简体中文](deployment.zh-CN.md)

- **业务库账号最小权限**：只读数据源使用只有 `SELECT/SHOW` 的账号；可写源也只授予必要表的 DML。策略引擎不是数据库权限的替代品。
- **生产必须上 HTTPS**：网关自身只监听 HTTP，前面用内网反向代理终止 TLS，并设置 `GATEWAY_COOKIE_SECURE=true`。Token 与 SQL 会经过网络，明文 HTTP 只允许在隔离的内网调试环境。
- **主密钥备份**：`GATEWAY_MASTER_KEY` 丢失后已保存的数据源密码不可恢复；不要与数据库备份放在同一位置。
- **审计含完整 SQL 字面量**：审计库属于敏感数据，需限制数据库访问、加密备份。审计保留期默认 180 天，每日 03:00（Asia/Shanghai）自动清理，也可在管理端手动清理。
- **日志**：应用日志不记录 Authorization、Token 明文、密码、解密后的数据源密码与查询结果值；数据库原始错误只写入仅管理员可见的审计详情。
- **单进程**：MCP 与管理端共享一个 uvicorn 进程；连接池按数据源隔离。池大小、溢出与回收时间默认分别为 5、5、3600 秒，可通过 `GATEWAY_DATASOURCE_POOL_SIZE`、`GATEWAY_DATASOURCE_POOL_MAX_OVERFLOW`、`GATEWAY_DATASOURCE_POOL_RECYCLE_SECONDS` 调整。
- **容器最小权限**：compose 以非 root 用户运行，根文件系统只读（仅 `/data` 卷与 `/tmp` 可写），启用 `no-new-privileges` 并丢弃全部 Linux capabilities。
- **来源白名单的强度不一样**：导出通道取 TCP 对端地址，不可伪造；MCP 入口取 `X-Forwarded-For` 首个地址，网关直接对外时该头由调用方自己写、可伪造，只有在「客户端只能经反代访问」时才成立。两端白名单都可在设置页维护（[来源 IP 白名单](admin-ui.zh-CN.md#51-来源-ip-白名单)），`/admin` 不受限——所以配错一定能改回来。
- **威胁模型、安全边界与已知限制**：见 [SECURITY.zh-CN.md](../SECURITY.zh-CN.md)。

## 首次部署验收

1. `docker compose up -d --build`，网关容器正常运行（默认使用容器内 SQLite 管理库；生产环境应改用外部 MySQL，见[环境变量](configuration.zh-CN.md)）。
2. `curl http://127.0.0.1:8080/healthz` 返回 `{"status":"ok"}`。
3. 管理端登录成功；错误密码被拒绝且不建立会话。
4. 新建一个测试数据源（连接成功才保存）。
5. 创建 Token，用 MCP 客户端执行 `search_objects` 与 `SELECT 1`。
6. 对只读源执行 `INSERT`，确认被拒绝且审计状态为 `denied`。
7. 对含 `phone` 列的表执行查询，确认结果已脱敏；审计中可查到该请求。

## 已知限制

- MCP 语句超过 `GATEWAY_MCP_QUERY_TIMEOUT_SECONDS`（默认 30 秒）会超时，UPDATE/DELETE 影响行数超过 `GATEWAY_MCP_WRITE_MAX_AFFECTED_ROWS`（默认 1000）会回滚。结果行数/字节上限会截断超限响应，但 **SELECT 没有上游硬行数上限**：网关不会注入 `LIMIT`，因此仍建议在业务库侧设置 `max_execution_time` / `max_statement_time`，并用审计的 `duration_ms`、`row_count` 监控。
- 脱敏会沿嵌套派生表、CTE、`UNION` 分支、别名以及命名列与 `*` 混合投影追踪列血缘。支持范围之外的表达式仍使用保守规则合并或自动关键词兜底，可能过度脱敏。
- 不解析存储过程/函数内部逻辑；数据库账号权限仍是最后一道边界。
- 每个用户同时只能有一个未过期的启用 Token。创建时可设置可选过期时间；过期 Token 及其派生凭证都会被拒绝。停用立即生效，管理端列表展示过期时间与最近使用时间。不支持自动轮换或同一用户并行使用多个有效 Token。
- Token 授权是数据源/库/表/列白名单，不做行级权限；字段受限时无法解析归属的 SQL 会被拒绝（宁可拒绝，不放宽）。
- 一个数据源仍对应一台主机 + 一套连接账号，多个库靠可访问库清单表达；不做跨实例的逻辑聚合（跨实例请拆成多个数据源，Token 可同时授权多个）。
- 授权选择器的表清单上限 2000、字段清单上限 5000；超出时只展示前 N 项（接口返回 `truncated=true`），可直接在编辑页保留原本已选对象。
- 管理端会话空闲 30 分钟自动失效（`GATEWAY_ADMIN_SESSION_IDLE_MINUTES` 可调），绝对有效期 8 小时；改密会立即使所有旧会话失效。
- 主密钥一期不支持轮换。
- 默认 SQLite 管理库只适合单实例、低并发：写入串行化，多副本会各自持有一份数据；生产环境请切换到外部 MySQL。非 MySQL 管理库由 ORM 元数据建表，升级时只补充缺失列，不重放迁移。
