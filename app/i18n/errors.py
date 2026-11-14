"""English translations for service-layer error messages and field labels.

Service code raises messages in Chinese (the historical language). Errors are
translated when they are rendered: ``GatewayError.__str__`` looks the exact
message up in this catalog. Parameterised messages that cannot be matched
exactly are raised through ``t()`` with a key from ``messages.py`` instead.
"""

from __future__ import annotations

ERROR_MESSAGES: dict[str, str] = {
    # Audit
    "审计服务不可用": "Audit service unavailable",
    "审计记录更新失败": "Failed to update the audit record",
    # Admin authentication and session
    "未登录或会话已过期": "Not signed in or the session has expired",
    "会话无效": "Invalid session",
    "会话已失效，请重新登录": "The session is no longer valid; please sign in again",
    "CSRF 校验失败": "CSRF validation failed",
    "新密码至少 8 字符": "The new password must be at least 8 characters",
    "管理员不存在": "Administrator not found",
    "旧密码不正确": "The current password is incorrect",
    "新密码不能与旧密码相同": "The new password must differ from the current one",
    "用户名或密码错误": "Incorrect username or password",
    "管理库暂时不可用，请稍后重试": "The management database is unavailable; please try again later",
    # Datasources
    "数据源不存在": "Datasource not found",
    "数据源名称已存在": "A datasource with that name already exists",
    "密码不能为空": "Password is required",
    "端口必须在 1-65535 之间": "Port must be between 1 and 65535",
    "至少选择一个可访问库": "Select at least one accessible database",
    "默认库必须在可访问库内": "The default database must be one of the accessible databases",
    "库未暴露或不存在": "Database is not exposed or does not exist",
    "目标库名不合法": "Invalid target database name",
    "数据源不存在或已停用": "Datasource does not exist or is disabled",
    "数据源不在该 Token 授权范围内": "Datasource is outside this token's scope",
    "连接失败，请检查地址、账号与网络": "Connection failed; check the host, credentials, and network",
    # Masking
    "脱敏策略必须是 mask/hash/null/off 之一": "Masking strategy must be one of mask, hash, null, or off",
    "脱敏模式必须是 phone/email/id_card/name/bank_card/generic 之一": (
        "Masking pattern must be one of phone, email, id_card, name, bank_card, or generic"
    ),
    "hash/null/off 策略不允许指定脱敏模式": "The hash, null, and off strategies do not accept a masking pattern",
    "该列已存在脱敏规则": "A masking rule already exists for this column",
    "脱敏规则不存在": "Masking rule not found",
    # Tokens
    "Token 不存在": "Token not found",
    "该用户已有启用 Token，请先停用": "This user already has an enabled token; disable it first",
    "Token 创建冲突，请重试": "Token creation conflicted; please retry",
    "授权范围格式错误": "Invalid scope format",
    "表或字段授权格式错误": "Invalid table or column grant format",
    "表授权格式错误": "Invalid table grant format",
    "字段授权格式错误": "Invalid column grant format",
    "授权范围 JSON 无法解析": "The scope JSON could not be parsed",
    "数据源授权格式错误": "Invalid datasource grant format",
    "全部表授权格式错误": "Invalid whole-datasource table grant format",
    "授权全部表之前必须先选择数据源": "Select a datasource before granting all of its tables",
    "全部表授权的数据源不在已选择的数据源中": "A whole-table datasource is not among the selected datasources",
    "库级全部表授权格式错误": "Invalid whole-database table grant format",
    "授权库级全部表之前必须先选择数据源": "Select a datasource before granting whole databases",
    "库级全部表的数据源不在已选择的数据源中": "A whole-database datasource is not among the selected datasources",
    "该数据源已授权全部表，不能再指定库": "This datasource already grants all tables; do not grant a specific database",
    "选择表之前必须先选择数据源": "Select a datasource before selecting tables",
    "选择字段之前必须先选择表": "Select a table before selecting columns",
    "表不属于已选择的数据源": "A table does not belong to the selected datasources",
    "该数据源已授权全部表，不能再指定具体表": "This datasource already grants all tables; do not grant specific tables",
    "该库已授权全部表，不能再指定具体表": "This database already grants all tables; do not grant specific tables",
    "字段不属于已选择的表": "A column does not belong to the selected tables",
    "该数据源已授权全部表，不能再指定字段": "This datasource already grants all tables; do not grant columns",
    "该库已授权全部表，不能再指定字段": "This database already grants all tables; do not grant columns",
    "授权范围过大，请减少选择": "Scope is too large; select fewer objects",
    "授权范围格式不正确，请重新选择": "Invalid scope format; please select again",
    # Credentials
    "无法确定申请来源 IP，拒绝签发未绑定来源的凭证": (
        "Cannot determine the requesting source IP; refusing to issue an unbound credential"
    ),
    "凭证不存在": "Credential not found",
    "凭证账号生成冲突，请重试": "Credential account generation conflicted; please retry",
    "该 Token 在此数据源下没有可用库，请先调整授权": (
        "This token has no available database on this datasource; adjust its scope first"
    ),
    "目标库不存在或未授权": "Target database does not exist or is not authorized",
    "Token 不存在或已停用": "Token does not exist or is disabled",
    # Settings and retention
    "审计保留天数必须在 1-3650 之间": "Audit retention days must be between 1 and 3650",
    "两次输入的新密码不一致": "The two new passwords do not match",
    # Admin metadata endpoints
    "请先填写完整的连接参数（含密码）": "Fill in the complete connection settings (including the password) first",
    "数据源参数不正确": "Invalid datasource parameter",
    "搜索关键词过长": "Search term is too long",
    "库名或表名不正确": "Invalid database or table name",
    "读取数据表失败，请检查数据源连接": "Failed to read tables; check the datasource connection",
    "读取表字段失败，请检查数据源连接": "Failed to read table columns; check the datasource connection",
    "搜索或分页参数不正确": "Invalid search or pagination parameters",
    "match 必须是 table 或 column": "match must be table or column",
    "数据源、库名、表名或分页参数不正确": "Invalid datasource, database, table, or pagination parameters",
    "请求体不是合法 JSON": "Request body is not valid JSON",
    "请求体格式不正确": "Invalid request body format",
    "导入条目过多或格式不正确": "Too many import entries or invalid format",
    "导入条目格式不正确": "Invalid import entry format",
    "导入条目名称不正确": "Invalid import entry name",
    "校验导入条目失败，请检查数据源连接": "Failed to verify import entries; check the datasource connection",
    "审计记录不存在": "Audit record not found",
    "请求参数不正确": "Invalid request parameters",
    "网关内部错误": "Gateway internal error",
    "页面不存在": "Page not found",
}

LABELS: dict[str, str] = {
    "名称": "Name",
    "主机": "Host",
    "账号": "Username",
    "默认库": "Default database",
    "数据库名": "Database name",
    "表名": "Table name",
    "列名": "Column name",
    "库名": "Database name",
    "字段名": "Column name",
    "MySQL 导出通道": "MySQL export channel",
    "MCP 入口": "MCP endpoint",
}
