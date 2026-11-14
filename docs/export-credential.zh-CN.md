# 临时导出账号（MySQL 协议）

[English](export-credential.md) | [简体中文](export-credential.zh-CN.md)

给脚本/程序做大结果集导出：管理员在管理端签发一个临时 MySQL 账号，消费方用标准 MySQL 客户端直连网关的导出端口，由网关按批从上游取行并流式回写。关闭时（默认）不监听端口、不加载协议层。

## 6.1 启用

```bash
# .env（首次引导值；之后在管理端设置页维护，DB 优先 —— 见「管理端 → 来源 IP 白名单」）
GATEWAY_MYSQL_EXPORT_ENABLED=true
GATEWAY_MYSQL_EXPORT_ALLOW_CIDRS=192.168.1.0/24,192.168.1.20
GATEWAY_EXPORT_BIND=192.168.1.10   # 部署机内网网卡 IP，导出端口只绑这一张网卡
```

- **白名单是唯一网络层控制**（v1 无 TLS）：取值来源是 TCP 对端地址，不可伪造；空名单不等于「不限制」，而是拒绝所有导出连接。已去掉「开启导出而白名单为空则拒绝启动」的启动门禁（见 [来源 IP 白名单](admin-ui.zh-CN.md#51-来源-ip-白名单)），兜底改到连接期。
- compose 的端口映射必须写成 `<内网网卡IP>:3307:3307`，**不要写 `3307:3307`**（等价 `0.0.0.0:3307`，公网可达）。安全组也只对消费方网段放行 3307。
- 白名单是应用层判定，端口绑定/安全组是网络层暴露面，两者都要收；生产机是公网 IP 时这条是硬要求。
- 导出走数据源连接池的上游连接，与 MCP 共用同一套策略、脱敏与审计。

## 6.2 签发与连接

管理端「导出凭证 → 签发」选择 Token、数据源、**目标库**与有效期（不超过 `GATEWAY_MYSQL_EXPORT_MAX_TTL_HOURS`），并确认「允许来源 IP」——默认填当前申请页的请求 IP，凭证要交给别的机器上的脚本时改成那台机器的出口 IP。账号形如 `exp_a1b2c3d4e5f6`，明文密码只在签发页展示一次，库里只存 `auth_string`（摘要）。

- **目标库候选 = 该 Token 在该数据源下的可用库**（可访问库 ∩ 已授权库）。默认选中数据源的默认库（若它是可用库），否则自动选第一个可用库；无候选时下拉为空、禁用提交并提示先调整 Token 授权，服务端同样拒绝签发。
- **目标库签发即固化**：连接建立后落在该库；之后修改数据源默认库不影响已签发的凭证。要换库就撤销重签（不提供修改与续期）。
- 同一数据源下的**其他可用库**可以在连接后 `USE`（判定与限制见 6.8）；其他数据源需分别签发。
- 目标库被移出可访问范围后凭证不会自动撤销，但连接后的第一条语句即返回 `DATABASE_NOT_EXPOSED`（1235）并写 denied 审计。

```bash
mysql --quick -h <gateway-host> -P 3307 -u exp_a1b2c3d4e5f6 -p'<签发时的明文密码>' <目标库>
```

```python
# SQLAlchemy + PyMySQL：必须开 stream_results，否则整段结果先在客户端内存里堆起来
engine = create_engine("mysql+pymysql://exp_a1b2c3d4e5f6:<password>@<host>:3307/<目标库>?charset=utf8mb4")
with engine.connect() as conn:
    result = conn.execution_options(stream_results=True, yield_per=1000).exec_driver_sql(sql)
    for row in result:
        ...
```

```java
// JDBC：必须开服务端游标，否则整段结果先在客户端内存里堆起来
String url = "jdbc:mysql://<host>:3307/<目标库>?useSSL=false&allowPublicKeyRetrieval=true"
           + "&useCursorFetch=true&defaultFetchSize=1000";
```

```javascript
// mysql2（Node.js）：用流式查询，不要 await 整段结果
const conn = mysql.createConnection({host, port: 3307, user, password, database: "<目标库>"});
conn.query('SELECT id, label FROM big_table').stream().on('data', (row) => { /* ... */ });
```

> 连接串里的库名只是握手提示：网关会用凭证目标库覆盖会话当前库（见 6.8），**改 URL 里的库名不会切换会话**。要换库请显式 `USE`。

## 6.3 客户端必须用流式游标（`useCursorFetch`）

网关按 `GATEWAY_MYSQL_EXPORT_FETCH_BATCH` 从上游取行、按批写给客户端，服务端内存有界；但如果客户端驱动不开启服务端游标，会把**全部结果缓冲在客户端进程里**，大表导出会 OOM：

| 客户端 | 开启方式 |
| --- | --- |
| MySQL CLI | 加 `--quick`（默认会把整个结果集缓冲到客户端内存，大表导出不要省这个参数） |
| JDBC / Connector/J | URL 加 `useCursorFetch=true&defaultFetchSize=1000`，并对 Statement 调 `setFetchSize(1000)` |
| PyMySQL | `cursor(pymysql.cursors.SSCursor)`（或 `SSCursor(conn)`） |
| SQLAlchemy | `execution_options(stream_results=True, yield_per=1000)` |
| Node mysql2 | `conn.query(sql).stream()` |

服务端支持预处理语句与分批 `COM_STMT_FETCH`（含跨批游标），JDBC 的 `useCursorFetch` 正是走这条路。

## 6.4 配额与中止

| 限制 | 环境变量 | 默认 | 超限表现 |
| --- | --- | --- | --- |
| 并发导出 | `GATEWAY_MYSQL_EXPORT_MAX_CONCURRENT` | 2 | 1040，多出的请求直接拒绝、不排队 |
| 最大行数 | `GATEWAY_MYSQL_EXPORT_MAX_ROWS` | 1000000 | 1105 `GATEWAY:EXPORT_ROW_LIMIT` |
| 最长时长 | `GATEWAY_MYSQL_EXPORT_MAX_SECONDS` | 600 | 1105 `GATEWAY:EXPORT_TIME_LIMIT` |
| 凭证有效期 | `GATEWAY_MYSQL_EXPORT_MAX_TTL_HOURS` | 72 | 过期后 1045 |
| 客户端断开 | — | — | 停止生成并 `KILL QUERY` 上游，审计 `interrupted` |

所有错误包都带 `request_id=`，可与管理端审计对照；数据库原始错误只写审计详情，不返回给客户端。

## 6.5 已知行为

1. **单条语句**：多语句返回 1064；`SELECT ... INTO OUTFILE/DUMPFILE` 也是 1064（sqlglot 无法解析该语法），`SELECT ... INTO @var` 返回 1235。
2. **只读白名单**：仅 SELECT/SHOW/DESCRIBE/EXPLAIN 等读语句；写语句、DDL、GRANT、CALL 一律 1235。
3. **目标库固定**：连接后的会话当前库是凭证目标库（签发时固化）；连接串/URL 里的库名会被网关覆盖，改它不会换库。`USE` 其他库的规则见 6.8。
4. **元数据按范围过滤**：`SHOW TABLES`、`DESCRIBE`、`SHOW COLUMNS`、`SHOW DATABASES`、`INFORMATION_SCHEMA`、`COM_FIELD_LIST` 六个入口都按签发时的表/字段白名单过滤，范围外对象不会出现。
5. **脱敏沿用数据源策略**：脱敏列在导出通道上的列类型为 `VAR_STRING`，值与 MCP 返回一致。
6. **配额与上限**：并发、行数、时长、TTL 见 6.4；缓冲执行（未开游标）同样受行数/时长上限约束。
7. **断开、撤销与过期**：客户端断开即停止生成并清理上游查询；凭证撤销或过期后新连接 1045，已建立的连接在下一条语句被拒。
8. **无 TLS、不支持 KILL**：v1 靠来源 IP 白名单（6.1）加凭证来源绑定（6.7）控制访问，客户端发 `KILL` 被拒绝；导出与 MCP 同进程，每 50 行让出一次事件循环、每 1000 行 drain 一次，避免长时间按住 MCP 请求。

## 6.6 MCP 大数据量自动交接（v1.5）

`execute_sql` 的一次只读查询超过返回上限时，网关**不会报错**，而是截断成预览、并（条件满足时）自动签发一张短时凭证交给 AI 自己写代码取全量。

**两层上限**

| 上限 | 配置 | 实现方式 | 触发后 |
|------|------|---------|--------|
| 行数 | `GATEWAY_MCP_RESULT_MAX_ROWS`（默认 2000） | SQL 层追加 `LIMIT cap+1`，多取的一行只用于判定截断 | `truncated_reason="rows"` |
| 字节 | `GATEWAY_MCP_RESULT_MAX_BYTES`（默认 2 MB） | 脱敏后逐行 JSON 序列化的 UTF-8 字节数累计 | `truncated_reason="bytes"` |

- 两者同时命中记 `"rows"`（行数先判）；任一配 `0` 即关闭该维度，两个都配 `0` 时行为与 v1.4 完全一致。
- 用户 SQL 自带更小 `LIMIT` 时不追加、也不判截断；`SHOW`/`DESCRIBE`/`EXPLAIN` 等非 `SELECT`/`UNION` 语句不追加 `LIMIT`，只受字节上限约束。
- 行数上限是**软上限**（`LIMIT` 追加在最终查询上，可被聚合/子查询语义绕过一部分），字节上限是硬兜底。导出通道自己的行数/时长上限（6.4）与这里无关。

**启用交接**（三件事同时满足才会签发）

```bash
GATEWAY_MYSQL_EXPORT_ENABLED=true
GATEWAY_MYSQL_EXPORT_ADVERTISE_HOST=192.0.2.12   # 客户端可达的地址，不能用 0.0.0.0 推断
GATEWAY_MCP_EXPORT_AUTO_ISSUE=true              # 默认开
```

**显式申请**：已知结果集会超限、或 `execute_sql` 会超时（根本拿不到截断响应）时，Agent 可以直接调用
`issue_export_credential(datasource, sql, reason)` 先换凭证再跑脚本。签发前会按该 Token 的数据源/表/
字段授权与只读策略预检 `sql`（不执行），写语句、越权或通道未启用都会被拒绝；审计为
`mcp_tool`（tool=`issue_export_credential`）+ `credential_lifecycle`（tool=`issue_explicit`）。
隐式截断交接仍然保留，作为 Agent 未主动调用时的兜底。

**返回字段**（其余字段与 7 章 `execute_sql` 一致）

```json
{
  "truncated": true,
  "truncated_reason": "rows",
  "hint": "结果已截断，请用下方 export 凭证通过 MySQL 协议取全量数据。",
  "export": {
    "protocol": "mysql",
    "host": "192.0.2.12",
    "port": 3307,
    "user": "exp_9f3a1c7b2d85",
    "password": "<只在本响应中出现一次>",
    "database": "demo",
    "source_ip": "192.168.1.20",
    "expires_at": "2026-10-28T07:30:00Z",
    "sql": "SELECT id, phone, created_at FROM users",
    "notice": "该凭证仅用于本次导出，请勿写入代码仓库或配置文件；凭证绑定 source_ip（本次 MCP 请求被网关看到的来源 IP），取数脚本要从同一来源地址连接，否则握手会被拒绝；取数完成后可忽略，到期自动失效。",
    "sample_code": "import csv\nimport os\n\nimport pymysql\n..."
  }
}
```

- `export.sql` 是**用户原始 SQL**（不含网关追加的 `LIMIT`，也不含 `SELECT *` 展开）——导出通道会自己重走一遍策略、授权改写与脱敏。
- `sample_code` 用 `pymysql` 的 `SSCursor` 流式写 CSV，密码从环境变量 `GATEWAY_EXPORT_PASSWORD` 读取，不写进生成的文件。
- 凭证能力**严格等于所属 Token**（含三级白名单），有效期默认 30 分钟；同一 Token + 数据源的自动凭证超过 `GATEWAY_MCP_EXPORT_MAX_ACTIVE` 时先撤销最旧的一条，管理员手工签发的凭证永远不受影响。
- `export.source_ip` 是这张凭证绑定的来源地址，取数脚本必须从同一 IP 发起 TCP 连接（见 6.7）。MCP 侧无法指定，只能原样透传。
- 不会截断的查询（含用户自带更小 `LIMIT`、或两个上限都关闭）四个新字段固定为 `false` / `null` / `null` / `null`。

**无法签发时只给 `hint`**（`export=null`，`execute_sql` 仍是成功返回）

| 情形 | `hint` |
|------|--------|
| `GATEWAY_MCP_EXPORT_AUTO_ISSUE=false` | 结果已截断（{上限}）。如需全量数据，请联系管理员签发临时导出凭证，或缩小查询范围（加 WHERE / 调小 LIMIT）。 |
| 导出通道未启用 | 结果已截断。导出通道未启用，请缩小查询范围后重试，或联系管理员。 |
| 未配置 `ADVERTISE_HOST`（或监听为通配地址） | 结果已截断。导出通道地址未配置，请缩小查询范围后重试，或联系管理员。 |
| 签发失败（Token/数据源停用、不在授权范围、管理库写入失败） | 结果已截断。自动签发导出凭证失败（{code}），请缩小查询范围后重试，或联系管理员。 |

审计上是两条记录：主查询一条 `mcp_tool`（`success`，`row_count` 为实际返回行数），凭证签发一条 `credential_lifecycle`（`tool=issue_auto`，`deny_reason` 形如 `mcp:gw_xxxx req=<主请求 request_id>`，可据此关联）。凭证明文不落库、不进日志、不进审计，只出现在这一次响应体里。

## 6.7 凭证来源 IP 绑定（v1.6）

每张凭证都带一个 `source_ip`，握手时比对 TCP 连接的来源地址，**不一致直接 1045**（与其余凭证类拒绝同码，具体原因只进审计 `CREDENTIAL_IP_MISMATCH`）。三条签发路径都绑：

| 签发路径 | 绑定值 |
| --- | --- |
| MCP 截断自动交接 | 本次 MCP 请求的来源 IP |
| MCP `issue_export_credential` | 本次 MCP 请求的来源 IP（调用方不可指定） |
| 管理端签发 | 表单「允许来源 IP」，留空取申请页请求 IP |

- 口径是**精确匹配**，不做 CIDR 容忍；`::ffff:192.0.2.1` 与 `192.0.2.1` 先归一再比，双栈监听不会误拒。
- 与 6.1 的白名单是两层：白名单管哪些网段能进导出端口（在网络层拦截、认证之前），`source_ip` 管这张凭证只能被哪一个地址使用（撤销/收敛粒度到单张凭证）。
- 取不到申请 IP 时**拒绝签发**（fail-closed），不会退化成不绑定。
- 迁移前的存量凭证 `source_ip` 为 NULL，视为未绑定、不校验来源，到期或被撤销后自然收敛（上限 `GATEWAY_MYSQL_EXPORT_MAX_TTL_HOURS`）；不留常驻开关。
- **部署注意**：MCP 侧记录的是网关看到的来源 IP。若 MCP 前面还有反代且没传 `X-Forwarded-For`，记录到的是反代地址，脚本从别处连就会被拒——排查顺序：签发响应/管理端列表里的 `source_ip` → 反代是否透传 `X-Forwarded-For` → 脚本所在机器的实际出口 IP。管理端签发可以把 `source_ip` 手动改成脚本的出口 IP。
- 同一台机器上也要用**同一个地址**：连 `127.0.0.1:3307` 时网关看到的来源是 `127.0.0.1`，连内网 IP `:3307` 时看到的是那张网卡的 IP。MCP 请求走 `http://127.0.0.1:8000/mcp`、取数脚本却连内网地址，两者来源不同即被拒（冒烟脚本的 `--export-host` 同理）。

## 6.8 多库会话与 `USE`（v1.8）

- 凭证登录后会话当前库 = 目标库。**连接串里的库名会被覆盖**：mysql-mimic 握手时先把库名写进会话，随后 `init()` 用凭证目标库覆盖它——在 JDBC URL / PyMySQL `database=` 里改库名看起来生效了，实际没有。换库只有两条路：签发时选目标库，或连接后显式 `USE`。
- `USE <目标库>`：只要求该库仍在数据源的**可访问库**清单里（Token 授权在签发时已校验，对象级授权每条 SQL 仍逐一判定）。
- `USE <其他库>`：必须同时满足**已暴露**与**该 Token 已授权**，否则拒绝：
  - 库已被移出可访问清单 → 1235 `DATABASE_NOT_EXPOSED`
  - 已暴露但该 Token 无该库任何授权 → 1235 `DATABASE_DENIED`
- `USE` 成功后，未限定表名的 SQL、策略与授权判定、脱敏规则、上游执行与审计都按这个当前库计算；元数据（`SHOW TABLES` / `DESCRIBE` / `COM_FIELD_LIST`）随当前库变化，schema 缓存按「数据源 + 库」重建。
- 连接时目标库已被移出可访问清单：登录成功，但下一条语句即 1235 `DATABASE_NOT_EXPOSED`（denied 审计记该库），不会被静默改到其他库。
- 导出通道恒只读，与库级读写策略无关。
