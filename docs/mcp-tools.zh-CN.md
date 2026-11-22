# MCP 工具

[English](mcp-tools.md) | [简体中文](mcp-tools.zh-CN.md)

所有工具的 `datasource` 参数都支持**精确名称**或**唯一业务描述片段**：数据源备注（管理端「备注」）可作为别名，例如备注为“示例业务库”时可直接传 `"示例业务库"`；匹配到多个数据源时返回 `DATASOURCE_AMBIGUOUS` 和候选清单。

推荐调用顺序：`list_datasources` → `describe_datasource`（或 `search_objects`）→ `execute_sql`，避免多轮试探。

### list_datasources

无参数，返回当前 Token 可访问的启用数据源：

```json
{"request_id": "...", "count": 1,
 "datasources": [{"name": "demo", "description": "示例业务库", "default_database": "demo",
                  "is_read_only": true,
                  "databases": [{"name": "demo", "read_only": true}, {"name": "report", "read_only": false}]}]}
```

- `databases[]` 是该 Token 在该数据源下的**可用库**（可访问 ∩ 已授权），`read_only` 为合并了数据源总开关的**有效**策略；Token 在该数据源下无任何可用库时为 `[]`。
- `is_read_only` 保留但降级为**数据源聚合字段**（总开关只读，或全部可用库都只读），不随 Token 变化；判断单个库能否写请看 `databases[].read_only`。

### describe_datasource

一次返回表结构，避免“先查表、再查字段”两轮：

```json
{"datasource": "示例业务库", "pattern": "批次 卡密", "include_columns": true, "limit": 50}
```

- `pattern` 可选（≤128 字符），空格分隔多词时需同时满足，匹配表名、表注释、列名、列注释。
- `database` 可选，限定库名；不传时在**全部可用库**内检索。传了未暴露的库返回 `DATABASE_NOT_EXPOSED`，已暴露但该 Token 无任何授权返回 `DATABASE_DENIED`（两者都不返回空结果，避免误判为「这个库没有表」）。
- `include_columns=false` 只返回表清单，适合大库。
- `limit` 表数量上限 1-100（默认 50）；每表最多返回 100 个字段，总字段上限 3000，超出时 `truncated=true`。
- 返回 `description`、`default_database`、`is_read_only`（聚合字段）、`databases[]`（可用库与有效 `read_only`）与 `tables[]`；每张表含 `database/table/comment/columns[]`，字段含 `name/data_type/nullable/primary_key/comment`。

### search_objects

```json
{"datasource": "demo", "pattern": "order", "object_type": "all", "database": null}
```

