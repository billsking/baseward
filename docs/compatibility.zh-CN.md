# 引擎兼容性

[English](compatibility.md) | [简体中文](compatibility.zh-CN.md)

BaseWard 说的是 **MySQL 线协议和 MySQL SQL 方言**。任何能按该协议应答的都是候选上游，因此覆盖范围是 MySQL 家族，而不是某一个产品。

有两类结论被刻意分开：

- **协议兼容** —— BaseWard 能连接、读取元数据并流式返回结果。
- **策略验证** —— 默认拒绝、四级 Token 作用域、脱敏与 fail-closed 审计确实在该引擎上执行过，并在测试中做了断言。

MySQL 8.0、MariaDB 11.8、TiDB v8.5 和 OceanBase CE 4.4 今天同时满足这两点，各自都有一次完整集成套件的留档运行作为依据。OceanBase 需要先调高一项会话设置（`ob_query_timeout`，见实测发现）；其余引擎预期可用，但未验证。

## 状态

| 引擎 | 协议 | 策略已验证 | 状态 |
|---|---|---|---|
| MySQL 8.0 | 是 | **是**（8.0.46） | 参考引擎。集成套件在其上全绿通过。 |
| MariaDB 10.6+ / 11.x | 是（分支） | **是**（11.8.9） | `information_schema` 和 `SHOW` 与 MySQL 保持接近。10.x 未执行。 |
| TiDB（MySQL 模式） | 是 | **是**（v8.5.0，单节点） | 通过套件，存在下文所列差异项。不覆盖集群拓扑。 |
| OceanBase（MySQL 模式） | 是 | **是**（4.4.2.1） | 把 `ob_query_timeout` 调高到超过其 10 s 默认值后即通过套件。连接池在建连时强制执行 `SET autocommit=0`，该引擎的审计回滚保证需要这一点。见下文记录。 |
| MySQL 5.7, 8.4 | 是 | 否 | 预期可用。未执行。 |
| Amazon Aurora MySQL, RDS MySQL | 是 | 否 | 预期可用。托管 MySQL，方言相同。 |
| Percona Server for MySQL | 是 | 否 | 预期可用。未执行。 |
| PolarDB MySQL, PolarDB-X | 是 | 否 | 预期可用，存在下文所列差异项。没有可执行验证的镜像。 |
| TDSQL（MySQL 模式）, GoldenDB, GaussDB(for MySQL) | 是 | 否 | 预期可用，存在下文所列差异项。没有可执行验证的镜像。 |
| OceanBase（Oracle 模式）, openGauss / GaussDB, Dameng, KingBase | 否 | 否 | 不支持：方言不同，元数据字典也不同。 |
| PostgreSQL, SQL Server, Oracle, ClickHouse, SQLite | 否 | 否 | 不支持。PostgreSQL 在路线图上。 |

管理库（BaseWard 自身的元数据存储）与其背后引擎是两个独立的选择：管理库默认是本地 SQLite 文件，MySQL 8.0 和 PostgreSQL 16 是已测试的外部后端（见 [configuration.zh-CN.md](configuration.zh-CN.md)）。

## 实测记录

这里的「已实测」是指：`tests/integration/` 中的集成套件在该引擎上执行并通过，断言了 AST 默认拒绝、四级 Token 作用域、结果脱敏与 fail-closed 审计。下表每一行是一次运行。

| 日期 | 引擎 | 结果 | 环境 |
|---|---|---|---|
| 2026-11-09 | MySQL 8.0.46 | 243 passed | `mysql:8.0` 容器，Linux bridge 网络 |
| 2026-11-09 | MariaDB 11.8.9 | 243 passed | `mariadb:11` 容器 |
| 2026-11-09 | TiDB v8.5.0 | 243 passed | `pingcap/tidb` 容器，`--store unistore` 单节点 |
| 2026-11-09 | OceanBase CE 4.4.2.1 | 243 passed | `oceanbase/oceanbase-ce` 容器，单 zone，MySQL 模式，租户 `test`；本次运行把 `ob_query_timeout` 从其 10 s 默认值调高到 600 s，`ob_trx_timeout` 保持默认 |

四次运行是同一 commit 上的同一套件，在一台其余负载空闲的主机上逐引擎执行。套件共 243 个测试；在下文的建连时修复落地时新增了 `test_pool_session_has_autocommit_off`，套件从 242 增长而来。同一天更早的运行中 OceanBase 得分 240/242 —— 两个失败分别是 autocommit 缺口（现已修复）和 10 s 语句配额掐断了 200 000 行的造数夹具（本次运行已调高，且这仍是一个值得了解的真实默认值）。留档的 commit 早于下文描述的系统库排除修复；自那次改动以来，套件尚未在这四个引擎上重跑。

