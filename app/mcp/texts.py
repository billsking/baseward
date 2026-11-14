"""MCP instructions and tool descriptions.

MCP clients have no browser language, so the language comes from
``GATEWAY_MCP_LANGUAGE`` and is applied once when the FastMCP instance is built.
"""

from __future__ import annotations

TEXTS: dict[str, dict[str, str]] = {
    "zh-CN": {
        "instructions": """数据库查询网关，按以下顺序工作以减少往返：
1. 不确定数据源名称时先调用 list_datasources；datasource 参数可传精确名称或唯一业务描述片段，有歧义时返回候选。
2. 需要表/字段信息时优先调用 describe_datasource（一次返回表与列、注释、主键），大库或只记得业务词时用 search_objects（支持中文注释与空格分词）。
3. 最后用 execute_sql 执行单条 SQL。不要用 information_schema 或 SHOW 语句探索元数据。
4. execute_sql 的返回若带 export 字段，说明本次结果已被截断、只拿到了预览。此时必须改用 export
   里的账号密码，按 sample_code 写代码通过 MySQL 协议（host/port/database）取全量数据；
   不要用更大的 LIMIT 反复重试，也不要让用户手工拼连接串。export 是一次性的短期凭证，取完即弃。
5. 用户明确要求导出全量数据，或已知结果集会超过返回上限、execute_sql 会超时时，可以在执行前
   直接调用 issue_export_credential(datasource, sql, reason) 拿凭证再跑脚本；
   普通小查询或“以防万一”不要调用。
""",
        "tool.list_datasources": """列出当前 Token 可访问的数据源（名称、业务描述、默认库、库清单与读写状态）。

首次查询或不确定数据源名称时先调用。datasource 参数可传这里返回的名称，
也可传唯一匹配的业务描述片段（如“示例库”）。每项的 databases 是当前
Token 可用库（可访问且已授权），read_only 为合并数据源总开关后的有效策略；
顶层 is_read_only 只是数据源聚合字段，判断能否写入请看 databases[].read_only。
""",
        "tool.describe_datasource": """一次获取数据源的表与字段结构（表/列注释、类型、主键）。

datasource 必填，支持名称或唯一业务描述片段；pattern 可选，按关键词过滤
（空格分隔多词时需同时满足，匹配表名/表注释/列名/列注释）；database 可选，
限定库名（不传时检索当前 Token 的全部可用库）；include_columns=false 只返回
表清单，适合大库先看有哪些表；limit 为表数量上限（1-100）。
结果按关键词语义相关度排序，响应中的 databases 为当前 Token 可用库及有效读写策略。
""",
        "tool.search_objects": """按关键词搜索表/列元数据，结果带注释并按相关度排序。

datasource 必填（名称或唯一业务描述片段）；pattern 为 1-128 字符，
空格分隔多个关键词时需同时满足，匹配表名、列名、表注释与列注释；
object_type 取 all/table/column；database 可选，不传时搜索当前 Token
的全部可用库；响应中的 databases 为本次实际检索的库及有效读写策略。
需要完整表结构时优先用 describe_datasource。
""",
        "tool.execute_sql": """执行单条 SQL 并返回脱敏后的结果。

datasource 必填（名称或唯一业务描述片段）；SQL 必须为单条语句且默认只读；
写权限由管理员在 Web 端显式开启，DDL/权限/多语句/无 WHERE 写一律拒绝；
查询元数据请使用 describe_datasource 或 search_objects。
database 可选，只决定「未写库名的表」在哪个库解析，不扩大授权范围：
SQL 里出现的每个库仍会逐一校验访问权限与读写策略；不传时使用数据源默认库；
跨库查询请写限定名「库.表」，且每个库都必须已在该数据源的可访问范围内。
当前 Token 的可用库与每库读写状态可用 list_datasources 查询。
结果可能被截断（truncated=true）；若返回 export，按其中 sample_code 用脚本
通过 MySQL 协议取全量；若只有 hint 而无 export，本次无法自动签发，把 hint
原样转告用户。
错误以 JSON {error:{code,message,request_id}} 返回。
""",
        "tool.issue_export_credential": """签发一张短期 MySQL 只读凭证，用于绕过 MCP 单次返回上限导出全量数据。

仅在以下情况调用：execute_sql 返回 truncated=true（只拿到预览）、用户明确要求
导出全量数据，或已知结果集很大、execute_sql 会超时。普通小查询或“以防万一”
不要调用。

sql 必填，必须是本次要导出的那条只读语句：网关会在签发前按该 Token 的数据源/
表/字段授权与只读策略预检，写语句或越权直接拒绝，不会执行该 SQL。reason 简要
说明用途（≤200 字符，写入审计）。database 可选，指定凭证连接的目标库；不传时
取数据源默认库，返回的 database 为实际生效的目标库。返回的 password 只出现一次，
用 sample_code 通过 MySQL 协议流式取数；凭证短时有效，不要写入代码仓库或配置文件。
凭证绑定返回里的 source_ip（本次 MCP 请求被网关看到的来源 IP），取数脚本要与
签发请求同一来源地址运行，否则握手会被拒。
""",
        "error.unauthenticated": "未认证的 MCP 调用",
    },
    "en": {
        "instructions": """Database query gateway. Work in this order to reduce round trips:
1. When the datasource name is unclear, call list_datasources first; the datasource argument accepts an exact name or a unique fragment of its business description, and ambiguous input returns candidates.
2. When you need table or column information, prefer describe_datasource (returns tables, columns, comments, and primary keys in one call); for large databases or when you only remember a business term, use search_objects (supports comments and space-separated keywords).
3. Finally, run a single SQL statement with execute_sql. Do not use information_schema or SHOW statements to explore metadata.
4. If execute_sql returns an export field, the result was truncated and you only received a preview. In that case, use the account and password in export with sample_code to write a script that fetches the full data over the MySQL protocol (host/port/database); do not retry with a larger LIMIT, and do not ask the user to assemble a connection string by hand. export is a one-time short-lived credential; discard it after use.
5. When the user explicitly asks to export all data, or you know the result set will exceed the return limit or execute_sql will time out, you may call issue_export_credential(datasource, sql, reason) before execution to obtain a credential and then run the script; do not call it for ordinary small queries or "just in case".
""",
        "tool.list_datasources": """List the datasources this token can access (name, business description, default database, database list, and read/write state).

Call this first for the initial query or when the datasource name is unclear. The datasource argument accepts a name returned here or a unique matching fragment of the business description (for example, "demo"). Each item's databases are the databases available to this token (accessible and granted); read_only is the effective policy after combining the datasource-wide switch. The top-level is_read_only is only an aggregate datasource field; check databases[].read_only to decide whether writes are allowed.
""",
        "tool.describe_datasource": """Fetch a datasource's table and column structure in one call (table/column comments, types, primary keys).

datasource is required and accepts a name or a unique business-description fragment. pattern is optional and filters by keyword (space-separated terms must all match; matches table names, table comments, column names, and column comments). database is optional and limits the search to one database (all databases available to this token are searched when omitted). Set include_columns=false to return only tables, which suits large databases. limit caps the number of tables (1-100).
Results are ranked by semantic relevance to the keyword; the response's databases are the databases available to this token with their effective read/write policy.
""",
        "tool.search_objects": """Search table and column metadata by keyword; results include comments and are ranked by relevance.

datasource is required (name or unique business-description fragment). pattern is 1-128 characters; space-separated terms must all match and are checked against table names, column names, table comments, and column comments. object_type is all/table/column. database is optional; when omitted, all databases available to this token are searched. The response's databases are the databases actually searched with their effective read/write policy.
Prefer describe_datasource when you need the full table structure.
""",
        "tool.execute_sql": """Run a single SQL statement and return the masked result.

datasource is required (name or unique business-description fragment). SQL must be a single statement and is read-only by default; write access is enabled explicitly by an administrator in the web UI, and DDL, privilege statements, multiple statements, and writes without WHERE are always rejected. Use describe_datasource or search_objects to inspect metadata.
database is optional and only decides which database resolves unqualified table names; it does not widen the scope. Every database that appears in the SQL is still checked for access and read/write policy. When omitted, the datasource default database is used. For cross-database queries, use qualified names "database.table", and every database must be accessible on the datasource.
Use list_datasources to see the databases available to this token and each database's read/write state.
Results may be truncated (truncated=true). If export is returned, use its sample_code in a script to fetch the full data over the MySQL protocol; if only hint is returned without export, automatic issuance is impossible for this call, so relay the hint to the user verbatim.
Errors are returned as JSON {error:{code,message,request_id}}.
""",
        "tool.issue_export_credential": """Issue a short-lived read-only MySQL credential to export the full result beyond the MCP single-response limit.

Call this only when execute_sql returns truncated=true (preview only), the user explicitly asks to export all data, or the result set is known to be large and execute_sql would time out. Do not call it for ordinary small queries or "just in case".

sql is required and must be the same read-only statement to export: before issuing, the gateway pre-checks it against the token's datasource/table/column grants and read-only policy; write statements or out-of-scope statements are rejected without being executed. reason briefly states the purpose (up to 200 characters; written to the audit log). database is optional and selects the credential's target database; when omitted, the datasource default database is used, and the returned database is the effective target. The returned password is shown only once; use sample_code to stream data over the MySQL protocol. The credential is short-lived; never write it into a repository or configuration file.
The credential is bound to source_ip in the response (the source IP the gateway saw for this MCP request); run the fetch script from the same source address, or the handshake is rejected.
""",
        "error.unauthenticated": "Unauthenticated MCP call",
    },
}


def mcp_texts(language: str) -> dict[str, str]:
    return TEXTS.get(language, TEXTS["zh-CN"])