- `pattern` 1-128 字符，空格分隔多词时需同时满足；匹配表名、列名、表注释与列注释（`% _ \` 已转义）。
- `object_type` 取 `all/table/column`。
- `database` 可选，语义与拒绝口径同 `describe_datasource`：不传检索全部可用库，未暴露/未授权分别返回 `DATABASE_NOT_EXPOSED` / `DATABASE_DENIED`。响应回显 `database` 与 `databases[]`。
- 结果带 `comment`，按相关度排序（名称完全匹配 > 前缀 > 子串 > 注释），最多 200 条，超出时 `truncated=true`。

### execute_sql

```json
{"datasource": "demo", "sql": "SELECT id, phone FROM users WHERE id = 1", "database": "report"}
```

- `database` 可选：本次执行的**有效默认库**，只决定未写库名的表落在哪个库，**不扩大授权范围**；不传时用数据源默认库。SQL 里出现的每个 `库.表` 仍逐一通过暴露与授权校验。
- 传了未暴露的库返回 `DATABASE_NOT_EXPOSED`，已暴露但该 Token 无该库授权返回 `DATABASE_DENIED`；两者消息只含库名，不出现表结构。
- 上游返回 1146（表不存在）且 SQL 有未限定库名的表时，网关在其他已授权可用库里查找同名表，并在错误消息末尾追加候选库名，**不会自动切换库**。

返回行是数组的数组（避免重名列丢失）：

```json
{
  "request_id": "1b2d...",
  "datasource": "demo",
  "database": "report",
  "statement_type": "Select",
  "columns": ["id", "phone"],
  "rows": [[1, "138****1234"]],
  "row_count": 1,
  "duration_ms": 12,
  "masked_columns": ["phone"],
  "truncated": false,
  "truncated_reason": null,
  "hint": null,
  "export": null
}
```

写操作返回 `columns=[]`、`rows=[]`，`row_count` 为受影响行数，同样带 `truncated=false` 等四个字段；结果超限时的截断、`hint` 与 `export` 见 6.6。

错误以 MCP `isError=true` 返回，文本为 JSON：

```json
{"error": {"code": "WRITE_ON_READONLY", "message": "数据源 demo 为只读，禁止写操作", "request_id": "..."}}
```

工具执行、准入控制和缺失身份错误都以 MCP `isError=true` 响应携带这段 JSON。HTTP 鉴权拒绝与 `/api/exchange` 错误也使用相同的 `{"error": {"code", "message", "request_id"}}` 响应体，并返回相应的 HTTP 状态码。`request_id` 与 HTTP 响应的 `X-Request-ID`、该次调用的审计记录、日志行里的 `request_id` 完全一致，一次失败的 Agent 调用可以端到端串联。也可以自行传入 `X-Request-ID`（字母、数字、`.`、`_`、`:`、`-`，最长 128 字符）以延续上游链路 id。

### issue_export_credential

```json
{"datasource": "demo", "sql": "SELECT id, phone FROM users WHERE ...", "reason": "导出某商户 2026-10 订单", "database": "report"}
```

已知结果集会超过返回上限、或 `execute_sql` 会超时时，在跑脚本前直接换一张短期 MySQL 只读凭证：

```json
{
  "request_id": "9c1e...",
  "protocol": "mysql",
  "host": "192.0.2.12",
  "port": 3307,
  "user": "exp_9f3a1c7b2d85",
  "password": "<只在本响应中出现一次>",
  "database": "demo",
  "source_ip": "192.168.1.20",
  "expires_at": "2026-10-28T07:30:00Z",
  "sql": "SELECT id, phone FROM users WHERE ...",
  "notice": "该凭证仅用于本次导出...",
  "sample_code": "import csv\nimport os\n..."
}
```

- `sql` 必填：签发前按该 Token 的授权范围与只读策略预检，但不执行；写语句、DDL、越权直接拒绝。
- `reason` 必填（≤200 字符）：写入审计，用于区分主动申请和截断兜底。
- `database` 可选：凭证的**目标库**，不传时取数据源默认库；返回的 `database` 是实际生效的目标库，凭证签发即固化。
- 有效期固定为 `GATEWAY_MCP_EXPORT_TTL_SECONDS`；同一 Token + 数据源的活跃凭证受 `GATEWAY_MCP_EXPORT_MAX_ACTIVE` 限制。
- `source_ip` 是本次 MCP 请求被网关看到的来源 IP，取数脚本必须从同一地址连接（6.7）；MCP 侧不允许调用方指定绑定值。
- 不要把返回的密码写进代码或配置文件；脚本从 `GATEWAY_EXPORT_PASSWORD` 环境变量读取（见 `sample_code`）。
- 通道未启用或未配置 `ADVERTISE_HOST` 时返回 `EXPORT_UNAVAILABLE`；参数非法返回 `INVALID_ARGUMENT`。

### 并发与限流

五个工具（包括元数据类工具）都经过同一套准入控制：

- `GATEWAY_MCP_MAX_CONCURRENT` 限制网关整体的在途工具调用数。
- `GATEWAY_MCP_MAX_CONCURRENT_PER_TOKEN` 限制单个 Token 的在途调用数；`0` 表示不限制单 Token，只保留全局上限。
- `GATEWAY_MCP_RATE_LIMIT_PER_MINUTE` 限制单个 Token 在固定 60 秒窗口内的调用次数；`0` 表示关闭。

超过上限的调用立即返回 `TOO_MANY_CONCURRENT` 或 `RATE_LIMITED`，不会排队等待，避免一个慢查询把后续请求全部堵在网关里。被限流的调用只记日志、不写审计：为每次拒绝写一行审计，反而会放大限流本来要卸载的负载。

### Token 授权范围

- **四级白名单**：数据源 → 库 → 表 → 字段。任一层留空表示该层不限制；选下级必须包含上级；表被选中但未勾选字段时，该表字段不受限。
- 任何一层的「全部」都不越过该数据源的可访问库；数据源级「全部表」等价于所有可访问库都设为「全部表」。
- 库层有三种状态：全部表 / 指定表 / 不允许。库级「全部表」条目上限 512，超出时提示改用数据源级「全部表」。
- 字段受限时，`execute_sql` 会校验投影、`WHERE/JOIN/GROUP BY/HAVING/ORDER BY` 以及写语句的 `SET/WHERE/INSERT` 列；`SELECT *` / `t.*` 自动展开为允许字段后再执行。无法确定字段归属或读取表结构失败时直接拒绝，不放过。
- `search_objects` / `describe_datasource` 只返回该 Token 可用库内的表与字段。
- 编辑授权范围立即生效且不轮换 Token 明文；停用、删除 Token 的行为不变。
- 授权只做数据源/库/表/列白名单，不做行级过滤（不会自动注入 WHERE 条件）。

### 错误码

`missing_token` / `invalid_token`（HTTP 401）、`auth_unavailable`（HTTP 503）、`INVALID_ARGUMENT`、`DATABASE_NOT_EXPOSED`、`DATABASE_DENIED`、`DATASOURCE_NOT_FOUND`、`DATASOURCE_AMBIGUOUS`、`DATASOURCE_DISABLED`、`DATASOURCE_DENIED`、`TABLE_DENIED`、`FIELD_DENIED`、`FIELDS_REQUIRED`、`FIELD_SCOPE_CHECK_FAILED`、`EMPTY_SQL`、`SQL_PARSE_ERROR`、`MULTI_STATEMENT`、`DDL_BLOCKED`、`UNSUPPORTED_STATEMENT`、`LOCKING_READ`、`INTO_OUTFILE`、`WRITE_ON_READONLY`、`NO_WHERE`、`QUERY_TIMEOUT`、`WRITE_LIMIT_EXCEEDED`、`RATE_LIMITED`、`TOO_MANY_CONCURRENT`、`EXPORT_UNAVAILABLE`、`CREDENTIAL_ISSUE_DENIED`、`AUDIT_UNAVAILABLE`、`DB_UNAVAILABLE`、`DB_ERROR`、`INTERNAL_ERROR`。

`NO_WHERE` 同时覆盖 WHERE 恒真的 UPDATE/DELETE（如 `WHERE 1=1`、`WHERE TRUE`）。`QUERY_TIMEOUT` 表示语句超过 `GATEWAY_MCP_QUERY_TIMEOUT_SECONDS` 并被上游 KILL；`WRITE_LIMIT_EXCEEDED` 表示 UPDATE/DELETE 影响行数超过 `GATEWAY_MCP_WRITE_MAX_AFFECTED_ROWS`，事务已回滚。`RATE_LIMITED` 表示当前 Token 用完了每分钟配额，`TOO_MANY_CONCURRENT` 表示网关或该 Token 的在途调用已达上限；两者都是瞬时错误，短暂等待后重试即可。

`DATABASE_NOT_EXPOSED` 与 `DATABASE_DENIED` 的消息只含库名、不含表结构：前者说明该库没被管理员勾进数据源的可访问库，后者说明库已暴露但 Token 没有该库授权。