一次全绿运行不覆盖的覆盖边界：

- TiDB 运行在 **unistore 单机模式**：SQL 层、事务和元数据的行为与文档一致，但真实的 PD/TiKV 拓扑、region 级错误和多节点 DDL 未被演练。
- 单容器运行不演练复制、故障切换，也不演练引擎自身的权限与租户模型中超出测试账号恰好持有部分的内容。
- 运行时会在目标引擎上创建 BaseWard 的管理库 schema，因为套件需要一台一次性的服务器。这**不是**对管理库的支持声明——管理库已测试的后端是 SQLite、MySQL 8.0 和 PostgreSQL 16，不是 TiDB 或 OceanBase。
- 流式夹具曾用 MariaDB 的 `seq_1_to_N` 伪表造数，而 MySQL 拒绝该写法。现在改用普通的 `INSERT ... SELECT` 生成序列，因此本套件是引擎中立的：在非 MySQL 引擎上的失败属于兼容性发现，而不是测试缺陷。

## 运行中的实测发现

**OceanBase 保持 autocommit 开启，破坏了审计回滚保证（已在连接池修复）。** BaseWard 在 `engine.begin()` 内执行写入（`app/mcp/pipeline.py:258-263`）：如果审计行无法落定，从该块逃逸的异常正是让写入回滚的机制。MySQL 8.0.46、MariaDB 11.8.9 和 TiDB v8.5.0 在已连接会话上报告 `@@autocommit = 0`，因为客户端在握手阶段请求关闭 autocommit。OceanBase CE 4.4.2.1 报告 `@@autocommit = 1` —— 它不遵守该标志 —— 于是每条语句在执行完的瞬间即提交，回滚成为空操作，审计落定失败时写入原样保留，而审计行记为 `error`、调用方拿到 `AUDIT_UNAVAILABLE`。直接实测：一次 `UPDATE` 加强制回滚之后，MySQL 与 TiDB 上存储的值是 `orig`，OceanBase 上是 `changed`。

这是一条对外承诺的保证出现缺口，不是策略绕过：语句仍然经过解析、作用域限定、放行判定与审计。修复现已落在连接池 —— 每条业务连接建连时以 `SET autocommit=0` 作为其 `init_command`（`app/datasources/pool.py:114-119`），对本就遵守握手标志的引擎是无操作。在同一次 OceanBase 探测中，`init_command` 与建连时事件两种方式都产生了 `rolled_back=True`，且留档的 243 测试 OceanBase 运行通过了 `test_pool_session_has_autocommit_off`。因此，被切换为可写的数据源（`app/datasources/service.py:248`）在该引擎上同样经过相同的 fail-closed 审计回滚。

**OceanBase 的默认语句配额会掐断长任务。** `ob_query_timeout` 默认 10 s（`10000000` µs），超时以错误 `4012` 中止，因此一条 BaseWard 本会乐意完整跑完的语句会被引擎叫停：它掐断了流式测试中 200 000 行的造数夹具。在把大导出或重元数据读取指向 OceanBase 之前，请在数据源账号或会话上设置 `ob_query_timeout`（并检查 `ob_trx_timeout`），同时记住 BaseWard 自身的 `GATEWAY_MYSQL_EXPORT_MAX_SECONDS` 是另一个独立的限制。

**系统库排除曾大小写敏感、且不感知引擎**，正如下文差异项 1 所预测：真实代码路径在 TiDB v8.5.0 上返回了 `INFORMATION_SCHEMA`、`METRICS_SCHEMA`、`PERFORMANCE_SCHEMA` 和 `test`，在 OceanBase CE 4.4.2.1 上返回了 `oceanbase` 加 `test`，在 MySQL 8.0.46 或 MariaDB 11.8.9 上则一无所返。这是元数据噪音，不是访问问题：Token 作用域对其中列出的任何对象仍然生效。此后已修复 —— 一份包含 `metrics_schema` 和 `oceanbase` 的小写清单，按大小写不敏感比较 —— 但该修复只有单元测试断言。上文四次引擎运行早于该修复，所以「噪音在 TiDB 和 OceanBase 上消失」还不是实测结论。

## 为什么「未验证」不等于「不安全」

策略在解析后的 AST 上判定，绝不靠字符串匹配，且解析固定为 MySQL 方言：

- `sqlglot.parse(sql, read="mysql")` —— `app/policy/engine.py:134`；改写后的语句在 `app/query/pipeline.py:405` 和 `:419` 以 `dialect="mysql"` 生成回来。
- 无法解析的 SQL 一律拒绝（`app/policy/engine.py:135-137`）。
- 凡被解析器退化为原始命令的一律拒绝：`exp.Command` 是 `_FORBIDDEN_NODES` 的成员（`app/policy/engine.py:33`），只有归结为 `SELECT` / `UNION` / `DESCRIBE <table>` / `INSERT` / `UPDATE` / `DELETE` 的语句才被放行（`app/policy/engine.py:157-162`）。

对于接受 MySQL 不接受的厂商专有语句的引擎，后果是：BaseWard 拒绝它。在非参考引擎上，风险是**功能收窄，不是策略绕过**。`REPLACE` 是唯一刻意的例外 —— 它在 MySQL 方言下解析为 `Command`，因此 `app/policy/engine.py:77` 将其重新解析为 `INSERT` 并走同一套写检查；重解析失败则该语句被拒绝。

## 信任一个引擎之前要核对的差异项

每一项都是 MySQL 家族引擎可能出现差异的具体位置，并附上假定 MySQL 行为的代码。

1. **系统库清单是一个小写集合** —— `mysql`、`information_schema`、`performance_schema`、`sys`、`metrics_schema`、`oceanbase`（`app/datasources/databases.py:34`）。每次 `information_schema` 读取都用 `exclude_system_schemas()` 构建谓词（`app/datasources/databases.py:51`），以 `LOWER(table_schema)` 与该集合比较；Python 侧的库清单同样把名称转小写。集合之外的引擎内部库 —— `__recycle_bin__` 之类 —— 仍不被排除，因此可能出现在 `search_objects`、`describe_datasource` 和管理端联想中。Token 作用域仍然把关访问，所以这是暴露噪音而非越权，但它是逐引擎要验证的第一件事。已用真实代码路径（`app/datasources/databases.py:list_upstream_databases`）在一台没有用户库的服务器上实测，**修复之前**：TiDB v8.5.0 返回 `INFORMATION_SCHEMA`、`METRICS_SCHEMA`、`PERFORMANCE_SCHEMA` 和 `test`，因为 TiDB 的系统库用大写命名，而当时的排除是精确、大小写敏感的匹配；OceanBase CE 4.4.2.1 返回 `oceanbase` 和 `test`，两者都不在清单上。MySQL 8.0.46 和 MariaDB 11.8.9 对同一台服务器返回空清单。真正由用户创建的 `test` 库本就该显示；引擎内部库才是噪音。`tests/unit/test_system_schemas.py` 现在断言清单、谓词与过滤；在对外暴露你的引擎之前请重新实测。
2. **会话内切库使用 `USE \`db\``**（`app/datasources/databases.py:307`，由 `app/mcp/pipeline.py` 调用）。所有 MySQL 协议引擎都接受 `USE`，但 OceanBase 的租户语义与 PolarDB-X 的逻辑库映射需要实机核查。
3. **元数据读取使用 `information_schema` 加 `INSTR`、`CONCAT` 和 `table_comment` / `column_comment`**（`app/mcp/introspect.py:172-358`、`app/policy/scope.py:78`、`app/mysql/session.py:305-336`）。注释列是中文标注的 schema 变得可搜索的关键；把这些列留空的引擎会把 `search_objects` 降级为仅按名称匹配。在大型分布式集群上，元数据查询本身可能很慢。
4. **导出通道直接使用 `aiomysql`** —— 非缓冲的 `SSCursor`（`app/datasources/export_pool.py:169`）和用于中止的 `KILL QUERY`（`app/datasources/export_pool.py:196`）。分布式引擎在 session-id 与 kill 语义上存在差异；请验证被中止的导出确实停掉了上游的工作，否则行数与时长配额就是仅有的刹车。
5. **错误处理以 MySQL errno 为键**（`1146` 表不存在及同类错误）。返回不同 errno 的引擎只会产生更粗粒度的消息，而不是更弱的检查 —— 拒绝发生在执行之前的策略层。
6. **字符集假定**：连接池以 `charset=utf8mb4` 建连（`app/datasources/pool.py:26-32`）。排序规则差异会改变排序与比较，进而可能改变一条脱敏查询返回哪些行。上文运行中观察到的服务器默认值已经各不相同：`utf8mb4_0900_ai_ci`（MySQL 8.0.46）、`utf8mb4_uca1400_ai_ci`（MariaDB 11.8.9）、`utf8mb4_bin`（TiDB v8.5.0）和 `utf8mb4_general_ci`（OceanBase CE 4.4.2.1）。`utf8mb4_bin` 按字节比较，因此大小写与重音差异会改变过滤器或脱敏匹配到哪些行；在这点重要时，请在 schema DDL 中固定排序规则，而不是依赖服务器默认值。
7. **上游账号的权限。** 应用层策略不能替代数据库授权。在每个引擎上，数据源账号都应只持有 `SELECT`（外加对显式可写库的 DML）。拥有超出 MySQL 授权词汇的角色或租户模型的引擎，需要各自的最小权限审查。
8. **`VERSION()` 不是它看上去的样子。** BaseWard 不存储引擎字段（`app/db/models.py` 只保留 host 和 port），所以网关及其 UI 中没有任何东西区分引擎 —— 而引擎自己报告的也是 MySQL 版本，不是自身版本：`8.0.11-TiDB-v8.5.0` 和 `5.7.25-OceanBase_CE-v4.4.2.1` 是上文运行中观察到的两个字符串。请按*报告出来的* MySQL 版本规划 CTE、JSON 与窗口函数支持，并核对引擎自己的文档。OceanBase 还要求连接用户名采用 `user@tenant` 形式。
9. **事务语义假定 autocommit 已关闭。** 支撑 fail-closed 审计的回滚依赖 `engine.begin()` 真正开启一个事务。请在已连接会话上检查 `SELECT @@autocommit` —— MySQL、MariaDB 和 TiDB 上是 `0`，OceanBase 上是 `1` —— 并同样检查隔离级别（前三者是 `REPEATABLE-READ`，OceanBase 是 `READ-COMMITTED`）。见上文实测发现。
10. **引擎侧语句配额。** `max_execution_time` 在 MySQL 8.0.46、TiDB v8.5.0 和 OceanBase CE 4.4.2.1 上存在且读数为 `0`（无限制），在 MariaDB 11.8.9 上则是未知变量（errno `1193`）—— 所以按某一个引擎的名字写下的检查项并不能照搬。OceanBase 另有 `ob_query_timeout`，默认 **10 s**，超时以错误 `4012` 中止；那是引擎配额，不是 BaseWard 的限制。它们与 BaseWard 自身的刹车（`GATEWAY_MYSQL_EXPORT_MAX_SECONDS`，`app/config.py:66`）相互独立，因此可能出现流被引擎掐断、而网关仍把自己的配额报告为原因的情况。

## 如何验证一个引擎

验证就是把现有套件对着引擎跑一遍，而不是编写新的断言：

```bash
cp .env.test.example .env.test      # TEST_DB_HOST/PORT/USER/PASSWORD against the target engine
pytest -q -m integration            # needs CREATE/DROP DATABASE on the test account
```

套件断言产品承诺所依赖的四条不变量：AST 默认拒绝、四级 Token 作用域、结果脱敏、fail-closed 审计。通过的引擎在上表中从「预期可用」移为「已实测」，且该行有证明它的测试运行作为依据。

`tests/integration/conftest.py` 用 MySQL DDL（`AUTO_INCREMENT`、`ENGINE=InnoDB`、`utf8mb4 COLLATE`）创建夹具，因此拒绝这些 DDL 的引擎会在 setup 阶段失败。请把 setup 失败当作兼容性发现，而不是测试缺陷。

在归咎于引擎之前，有两项环境前提值得检查：

- **单调时钟。** 时钟回拨时，OceanBase 的选举与时间轮会拒绝运行：observer 记录了 `OB_CLOCK_OUT_OF_ORDER`（`-6201`），sys 租户始终选不出 leader，表现为永远停在 `Server is initializing`，而不是一个明确的错误。在 WSL2 下，`systemd-timesyncd` 每次轮询都把时钟往回拨（364 条 `Time jumped backwards` 内核消息）；禁用它之后，同一个镜像 50 s 即启动完成。在断定某引擎不兼容之前，先检查 `dmesg` 里有没有时钟回跳。
- **引擎侧语句配额。** 流式夹具用一条语句造 200 000 行数据，OceanBase 默认 10 s 的 `ob_query_timeout` 会中止它。为本次运行调高该值并记录下来，这样一次全绿运行就不是靠悄悄改动引擎默认值买来的。

单元套件（`pytest -q -m "not integration"`）不需要数据库，在每个引擎上运行结果都相同，因此它不是引擎兼容性的证据。

## 不作哪些声明

- 在某个非 MySQL 引擎被列入上文已实测之前，不声称它通过了策略、脱敏或审计验证。
- 不声称功能对等：MySQL 协议引擎可能拒绝 BaseWard 放行的语句，也可能接受 BaseWard 拒绝的语句。拒绝会被审计，因此这种错位在审计轨迹中可见。
- 不提供任何形式的跨方言支持。加入 PostgreSQL 不是换个驱动就行 —— 方言字面量、`information_schema` 查询、`USE` 语义和 errno 这条轴线都必须先抽象出来。