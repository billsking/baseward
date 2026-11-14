"""Admin UI and service message catalog (Chinese source, English translation)."""

from __future__ import annotations

MESSAGES: dict[str, dict[str, str]] = {
    # Shared
    "common.save": {"zh-CN": "保存", "en": "Save"},
    "common.cancel": {"zh-CN": "取消", "en": "Cancel"},
    "common.create": {"zh-CN": "创建", "en": "Create"},
    "common.delete": {"zh-CN": "删除", "en": "Delete"},
    "common.copy": {"zh-CN": "复制", "en": "Copy"},
    "common.details": {"zh-CN": "详情", "en": "Details"},
    "common.all": {"zh-CN": "全部", "en": "All"},
    "common.select_all": {"zh-CN": "全选", "en": "Select all"},
    "common.clear": {"zh-CN": "清空", "en": "Clear"},
    "common.readonly": {"zh-CN": "只读", "en": "Read-only"},
    "common.writable": {"zh-CN": "可写", "en": "Writable"},
    "common.enabled": {"zh-CN": "启用", "en": "Enabled"},
    "common.disabled": {"zh-CN": "停用", "en": "Disabled"},
    "common.back_to_list": {"zh-CN": "返回列表", "en": "Back to list"},
    "common.prev": {"zh-CN": "上一页", "en": "Previous"},
    "common.next": {"zh-CN": "下一页", "en": "Next"},
    "common.page_of": {"zh-CN": "第 {page} / {pages} 页", "en": "Page {page} of {pages}"},
    "common.total": {"zh-CN": "共 {n} 条", "en": "{n} total"},
    "common.parenthesized": {"zh-CN": "（{text}）", "en": " ({text})"},
    "common.list_separator": {"zh-CN": "、", "en": ", "},
    "common.time": {"zh-CN": "时间", "en": "Time"},
    "common.user": {"zh-CN": "用户", "en": "User"},
    "common.datasource": {"zh-CN": "数据源", "en": "Datasource"},
    "common.database": {"zh-CN": "库", "en": "Database"},
    "common.tool": {"zh-CN": "工具", "en": "Tool"},
    "common.status": {"zh-CN": "状态", "en": "Status"},
    "common.duration": {"zh-CN": "耗时", "en": "Duration"},
    "common.actions": {"zh-CN": "操作", "en": "Actions"},
    # Shell
    "app.title": {"zh-CN": "BaseWard", "en": "BaseWard"},
    "nav.dashboard": {"zh-CN": "仪表盘", "en": "Dashboard"},
    "nav.datasources": {"zh-CN": "数据源", "en": "Datasources"},
    "nav.tokens": {"zh-CN": "Token", "en": "Tokens"},
    "nav.credentials": {"zh-CN": "凭证", "en": "Credentials"},
    "nav.short_credentials": {"zh-CN": "短期凭证", "en": "Short credentials"},
    "nav.audit": {"zh-CN": "审计", "en": "Audit"},
    "nav.settings": {"zh-CN": "设置", "en": "Settings"},
    "nav.logout": {"zh-CN": "退出", "en": "Sign out"},
    "nav.language": {"zh-CN": "语言", "en": "Language"},
    "footer.text": {
        "zh-CN": "BaseWard · AI 与数据之间的守门人",
        "en": "BaseWard · The warden between AI and your data",
    },
    # Login
    "login.hint": {"zh-CN": "请使用管理员账号登录", "en": "Sign in with an administrator account"},
    "login.username": {"zh-CN": "用户名", "en": "Username"},
    "login.password": {"zh-CN": "密码", "en": "Password"},
    "login.submit": {"zh-CN": "登录", "en": "Sign in"},
    # Pages
    "page.dashboard": {"zh-CN": "仪表盘", "en": "Dashboard"},
    "page.datasources": {"zh-CN": "数据源", "en": "Datasources"},
    "page.datasource_new": {"zh-CN": "新建数据源", "en": "New datasource"},
    "page.datasource_edit": {"zh-CN": "编辑数据源", "en": "Edit datasource"},
    "page.datasource_edit_named": {"zh-CN": "编辑数据源 · {name}", "en": "Edit datasource · {name}"},
    "page.policy": {"zh-CN": "策略与脱敏 · {name}", "en": "Policy and masking · {name}"},
    "page.tokens": {"zh-CN": "Token", "en": "Tokens"},
    "page.token_edit": {"zh-CN": "编辑 Token 授权", "en": "Edit token scope"},
    "page.credentials": {"zh-CN": "临时导出凭证", "en": "Temporary export credentials"},
    "page.short_credentials": {"zh-CN": "短期凭证", "en": "Short-lived credentials"},
    "page.credential_new": {"zh-CN": "签发临时导出凭证", "en": "Issue temporary export credential"},
    "page.credential_issued": {"zh-CN": "凭证已签发", "en": "Credential issued"},
    "page.audit": {"zh-CN": "审计日志", "en": "Audit log"},
    "page.audit_detail": {"zh-CN": "审计详情", "en": "Audit detail"},
    "page.settings": {"zh-CN": "设置", "en": "Settings"},
    # Flash messages
    "flash.datasource_created": {"zh-CN": "数据源已创建", "en": "Datasource created"},
    "flash.datasource_updated": {"zh-CN": "数据源已保存", "en": "Datasource saved"},
    "flash.datasource_deleted": {"zh-CN": "数据源已删除", "en": "Datasource deleted"},
    "flash.connection_ok": {"zh-CN": "连接测试成功", "en": "Connection test succeeded"},
    "flash.policy_saved": {"zh-CN": "读写策略已保存", "en": "Read/write policy saved"},
    "flash.rule_added": {"zh-CN": "脱敏规则已添加", "en": "Masking rule added"},
    "flash.rule_deleted": {"zh-CN": "脱敏规则已删除", "en": "Masking rule deleted"},
    "flash.token_created": {"zh-CN": "Token 已创建", "en": "Token created"},
    "flash.token_scope_updated": {"zh-CN": "Token 授权范围已更新", "en": "Token scope updated"},
    "flash.token_policy_updated": {"zh-CN": "Token 行为人策略已更新", "en": "Token actor policy updated"},
    "form.actor_policy": {"zh-CN": "行为人策略", "en": "Actor policy"},
    "form.actor_policy.ignore": {"zh-CN": "个人（忽略身份头）", "en": "Personal (ignore headers)"},
    "form.actor_policy.require": {"zh-CN": "代理（强制身份头）", "en": "Agent (require headers)"},
    "form.token_kind": {"zh-CN": "令牌类型", "en": "Token kind"},
    "form.token_kind.access": {"zh-CN": "常规（可访问数据源）", "en": "Access (datasource queries)"},
    "form.token_kind.exchange": {"zh-CN": "换发（仅换短期凭证）", "en": "Exchange (mint short credentials only)"},
    "token.exchange_hint": {
        "zh-CN": "换发型令牌只能调用 /api/exchange 换发短期凭证，不能查库、不能签发导出凭证；其授权范围即换发出的短期凭证的权限上限，行为人策略固定为 require。",
        "en": "Exchange tokens may only call /api/exchange; they cannot query data or issue export credentials. Their scope is the ceiling for every short credential they mint, and the actor policy is fixed to require.",
    },
    "short_credential.intro": {
        "zh-CN": "由换发型令牌经 /api/exchange 签发，绑定单一行为人与来源 IP，到期自动失效；此处可提前吊销。",
        "en": "Minted by exchange tokens via /api/exchange, bound to one actor and source IP, and expiring automatically; revoke early here.",
    },
    "short_credential.list_title": {"zh-CN": "短期凭证列表", "en": "Short credential list"},
    "short_credential.table.prefix": {"zh-CN": "凭证前缀", "en": "Credential prefix"},
    "short_credential.table.parent": {"zh-CN": "所属换发令牌", "en": "Parent exchange token"},
    "short_credential.confirm_revoke": {
        "zh-CN": "确认吊销该短期凭证？吊销后使用该凭证的请求立即被拒绝。",
        "en": "Revoke this short credential? Requests using it are rejected immediately.",
    },
    "short_credential.empty": {"zh-CN": "暂无短期凭证", "en": "No short credentials yet"},
    "column.actor_policy": {"zh-CN": "策略", "en": "Policy"},
    "column.actor": {"zh-CN": "行为人", "en": "Actor"},
    "token.actor_policy_hint": {
        "zh-CN": "代理型 Token 要求每次调用携带终端用户身份头（X-BaseWard-Actor-*），缺失即拒绝；个人型忽略身份头。",
        "en": "Agent tokens require end-user identity headers (X-BaseWard-Actor-*) on every call and reject missing ones; personal tokens ignore them.",
    },
    "audit.actor_title": {"zh-CN": "行为人", "en": "Actor"},
    "audit.trust.asserted": {"zh-CN": "自报", "en": "Self-asserted"},
    "flash.token_disabled": {"zh-CN": "Token 已停用", "en": "Token disabled"},
    "flash.token_deleted": {"zh-CN": "Token 已删除", "en": "Token deleted"},
    "flash.credential_revoked": {"zh-CN": "凭证已撤销", "en": "Credential revoked"},
    "flash.short_credential_revoked": {"zh-CN": "短期凭证已吊销", "en": "Short credential revoked"},
    "flash.password_changed": {
        "zh-CN": "密码已修改，请使用新密码重新登录",
        "en": "Password changed; sign in with the new password",
    },
    "flash.retention_saved": {"zh-CN": "审计保留天数已保存", "en": "Audit retention saved"},
    "flash.allowlist_saved": {
        "zh-CN": "来源 IP 白名单已保存，立即生效",
        "en": "Source IP allowlist saved and effective immediately",
    },
    "flash.cleanup_done": {"zh-CN": "过期审计已清理", "en": "Expired audit records cleaned up"},
    "flash.connection_ok_extra": {"zh-CN": "耗时 {n} ms", "en": "took {n} ms"},
    "flash.cleanup_done_extra": {
        "zh-CN": "删除 {n} 条过期记录",
        "en": "deleted {n} expired records",
    },
    # Dashboard
    "dashboard.new_datasource": {"zh-CN": "新建数据源", "en": "New datasource"},
    "dashboard.manage_tokens": {"zh-CN": "管理 Token", "en": "Manage tokens"},
    "dashboard.stat_datasources": {"zh-CN": "数据源", "en": "Datasources"},
    "dashboard.stat_datasources_note": {"zh-CN": "启用 {n} 个", "en": "{n} enabled"},
    "dashboard.stat_tokens": {"zh-CN": "启用 Token", "en": "Enabled tokens"},
    "dashboard.stat_tokens_note": {
        "zh-CN": "每个用户最多一个启用 Token",
        "en": "One enabled token per user",
    },
    "dashboard.stat_today_calls": {"zh-CN": "今日调用", "en": "Today's calls"},
    "dashboard.stat_today_note": {
        "zh-CN": "按 Asia/Shanghai 统计",
        "en": "Counted in Asia/Shanghai",
    },
    "dashboard.stat_denied": {"zh-CN": "今日拒绝", "en": "Denied today"},
    "dashboard.stat_denied_note": {
        "zh-CN": "策略或认证拒绝",
        "en": "Policy or authentication denials",
    },
    "dashboard.recent_audit": {"zh-CN": "最近审计", "en": "Recent audit"},
    "dashboard.view_all": {"zh-CN": "查看全部", "en": "View all"},
    "dashboard.empty": {"zh-CN": "暂无审计记录", "en": "No audit records yet"},
    # Datasource list
    "datasource.new": {"zh-CN": "新建数据源", "en": "New datasource"},
    "datasource.table.name": {"zh-CN": "名称", "en": "Name"},
    "datasource.table.address": {"zh-CN": "地址", "en": "Address"},
    "datasource.table.databases": {"zh-CN": "可访问库", "en": "Accessible databases"},
    "datasource.table.access": {"zh-CN": "读写", "en": "Access"},
    "datasource.table.remark": {"zh-CN": "备注", "en": "Remark"},
    "datasource.flag_default": {"zh-CN": "默认", "en": "default"},
    "datasource.flag_readonly": {"zh-CN": "只读", "en": "read-only"},
    "datasource.flag_writable": {"zh-CN": "可写", "en": "writable"},
    "datasource.summary_all_readonly": {"zh-CN": "全部只读", "en": "All read-only"},
    "datasource.summary_writable": {
        "zh-CN": "可写 {n} 个库",
        "en": "{n} writable databases",
    },
    "datasource.master_readonly": {"zh-CN": "总开关只读", "en": "Master switch: read-only"},
    "datasource.edit": {"zh-CN": "编辑", "en": "Edit"},
    "datasource.policy": {"zh-CN": "策略", "en": "Policy"},
    "datasource.test": {"zh-CN": "测试", "en": "Test"},
    "datasource.confirm_delete": {
        "zh-CN": "确认删除数据源 {name}？其脱敏规则会一并删除。",
        "en": "Delete datasource {name}? Its masking rules will also be removed.",
    },
    "datasource.empty": {
        "zh-CN": "还没有数据源，先创建一个。",
        "en": "No datasources yet. Create one first.",
    },
    # Datasource form
    "datasource.narrowing_title": {
        "zh-CN": "移除可访问库会影响以下对象，确认后才会保存：",
        "en": "Removing accessible databases affects the following objects; save only after confirming:",
    },
    "datasource.narrowing_removed": {
        "zh-CN": "移除的库：{names}",
        "en": "Removed databases: {names}",
    },
    "datasource.narrowing_tokens": {
        "zh-CN": "{n} 个 Token 的授权将失效（配置保留）",
        "en": "{n} token grants will become invalid (configuration kept)",
    },
    "datasource.narrowing_credentials": {
        "zh-CN": "{n} 张有效凭证的目标库将被拒",
        "en": "The target database of {n} active credentials will be denied",
    },
    "datasource.narrowing_databases": {
        "zh-CN": "{n} 个库策略行会删除",
        "en": "{n} database policy rows will be deleted",
    },
    "datasource.narrowing_confirm": {
        "zh-CN": "我已了解影响，确认保存",
        "en": "I understand the impact and want to save",
    },
    "field.name": {"zh-CN": "名称", "en": "Name"},
    "field.host": {"zh-CN": "主机", "en": "Host"},
    "field.port": {"zh-CN": "端口", "en": "Port"},
    "field.username": {"zh-CN": "账号", "en": "Username"},
    "field.password": {"zh-CN": "密码", "en": "Password"},
    "field.default_database": {"zh-CN": "默认库", "en": "Default database"},
    "field.remark": {"zh-CN": "备注", "en": "Remark"},
    "field.database": {"zh-CN": "库", "en": "Database"},
    "field.table": {"zh-CN": "表", "en": "Table"},
    "field.column": {"zh-CN": "列", "en": "Column"},
    "field.strategy": {"zh-CN": "策略", "en": "Strategy"},
    "field.pattern": {"zh-CN": "模式", "en": "Pattern"},
    "datasource.password_keep_hint": {
        "zh-CN": "（留空沿用原密码）",
        "en": "(leave blank to keep the current password)",
    },
    "datasource.databases_title": {"zh-CN": "可访问库", "en": "Accessible databases"},
    "datasource.databases_hint": {
        "zh-CN": "AI 只能看到勾选的库。新增或重新加入的库默认只读；重新加入会恢复旧授权，但库策略按默认只读重建。",
        "en": (
            "The AI can only see the databases you check. New or re-added databases are read-only "
            "by default; re-adding restores the old grant, but the database policy is rebuilt as read-only."
        ),
    },
    "datasource.read_databases": {"zh-CN": "读取库清单", "en": "Load databases"},
    "datasource.filter_databases": {"zh-CN": "按库名过滤", "en": "Filter by database name"},
    "datasource.empty_new": {
        "zh-CN": "先填写上方连接参数，再点「读取库清单」。",
        "en": "Fill in the connection fields above, then click “Load databases”.",
    },
    "datasource.empty_edit": {
        "zh-CN": "尚未配置可访问库。",
        "en": "No accessible databases configured yet.",
    },
    "datasource.read_hint_new": {
        "zh-CN": "读取不到库清单时无法保存（与连接测试失败同口径）。",
        "en": "Saving is blocked until the database list can be read (same rule as a failed connection test).",
    },
    "datasource.read_hint_edit": {
        "zh-CN": "读取失败时仍可查看已保存清单，但不能刷新或新增库。",
        "en": "If loading fails you can still view the saved list, but cannot refresh or add databases.",
    },
    "datasource.default_database_placeholder": {
        "zh-CN": "（先勾选可访问库）",
        "en": "(select accessible databases first)",
    },
    "datasource.remark_hint": {
        "zh-CN": "（会展示给 AI，可写业务别名，如“示例库”）",
        "en": "(shown to the AI; a business alias works well, e.g. “demo”)",
    },
    "datasource.save_hint": {
        "zh-CN": "保存前会先用该配置连接数据库，连接成功才会写入。",
        "en": "The gateway tests the connection before saving; the record is written only on success.",
    },
    # Policy
    "policy.edit_connection": {"zh-CN": "编辑连接", "en": "Edit connection"},
    "policy.readwrite": {"zh-CN": "读写策略", "en": "Read/write policy"},
    "policy.master_switch": {"zh-CN": "总开关：{state}", "en": "Master switch: {state}"},
    "policy.master_hint": {
        "zh-CN": "总开关是上位强制只读：开启后任何库都禁止写。实际可写 = 总开关可写 且 该库可写。",
        "en": (
            "The master switch overrides everything: when read-only, no database can be written. "
            "Effective write access = master switch writable AND the database writable."
        ),
    },
    "policy.preconfigured_warning": {
        "zh-CN": "总开关当前为只读，下方标记为可写的库仅作预配置，实际仍全部禁写。",
        "en": (
            "The master switch is read-only, so databases marked writable below are pre-configured only; "
            "all writes are still denied."
        ),
    },
    "policy.readonly_recommended": {"zh-CN": "只读（推荐）", "en": "Read-only (recommended)"},
    "policy.save_master": {"zh-CN": "保存总开关", "en": "Save master switch"},
    "policy.per_database": {"zh-CN": "按库策略", "en": "Per-database policy"},
    "policy.table.effective": {"zh-CN": "有效策略", "en": "Effective policy"},
    "policy.table.stored": {"zh-CN": "库级配置", "en": "Database setting"},
    "policy.effective_readonly_master": {
        "zh-CN": "只读（总开关）",
        "en": "Read-only (master switch)",
    },
    "policy.confirm_writable": {"zh-CN": "确认改为可写", "en": "Confirm switch to writable"},
    "policy.make_writable": {"zh-CN": "改为可写", "en": "Make writable"},
    "policy.make_readonly": {"zh-CN": "改为只读", "en": "Make read-only"},
    "policy.empty_databases": {"zh-CN": "暂无可访问库。", "en": "No accessible databases."},
    "policy.manual_rules": {"zh-CN": "手动脱敏规则", "en": "Manual masking rules"},
    "policy.manual_hint": {
        "zh-CN": (
            "手动规则优先于自动识别；<code>off</code> 仅关闭该列的自动规则，"
            "<code>null</code> / <code>hash</code> / <code>mask</code> 为实际脱敏。"
        ),
        "en": (
            "Manual rules take precedence over automatic detection; <code>off</code> disables "
            "automatic masking for that column, while <code>null</code>, <code>hash</code>, and "
            "<code>mask</code> actually mask the data."
        ),
    },
    "policy.not_exposed": {"zh-CN": "未暴露", "en": "Not exposed"},
    "policy.strategy.null": {"zh-CN": "置空", "en": "Null"},
    "policy.strategy.hash": {"zh-CN": "哈希", "en": "Hash"},
    "policy.strategy.off": {"zh-CN": "关闭自动", "en": "Disable auto"},
    "policy.strategy.mask": {"zh-CN": "掩码", "en": "Mask"},
    "policy.confirm_delete_rule": {"zh-CN": "确认删除该脱敏规则？", "en": "Delete this masking rule?"},
    "policy.confirm_required": {
        "zh-CN": "改为可写需勾选确认",
        "en": "Check the confirmation box before making it writable",
    },
    "policy.empty_rules": {
        "zh-CN": "暂无手动规则，仅按列名自动识别。",
        "en": "No manual rules; columns are detected automatically by name.",
    },
    "policy.add_rule": {"zh-CN": "新增规则", "en": "Add rule"},
    "policy.pattern_empty": {"zh-CN": "（非 mask 留空）", "en": "(leave blank unless mask)"},
    "policy.add_rule_submit": {"zh-CN": "添加规则", "en": "Add rule"},
    "policy.auto_keywords": {"zh-CN": "自动识别关键词", "en": "Automatic detection keywords"},
    "policy.auto_hint": {
        "zh-CN": "按列名子串匹配、大小写不敏感，命中顺序优先。手动规则可覆盖。",
        "en": "Substring match on column names, case-insensitive, in priority order. Manual rules override.",
    },
    "policy.priority": {"zh-CN": "优先级", "en": "Priority"},
    "policy.keywords": {"zh-CN": "关键词", "en": "Keywords"},
    # Tokens
    "token.new_created": {"zh-CN": "新 Token 已创建", "en": "New token created"},
    "token.plaintext_warning": {
        "zh-CN": "明文仅本次展示，关闭页面后无法再次查看，请立即复制并妥善保存。",
        "en": "The plaintext is shown only once. Copy it now and store it safely; it cannot be viewed again.",
    },
    "token.create_title": {"zh-CN": "创建 Token", "en": "Create token"},
    "token.username": {"zh-CN": "用户名", "en": "Username"},
    "token.one_enabled_hint": {
        "zh-CN": "每个用户同时只能有一个启用 Token；停用后立即失效。",
        "en": "Each user can have one enabled token at a time; disabling takes effect immediately.",
    },
    "token.list_title": {"zh-CN": "Token 列表", "en": "Tokens"},
    "token.table.prefix": {"zh-CN": "前缀", "en": "Prefix"},
    "token.table.scope": {"zh-CN": "授权范围", "en": "Scope"},
    "token.table.created": {"zh-CN": "创建时间", "en": "Created"},
    "token.table.disabled": {"zh-CN": "停用时间", "en": "Disabled"},
    "token.scope_tooltip": {
        "zh-CN": "已限制数据源 / 表 / 字段",
        "en": "Restricted by datasource / table / column",
    },
    "token.grant": {"zh-CN": "授权", "en": "Scope"},
    "token.confirm_disable": {
        "zh-CN": "停用后该 Token 立即失效，确认？",
        "en": "Disabling makes this token invalid immediately. Continue?",
    },
    "token.confirm_delete": {"zh-CN": "确认删除该 Token 记录？", "en": "Delete this token record?"},
    "token.empty": {"zh-CN": "还没有 Token。", "en": "No tokens yet."},
    "token.scope_no_rotate": {
        "zh-CN": "修改授权范围不会轮换 Token 明文，已发放的 Token 继续使用新范围。",
        "en": "Changing the scope does not rotate the token plaintext; issued tokens keep working with the new scope.",
    },
    "token.save_scope": {"zh-CN": "保存授权范围", "en": "Save scope"},
    # Scope picker (server-rendered shell)
    "scope.title": {"zh-CN": "授权范围", "en": "Scope"},
    "scope.hint": {
        "zh-CN": "分「数据源 → 表 → 字段」三级授权。全部态包含以后新增对象；指定态只包含当前选择。",
        "en": (
            "Three levels: datasource → table → column. “All” includes objects added later; "
            "“selected” includes only the current selection."
        ),
    },
    "scope.copy": {"zh-CN": "复制范围", "en": "Copy scope"},
    "scope.template": {"zh-CN": "模板", "en": "Templates"},
    "scope.preview": {"zh-CN": "查看预览", "en": "Preview"},
    "scope.reset": {"zh-CN": "重置为全部", "en": "Reset to all"},
    "scope.datasource_scope": {"zh-CN": "数据源范围", "en": "Datasource scope"},
    "scope.all_datasources": {"zh-CN": "全部数据源", "en": "All datasources"},
    "scope.all_datasources_hint": {
        "zh-CN": "含以后新增，最高权限",
        "en": "Includes future datasources; maximum access",
    },
    "scope.specified_datasources": {"zh-CN": "指定数据源", "en": "Selected datasources"},
    "scope.specified_datasources_hint": {
        "zh-CN": "只授权当前勾选的数据源",
        "en": "Only the datasources selected now",
    },
    "scope.loading": {"zh-CN": "正在读取授权范围…", "en": "Loading scope…"},
    "scope.all_note": {
        "zh-CN": "当前为「全部数据源」：该 Token 可访问所有数据源、全部表与字段，包括以后新增的对象。",
        "en": (
            "Currently “all datasources”: this token can access every datasource, table, and column, "
            "including objects added later."
        ),
    },
    "scope.switch_to_specified": {
        "zh-CN": "改为指定数据源",
        "en": "Switch to selected datasources",
    },
    "scope.bulk_label": {"zh-CN": "数据源批量：", "en": "Datasource actions:"},
    "scope.select_all_datasources": {"zh-CN": "全选数据源", "en": "Select all datasources"},
    "scope.invert_datasources": {"zh-CN": "反选数据源", "en": "Invert datasource selection"},
    "scope.clear_datasources": {"zh-CN": "清空数据源", "en": "Clear datasources"},
    "scope.all_tables": {"zh-CN": "全部表", "en": "All tables"},
    "scope.pick_tables": {"zh-CN": "选择表", "en": "Select tables"},
    "scope.import_list": {"zh-CN": "清单导入", "en": "Import list"},
    "scope.empty_datasources": {"zh-CN": "尚未配置数据源。", "en": "No datasources configured yet."},
    # Credentials
    "credential.issue_new": {"zh-CN": "签发新凭证", "en": "Issue credential"},
    "credential.export_disabled": {
        "zh-CN": "导出通道未启用，签发的凭证在通道启用前无法使用。",
        "en": "The export channel is disabled; issued credentials cannot be used until it is enabled.",
    },
    "credential.list_title": {"zh-CN": "凭证列表", "en": "Credentials"},
    "credential.table.account": {"zh-CN": "账号", "en": "Account"},
    "credential.table.database": {"zh-CN": "目标库", "en": "Target database"},
    "credential.table.issuer": {"zh-CN": "签发人", "en": "Issued by"},
    "credential.table.source_ip": {"zh-CN": "来源 IP", "en": "Source IP"},
    "credential.table.expires": {"zh-CN": "过期时间", "en": "Expires"},
    "credential.table.last_used": {"zh-CN": "最后使用", "en": "Last used"},
    "credential.status.active": {"zh-CN": "有效", "en": "Active"},
    "credential.status.expired": {"zh-CN": "已过期", "en": "Expired"},
    "credential.status.revoked": {"zh-CN": "已撤销", "en": "Revoked"},
    "credential.confirm_revoke": {
        "zh-CN": "撤销后该凭证立即无法再建立新连接，确认？",
        "en": "After revocation this credential can no longer open new connections. Continue?",
    },
    "credential.revoke": {"zh-CN": "撤销", "en": "Revoke"},
    "credential.empty": {
        "zh-CN": "还没有临时导出凭证。",
        "en": "No temporary export credentials yet.",
    },
    "credential.no_db_for_token": {
        "zh-CN": "该 Token 在此数据源下没有可用库，请先调整授权。",
        "en": "This token has no available database on this datasource; adjust its scope first.",
    },
    "credential.token_user": {"zh-CN": "Token 用户", "en": "Token user"},
    "credential.datasource_filtered": {
        "zh-CN": "（按 Token 授权过滤）",
        "en": "(filtered by token scope)",
    },
    "credential.target_db_hint": {
        "zh-CN": "（可访问 ∩ 该 Token 已授权）",
        "en": "(accessible ∩ granted to this token)",
    },
    "credential.ttl_hours": {"zh-CN": "有效期（小时）", "en": "Lifetime (hours)"},
    "credential.source_ip_label": {"zh-CN": "允许来源 IP", "en": "Allowed source IP"},
    "credential.source_ip_hint": {"zh-CN": "（仅该地址可连）", "en": "(only this address may connect)"},
    "credential.quick_ttl": {"zh-CN": "快捷有效期：", "en": "Quick TTL:"},
    "credential.hours": {"zh-CN": "{n} 小时", "en": "{n} hours"},
    "credential.help": {
        "zh-CN": (
            "凭证继承所选 Token 的授权范围，目标库签发即固化（要换库就撤销重签）；"
            "同一数据源下的其他可用库可在连接后 USE。有效期最长 {max} 小时，密码由系统生成，"
            "仅在签发结果页展示一次。"
        ),
        "en": (
            "The credential inherits the selected token's scope; the target database is fixed at issue "
            "time (revoke and reissue to change it). Other available databases on the same datasource "
            "can be selected with USE after connecting. The maximum lifetime is {max} hours; the "
            "password is generated and shown once on the result page."
        ),
    },
    "credential.ip_bind_help": {
        "zh-CN": (
            "凭证按「允许来源 IP」绑定：只有从该地址发起的 MySQL 连接才能使用。"
            "签发给别的机器上的脚本时，改成那台机器访问网关的出口 IP（不是网关地址）。"
        ),
        "en": (
            "Credentials are bound to the allowed source IP: only MySQL connections from that address "
            "can use them. When issuing for a script on another machine, use that machine's egress IP "
            "towards the gateway (not the gateway address)."
        ),
    },
    "credential.issue": {"zh-CN": "签发", "en": "Issue"},
    "credential.no_tokens": {
        "zh-CN": "没有可用的启用 Token，请先到 {link} 页面创建。",
        "en": "No enabled token is available. Create one on the {link} page.",
    },
    "credential.password_warning": {
        "zh-CN": "密码仅本次展示，离开本页后不可再查看，遗失只能重新签发。",
        "en": "The password is shown only once. If lost, issue a new credential.",
    },
    "credential.database_notice": {
        "zh-CN": (
            "本凭证的默认落点已固定为库 {db}；"
            "同一数据源下的其他可用库可在连接后 USE，其他数据源需分别签发。"
        ),
        "en": (
            "This credential defaults to database {db}; other available databases on the same "
            "datasource can be selected with USE after connecting, and other datasources need separate credentials."
        ),
    },
    "credential.ip_bind_note": {
        "zh-CN": "只有从「允许来源 IP」发起的 MySQL 连接才能使用该凭证，来源不一致会在握手阶段被拒。",
        "en": (
            "Only MySQL connections from the allowed source IP can use this credential; a different "
            "source is rejected during the handshake."
        ),
    },
    "credential.plaintext_password": {"zh-CN": "明文密码", "en": "Plaintext password"},
    "credential.copy_immediately": {
        "zh-CN": "请立即复制并通过安全渠道交付给使用方。",
        "en": "Copy it now and deliver it to the consumer over a secure channel.",
    },
    "credential.mysql_client": {"zh-CN": "MySQL 客户端", "en": "MySQL client"},
    "credential.gateway_placeholder": {"zh-CN": "<网关地址>", "en": "<gateway-host>"},
    # Audit
    "audit.from": {"zh-CN": "开始时间（本地）", "en": "From (local)"},
    "audit.to": {"zh-CN": "结束时间（本地）", "en": "To (local)"},
    "audit.keyword": {"zh-CN": "关键词", "en": "Keyword"},
    "audit.keyword_placeholder": {
        "zh-CN": "SQL / 用户 / 数据源 / 凭证账号",
        "en": "SQL / user / datasource / credential account",
    },
    "audit.filter": {"zh-CN": "筛选", "en": "Filter"},
    "audit.reset": {"zh-CN": "重置", "en": "Reset"},
    "audit.table.event": {"zh-CN": "事件", "en": "Event"},
    "audit.table.credential": {"zh-CN": "凭证账号", "en": "Credential account"},
    "audit.table.deny": {"zh-CN": "拒绝原因", "en": "Deny reason"},
    "audit.table.rows": {"zh-CN": "行数", "en": "Rows"},
    "audit.empty": {
        "zh-CN": "没有符合条件的审计记录。",
        "en": "No audit records match the filters.",
    },
    "audit.detail.request_info": {"zh-CN": "请求信息", "en": "Request information"},
    "audit.detail.event_type": {"zh-CN": "事件类型", "en": "Event type"},
    "audit.detail.token_prefix": {"zh-CN": "Token 前缀", "en": "Token prefix"},
    "audit.detail.client_ip": {"zh-CN": "客户端 IP", "en": "Client IP"},
    "audit.detail.started": {"zh-CN": "开始时间", "en": "Started"},
    "audit.detail.finished": {"zh-CN": "结束时间", "en": "Finished"},
    "audit.detail.rows": {"zh-CN": "影响/返回行数", "en": "Affected/returned rows"},
    "audit.detail.datasource_id": {"zh-CN": "（ID {id}）", "en": "(ID {id})"},
    "audit.detail.no_sql": {"zh-CN": "本次调用没有 SQL。", "en": "This call had no SQL."},
    "audit.detail.error": {"zh-CN": "错误详情", "en": "Error details"},
    "audit.status.pending": {"zh-CN": "执行中", "en": "Running"},
    "audit.status.success": {"zh-CN": "成功", "en": "Success"},
    "audit.status.denied": {"zh-CN": "已拒绝", "en": "Denied"},
    "audit.status.error": {"zh-CN": "失败", "en": "Failed"},
    "audit.status.interrupted": {"zh-CN": "已中断", "en": "Interrupted"},
    "audit.event.mcp_tool": {"zh-CN": "MCP 调用", "en": "MCP call"},
    "audit.event.auth_failure": {"zh-CN": "认证失败", "en": "Authentication failure"},
    "audit.event.mysql_export": {"zh-CN": "MySQL 导出", "en": "MySQL export"},
    "audit.event.mysql_auth": {"zh-CN": "导出认证", "en": "Export authentication"},
    "audit.event.credential_lifecycle": {"zh-CN": "凭证生命周期", "en": "Credential lifecycle"},
    "audit.event.exchange": {"zh-CN": "凭证换发", "en": "Credential exchange"},
    "audit.event.setting_change": {"zh-CN": "设置变更", "en": "Setting change"},
    "audit.event.policy_change": {"zh-CN": "策略变更", "en": "Policy change"},
    # Settings
    "settings.change_password": {"zh-CN": "修改密码", "en": "Change password"},
    "settings.current_password": {"zh-CN": "当前密码", "en": "Current password"},
    "settings.new_password": {
        "zh-CN": "新密码（至少 8 字符）",
        "en": "New password (at least 8 characters)",
    },
    "settings.confirm_password": {"zh-CN": "确认新密码", "en": "Confirm new password"},
    "settings.password_hint": {
        "zh-CN": "修改成功后所有已登录会话立即失效，需要重新登录。",
        "en": "After a successful change every signed-in session is invalidated and must sign in again.",
    },
    "settings.change_password_submit": {"zh-CN": "修改密码", "en": "Change password"},
    "settings.audit_retention": {"zh-CN": "审计保留", "en": "Audit retention"},
    "settings.retention_hint": {
        "zh-CN": "每日 03:00（Asia/Shanghai）自动清理；也可立即手动清理。",
        "en": "Cleaned automatically every day at 03:00 Asia/Shanghai; you can also clean up now.",
    },
    "settings.retention_days": {"zh-CN": "保留天数（1-3650）", "en": "Retention days (1-3650)"},
    "settings.save_retention": {"zh-CN": "保存保留天数", "en": "Save retention days"},
    "settings.confirm_cleanup": {
        "zh-CN": "将立即删除超过保留期的审计记录，确认？",
        "en": "Audit records past the retention period will be deleted now. Continue?",
    },
    "settings.cleanup_now": {
        "zh-CN": "立即清理过期审计",
        "en": "Clean up expired audit records now",
    },
    "settings.allowlist": {"zh-CN": "来源 IP 白名单", "en": "Source IP allowlist"},
    "settings.allowlist_hint": {
        "zh-CN": (
            "每行一个 IP 或 CIDR（例：192.0.2.0/24、192.168.1.20），最多 50 条；"
            "保存后立即生效，不用改环境变量也不用重启。"
        ),
        "en": (
            "One IP or CIDR per line (for example 192.0.2.0/24 or 192.168.1.20), up to 50 entries. "
            "Changes take effect immediately; no environment change or restart is needed."
        ),
    },
    "settings.export_disabled_allowlist": {
        "zh-CN": "MySQL 导出通道当前未开启（GATEWAY_MYSQL_EXPORT_ENABLED=false），白名单在开启后生效。",
        "en": (
            "The MySQL export channel is disabled (GATEWAY_MYSQL_EXPORT_ENABLED=false); the allowlist "
            "takes effect once it is enabled."
        ),
    },
    "settings.allowlist_empty_note": {
        "zh-CN": "；在此留空并保存会改为全部拒绝",
        "en": "; saving an empty list here changes it to deny all",
    },
    "settings.source_ip_hint": {
        "zh-CN": (
            "判定来源：只有来自 GATEWAY_TRUSTED_PROXIES 网段的连接才采信 X-Forwarded-For，"
            "其余一律取 TCP 对端地址；导出通道始终取 TCP 对端地址，不可伪造。"
        ),
        "en": (
            "Source resolution: X-Forwarded-For is trusted only for connections from "
            "GATEWAY_TRUSTED_PROXIES networks; otherwise the TCP peer address is used. The export "
            "channel always uses the TCP peer address and cannot be spoofed."
        ),
    },
    "settings.save_allowlist": {"zh-CN": "保存白名单", "en": "Save allowlist"},
    "settings.allowlist_missing": {
        "zh-CN": "缺少白名单字段：{fields}",
        "en": "Missing allowlist fields: {fields}",
    },
    # Error pages
    "error.not_found": {"zh-CN": "未找到", "en": "Not found"},
    "error.request_rejected": {"zh-CN": "请求被拒绝", "en": "Request rejected"},
    "error.bad_parameters": {"zh-CN": "参数不正确", "en": "Invalid parameters"},
    "error.bad_parameters_message": {
        "zh-CN": "请检查表单内容后重试。",
        "en": "Check the form and try again.",
    },
    "error.request_failed": {"zh-CN": "请求失败", "en": "Request failed"},
    "error.page_not_found": {"zh-CN": "页面不存在", "en": "Page not found"},
    "error.page_not_found_message": {
        "zh-CN": "请求的页面不存在。",
        "en": "The requested page does not exist.",
    },
    "error.service_error": {"zh-CN": "服务异常", "en": "Service error"},
    "error.internal_message": {
        "zh-CN": "网关内部错误，请联系管理员。",
        "en": "Gateway internal error; contact the administrator.",
    },
    "error.back_dashboard": {"zh-CN": "返回仪表盘", "en": "Back to dashboard"},
    "error.go_login": {"zh-CN": "前往登录", "en": "Go to sign-in"},
    # Parameterised service errors
    "error.field.required": {
        "zh-CN": "{field}不能为空且长度不超过 {limit}",
        "en": "{field} is required and must be at most {limit} characters",
    },
    "error.field.required_spaced": {
        "zh-CN": "{field} 不能为空且长度不超过 {limit}",
        "en": "{field} is required and must be at most {limit} characters",
    },
    "error.field.invalid": {"zh-CN": "{field}格式错误", "en": "Invalid {field}"},
    "error.datasource.database_name_too_long": {
        "zh-CN": "库名 {name}… 长度不能超过 {limit}",
        "en": "Database name {name}… exceeds {limit} characters",
    },
    "error.datasource.duplicate_database": {
        "zh-CN": "可访问库清单包含重复项：{name}",
        "en": "Duplicate database in the accessible list: {name}",
    },
    "error.datasource.connection_failed": {
        "zh-CN": "连接失败{detail}，请检查地址、账号与网络",
        "en": "Connection failed{detail}; check the host, credentials, and network",
    },
    "error.datasource.error_code": {
        "zh-CN": "（错误码 {errno}）",
        "en": " (error code {errno})",
    },
    "error.datasource.list_failed": {
        "zh-CN": "读取库清单失败{detail}，请检查地址、账号与网络",
        "en": "Failed to read the database list{detail}; check the host, credentials, and network",
    },
    "error.masking.database_outside": {
        "zh-CN": "库 {database} 不在可访问库内",
        "en": "Database {database} is not in the accessible list",
    },
    "error.scope.whole_database_limit": {
        "zh-CN": "库级「全部表」不能超过 {limit} 个，请改用数据源级「全部表」",
        "en": (
            "At most {limit} whole-database grants are allowed; use whole-datasource grants instead"
        ),
    },
    "error.scope.datasource_limit": {
        "zh-CN": "数据源选择不能超过 {limit} 个",
        "en": "At most {limit} datasources can be selected",
    },
    "error.scope.table_limit": {
        "zh-CN": "表选择不能超过 {limit} 张",
        "en": "At most {limit} tables can be selected",
    },
    "error.scope.column_limit": {
        "zh-CN": "字段选择不能超过 {limit} 个",
        "en": "At most {limit} columns can be selected",
    },
    "error.token.username_required": {
        "zh-CN": "用户名不能为空且长度不超过 {limit}",
        "en": "Username is required and must be at most {limit} characters",
    },
    "error.token.actor_policy_invalid": {
        "zh-CN": "actor_policy 必须是 ignore/require 之一",
        "en": "actor_policy must be one of ignore or require",
    },
    "error.token.missing_datasources": {
        "zh-CN": "授权范围包含不存在的数据源：{ids}",
        "en": "Scope includes datasources that do not exist: {ids}",
    },
    "error.token.kind_invalid": {
        "zh-CN": "token_kind 必须是 access/exchange 之一",
        "en": "token_kind must be one of access or exchange",
    },
    "error.token.exchange_policy_locked": {
        "zh-CN": "换发型 Token 的行为人策略固定为 require，不可改为 ignore",
        "en": "Exchange tokens always require an actor assertion; the policy cannot be set to ignore",
    },
    "error.exchange.ttl_range": {
        "zh-CN": "有效期必须为 1-{max} 分钟",
        "en": "Lifetime must be between 1 and {max} minutes",
    },
    "error.exchange.source_ip_required": {
        "zh-CN": "无法确定申请来源 IP，拒绝签发未绑定来源的短期凭证",
        "en": "Cannot determine the requesting source IP; refusing to issue an unbound short credential",
    },
    "error.exchange.token_required": {
        "zh-CN": "换发端点只接受换发型 Token（token_kind=exchange）",
        "en": "The exchange endpoint only accepts exchange tokens (token_kind=exchange)",
    },
    "error.exchange.access_denied": {
        "zh-CN": "换发型 Token 只能用于换发短期凭证，不能访问数据源",
        "en": "Exchange tokens may only mint short credentials; they cannot access datasources",
    },
    "error.actor.mismatch": {
        "zh-CN": "身份头与短期凭证绑定的行为人不一致",
        "en": "Identity headers do not match the actor bound to this short credential",
    },
    "error.actor.required": {
        "zh-CN": "缺少终端用户身份头（X-BaseWard-Actor-*），代理型 Token 必须携带",
        "en": "Missing end-user identity headers (X-BaseWard-Actor-*); agent tokens must carry them",
    },
    "error.actor.invalid": {
        "zh-CN": "终端用户身份头非法：{field}",
        "en": "Invalid end-user identity header: {field}",
    },
    "error.credential.source_ip_invalid": {
        "zh-CN": "来源 IP 不合法：{value}",
        "en": "Invalid source IP: {value}",
    },
    "error.credential.ttl_range": {
        "zh-CN": "有效期必须为 1-{max} 小时",
        "en": "Lifetime must be between 1 and {max} hours",
    },
    "error.credential.agent_token_no_export": {
        "zh-CN": "代理型 Token（actor_policy=require）不支持签发导出凭证",
        "en": "Agent tokens (actor_policy=require) cannot issue export credentials",
    },
    "error.allowlist.invalid": {
        "zh-CN": (
            "白名单条目非法：{entries}；每行一个 IP 或 CIDR，例如 192.0.2.0/24 或 192.168.1.20"
        ),
        "en": (
            "Invalid allowlist entries: {entries}; use one IP or CIDR per line, for example "
            "192.0.2.0/24 or 192.168.1.20"
        ),
    },
    "error.allowlist.too_many": {
        "zh-CN": "{label}白名单最多 {limit} 条，当前 {count} 条",
        "en": "The {label} allowlist accepts at most {limit} entries; currently {count}",
    },
    "error.allowlist.too_long": {
        "zh-CN": "{label}白名单规范化后 {length} 字符，超出 {limit} 上限，请合并网段",
        "en": (
            "The normalized {label} allowlist is {length} characters, over the {limit} limit; "
            "merge networks to shorten it"
        ),
    },
    "error.allowlist.host_bits": {
        "zh-CN": "{item}（网段含主机位，请写起始地址）",
        "en": "{item} (network has host bits; write the network address)",
    },
    "error.allowlist.more_items": {
        "zh-CN": "等 {n} 项",
        "en": "and {n} more",
    },
    # Scope summaries (also consumed by MCP responses)
    "scope.summary.broken": {
        "zh-CN": "配置异常（拒绝全部）",
        "en": "Misconfigured (deny all)",
    },
    "scope.summary.all": {"zh-CN": "全部", "en": "All"},
    "scope.summary.datasources": {"zh-CN": "数据源 {n}", "en": "Datasources {n}"},
    "scope.summary.datasources_all": {"zh-CN": "数据源 全部", "en": "Datasources: all"},
    "scope.summary.tables_all_datasources": {
        "zh-CN": "表 全部({n} 个数据源)",
        "en": "Tables: all ({n} datasources)",
    },
    "scope.summary.tables_all_and_explicit": {
        "zh-CN": "表 全部({n} 个数据源) + 指定 {m}",
        "en": "Tables: all ({n} datasources) + {m} selected",
    },
    "scope.summary.tables": {"zh-CN": "表 {n}", "en": "Tables {n}"},
    "scope.summary.tables_all": {"zh-CN": "表 全部", "en": "Tables: all"},
    "scope.summary.whole_databases": {
        "zh-CN": "整库 {n} 个",
        "en": "Whole databases: {n}",
    },
    "scope.summary.columns": {"zh-CN": "字段 {n}", "en": "Columns {n}"},
    "scope.summary.columns_all": {"zh-CN": "字段 全部", "en": "Columns: all"},
    # Allowlist descriptions
    "allowlist.source_db_entries": {
        "zh-CN": "当前生效：本页已保存的 {n} 条（{saved}）",
        "en": "Active: {n} entries saved on this page ({saved})",
    },
    "allowlist.source_db_empty": {
        "zh-CN": "本页已保存过且为空（{saved}）；当前拒绝所有连接",
        "en": "Saved as empty on this page ({saved}); all connections are currently denied",
    },
    "allowlist.source_env_entries": {
        "zh-CN": "当前生效：环境变量 {env} 的 {n} 条；本页保存后改按本页的值",
        "en": "Active: {n} entries from environment variable {env}; saving this page switches to its value",
    },
    "allowlist.source_allow_all": {
        "zh-CN": "当前不限制来源 IP（{env} 也为空）",
        "en": "Source IP is currently unrestricted ({env} is also empty)",
    },
    "allowlist.source_deny_all": {
        "zh-CN": "当前拒绝所有连接（{env} 也为空）",
        "en": "All connections are currently denied ({env} is also empty)",
    },
    # Dynamic JS text
    "js.copy_done": {"zh-CN": "已复制", "en": "Copied"},
    "js.copy_failed": {
        "zh-CN": "复制失败，请手动复制：",
        "en": "Copy failed; copy manually:",
    },
    "js.db_loading": {"zh-CN": "正在读取…", "en": "Loading…"},
    "js.db_loaded": {"zh-CN": "已读取 {n} 个库", "en": "Loaded {n} databases"},
    "js.db_load_failed": {
        "zh-CN": "读取库清单失败，请检查网络或连接参数",
        "en": "Failed to load databases; check the network or connection settings",
    },
    "js.db_select_first": {"zh-CN": "至少选择一个可访问库", "en": "Select at least one accessible database"},
    "js.db_default_required": {"zh-CN": "请选择默认库", "en": "Select a default database"},
    "js.db_placeholder": {
        "zh-CN": "（先勾选可访问库）",
        "en": "(select accessible databases first)",
    },
    "js.suggest_truncated": {
        "zh-CN": "匹配较多，仅显示前 50 条，请输入更精确的关键词。",
        "en": "Too many matches; showing the first 50. Enter a more specific keyword.",
    },
    "js.suggest_empty_table": {
        "zh-CN": "没有匹配的表名，可直接手动输入。",
        "en": "No matching table names; type one manually.",
    },
    "js.suggest_empty_column": {
        "zh-CN": "没有匹配的字段名，可直接手动输入。",
        "en": "No matching column names; type one manually.",
    },
    "js.suggest_need_database": {
        "zh-CN": "请先填写库名后再选择字段。",
        "en": "Enter a database name before selecting columns.",
    },
    "js.suggest_need_table": {
        "zh-CN": "请先填写表名后再选择字段。",
        "en": "Enter a table name before selecting columns.",
    },
    "js.suggest_loading": {"zh-CN": "正在加载…", "en": "Loading…"},
    "js.suggest_failed": {
        "zh-CN": "元数据加载失败，可直接手动输入。",
        "en": "Failed to load metadata; type values manually.",
    },
    "js.list_separator": {"zh-CN": "、", "en": ", "},
    "js.db_load_failed_short": {
        "zh-CN": "读取库清单失败",
        "en": "Failed to load the database list",
    },
    # Scope picker (token edit / new token)
    "js.scope.close": {"zh-CN": "关闭", "en": "Close"},
    "js.scope.apply": {"zh-CN": "应用", "en": "Apply"},
    "js.scope.cancel": {"zh-CN": "取消", "en": "Cancel"},
    "js.scope.delete": {"zh-CN": "删除", "en": "Delete"},
    "js.scope.copy": {"zh-CN": "复制", "en": "Copy"},
    "js.scope.search": {"zh-CN": "搜索", "en": "Search"},
    "js.scope.verify": {"zh-CN": "校验", "en": "Validate"},
    "js.scope.loading": {"zh-CN": "正在加载…", "en": "Loading…"},
    "js.scope.load_more": {"zh-CN": "加载更多", "en": "Load more"},
    "js.scope.show_more_selected": {
        "zh-CN": "显示更多已选",
        "en": "Show more selected",
    },
    "js.scope.pick_current": {"zh-CN": "选择当前结果", "en": "Select current results"},
    "js.scope.unpick_current": {
        "zh-CN": "取消当前结果",
        "en": "Deselect current results",
    },
    "js.scope.invert_current": {
        "zh-CN": "反选当前结果",
        "en": "Invert current results",
    },
    "js.scope.clear_selected": {"zh-CN": "清空已选", "en": "Clear selection"},
    "js.scope.grant_all_tables": {"zh-CN": "改为全部表", "en": "Switch to all tables"},
    "js.scope.results_tab": {"zh-CN": "搜索结果", "en": "Search results"},
    "js.scope.selected_tab": {"zh-CN": "已选 ({n})", "en": "Selected ({n})"},
    "js.scope.selected_tables": {"zh-CN": "已选 {n} 张表", "en": "{n} tables selected"},
    "js.scope.selected_columns": {
        "zh-CN": "已选 {n} 个字段",
        "en": "{n} columns selected",
    },
    "js.scope.datasource_number": {
        "zh-CN": "数据源 #{id}",
        "en": "Datasource #{id}",
    },
    "js.scope.missing_meta": {
        "zh-CN": "已不存在或配置异常",
        "en": "Missing or misconfigured",
    },
    "js.scope.needs_attention": {"zh-CN": "需处理", "en": "Needs attention"},
    "js.scope.missing_note": {
        "zh-CN": "该数据源已不存在，请取消勾选后保存。",
        "en": "This datasource no longer exists; uncheck it before saving.",
    },
    "js.scope.missing_mode": {"zh-CN": "已不存在", "en": "Missing"},
    "js.scope.mode_unselected": {"zh-CN": "未选择", "en": "Not selected"},
    "js.scope.mode_all_tables": {"zh-CN": "全部表", "en": "All tables"},
    "js.scope.mode_all_tables_note": {
        "zh-CN": "包含以后新增的表；不能再单独限制字段。",
        "en": "Includes tables added later; columns cannot be restricted separately.",
    },
    "js.scope.mode_no_tables": {"zh-CN": "无表权限", "en": "No table access"},
    "js.scope.mode_no_tables_note": {
        "zh-CN": "该数据源下所有表都会被拒绝，仅保留数据源授权。",
        "en": "All tables on this datasource are denied; only the datasource grant remains.",
    },
    "js.scope.whole_and_list": {
        "zh-CN": "整库 {db} 个库 · 指定 {tables} 张表",
        "en": "Whole databases: {db} · tables: {tables}",
    },
    "js.scope.list_only": {"zh-CN": "指定 {tables} 张表", "en": "Tables: {tables}"},
    "js.scope.whole_list_note": {
        "zh-CN": "整库放行的库包含以后新增的表；另有 {tables} 张指定表限制了字段。",
        "en": (
            "Whole-database grants include tables added later; "
            "{tables} selected tables have column restrictions."
        ),
    },
    "js.scope.field_note": {
        "zh-CN": "其中 {tables} 张表限制了字段。",
        "en": "{tables} of these tables have column restrictions.",
    },
    "js.scope.fields_open_note": {
        "zh-CN": "未限制字段的表默认开放全部字段。",
        "en": "Tables without column restrictions expose all columns.",
    },
    "js.scope.summary_all": {
        "zh-CN": "全部数据源 · 全部表 · 全部字段",
        "en": "All datasources · all tables · all columns",
    },
    "js.scope.summary_all_note": {
        "zh-CN": "包含以后新增的数据源、表与字段，权限最大。",
        "en": "Includes datasources, tables, and columns added later; widest access.",
    },
    "js.scope.summary_selected": {
        "zh-CN": "已选择 {n} 个数据源",
        "en": "{n} datasources selected",
    },
    "js.scope.summary_none": {
        "zh-CN": "尚未选择数据源",
        "en": "No datasource selected",
    },
    "js.scope.counts": {
        "zh-CN": (
            "表：全部 {all} 个数据源 · 整库 {dbs} 个库 · 指定 {tables} 张（{list_ds} 个数据源）"
            " · 无权限 {none} 个；字段：指定 {cols} 个（涉及 {field_tables} 张表）"
        ),
        "en": (
            "Tables: all {all} datasources · whole databases {dbs} · selected {tables} "
            "({list_ds} datasources) · no access {none}; columns: {cols} selected "
            "({field_tables} tables)"
        ),
    },
    "js.scope.warn_no_ds": {
        "zh-CN": "指定模式下至少需要选择一个数据源，否则无法保存。",
        "en": "Select at least one datasource in specified mode; otherwise the scope cannot be saved.",
    },
    "js.scope.warn_none": {
        "zh-CN": "有 {n} 个数据源设为无表权限，其下所有查询都会被拒绝。",
        "en": "{n} datasources have no table access; all queries on them will be denied.",
    },
    "js.scope.warn_missing": {
        "zh-CN": "有 {n} 个已选数据源已不存在，请取消勾选后保存。",
        "en": "{n} selected datasources no longer exist; uncheck them before saving.",
    },
    "js.scope.limits": {
        "zh-CN": "上限：数据源 {ds} · 表 {tables} · 字段 {columns}",
        "en": "Limits: {ds} datasources · {tables} tables · {columns} columns",
    },
    "js.scope.limits_current": {
        "zh-CN": "（当前已选 {tables} 张表、{columns} 个字段）",
        "en": " (currently {tables} tables, {columns} columns selected)",
    },
    "js.scope.validate_no_ds": {
        "zh-CN": "请至少选择一个数据源，或改为「全部数据源」。",
        "en": 'Select at least one datasource, or switch to "all datasources".',
    },
    "js.scope.validate_ds_limit": {
        "zh-CN": "数据源选择不能超过 {limit} 个。",
        "en": "At most {limit} datasources can be selected.",
    },
    "js.scope.validate_table_limit": {
        "zh-CN": "表选择不能超过 {limit} 张。",
        "en": "At most {limit} tables can be selected.",
    },
    "js.scope.validate_column_limit": {
        "zh-CN": "字段选择不能超过 {limit} 个。",
        "en": "At most {limit} columns can be selected.",
    },
    "js.scope.validate_payload": {
        "zh-CN": "授权范围过大，请减少表或字段选择。",
        "en": "Scope is too large; select fewer tables or columns.",
    },
    "js.scope.confirm_all": {
        "zh-CN": "改为「全部数据源」会放开当前所有限制，并包含以后新增的数据源、表与字段。确认继续？",
        "en": (
            'Switching to "all datasources" removes all current restrictions and includes '
            "datasources, tables, and columns added later. Continue?"
        ),
    },
    "js.scope.confirm_unselect": {
        "zh-CN": "取消选择「{name}」会清除该数据源下的整库、表与字段选择。确认继续？",
        "en": 'Unchecking "{name}" clears its whole-database, table, and column selections. Continue?',
    },
    "js.scope.confirm_all_tables": {
        "zh-CN": "改为「全部表」会清除该数据源下已选的 {n} 张表、整库授权与字段限制。确认继续？",
        "en": (
            'Switching to "all tables" clears {n} selected tables, whole-database grants, '
            "and column restrictions on this datasource. Continue?"
        ),
    },
    "js.scope.confirm_invert": {
        "zh-CN": "反选数据源会清除被取消勾选的数据源下的整库、表与字段选择。确认继续？",
        "en": (
            "Inverting datasources clears whole-database, table, and column selections "
            "on the unchecked datasources. Continue?"
        ),
    },
    "js.scope.confirm_clear": {
        "zh-CN": "清空后会取消所有已选数据源及其整库、表、字段选择。确认继续？",
        "en": (
            "Clearing removes all selected datasources and their whole-database, table, "
            "and column selections. Continue?"
        ),
    },
    "js.scope.table_picker_title": {
        "zh-CN": "选择表 · {name}",
        "en": "Select tables · {name}",
    },
    "js.scope.table_picker_subtitle": {
        "zh-CN": "默认库：{db}；支持按表名或字段名搜索。",
        "en": "Default database: {db}; search by table or column name.",
    },
    "js.scope.not_set": {"zh-CN": "未设置", "en": "not set"},
    "js.scope.mode_all_tables_short": {
        "zh-CN": "含以后新增，不能再限制字段",
        "en": "Includes future tables; columns cannot be restricted",
    },
    "js.scope.mode_list_tables": {"zh-CN": "指定表", "en": "Selected tables"},
    "js.scope.mode_list_tables_short": {
        "zh-CN": "逐张选择，可再限制字段",
        "en": "Pick tables individually; columns can be restricted",
    },
    "js.scope.search_tables_placeholder": {
        "zh-CN": "搜索表名 / 注释 / 字段名",
        "en": "Search table name / comment / column name",
    },
    "js.scope.match_table": {"zh-CN": "仅匹配表名", "en": "Match table names only"},
    "js.scope.match_column": {
        "zh-CN": "表名或字段名",
        "en": "Table or column names",
    },
    "js.scope.badge_columns": {"zh-CN": "字段 {n}", "en": "Columns {n}"},
    "js.scope.field_button": {"zh-CN": "字段", "en": "Columns"},
    "js.scope.no_matching_tables": {
        "zh-CN": "没有匹配的表。",
        "en": "No matching tables.",
    },
    "js.scope.no_tables": {
        "zh-CN": "该数据源下没有可授权的表。",
        "en": "This datasource has no grantable tables.",
    },
    "js.scope.total_tables": {"zh-CN": "共 {total} 张表", "en": "{total} tables"},
    "js.scope.total_tables_loaded": {
        "zh-CN": "共 {total} 张表，已加载 {loaded}",
        "en": "{total} tables, {loaded} loaded",
    },
    "js.scope.none_selected_tables": {
        "zh-CN": "尚未选择任何表。",
        "en": "No tables selected.",
    },
    "js.scope.all_databases": {"zh-CN": "全部库", "en": "All databases"},
    "js.scope.load_failed": {
        "zh-CN": "加载失败：{error}",
        "en": "Load failed: {error}",
    },
    "js.scope.confirm_draft_all": {
        "zh-CN": "改为「全部表」会清除当前已选的 {n} 张表及字段限制。确认继续？",
        "en": (
            'Switching to "all tables" clears {n} selected tables and their column '
            "restrictions. Continue?"
        ),
    },
    "js.scope.draft_all_note": {
        "zh-CN": "该数据源授权全部表（含以后新增），不能单独限制字段。",
        "en": (
            "This datasource grants all tables (including future ones); "
            "columns cannot be restricted separately."
        ),
    },
    "js.scope.draft_apply_all": {
        "zh-CN": "改为「全部表」会清除该数据源已选的 {n} 张表、整库授权及字段限制。确认继续？",
        "en": (
            'Switching to "all tables" clears {n} selected tables, whole-database grants, '
            "and column restrictions on this datasource. Continue?"
        ),
    },
    "js.scope.confirm_no_tables": {
        "zh-CN": "未选择任何表，该数据源将保存为「无表权限」，其下所有查询都会被拒绝。确认继续？",
        "en": (
            'No tables selected: this datasource will be saved with "no table access", '
            "and all queries on it will be denied. Continue?"
        ),
    },
    "js.scope.confirm_narrow_dbs": {
        "zh-CN": "库 {dbs} 已整库放行，改为指定表会把该库收窄到所选表。确认继续？",
        "en": (
            "Databases {dbs} are granted whole; switching to selected tables narrows them "
            "to the selected tables. Continue?"
        ),
    },
    "js.scope.confirm_clear_draft": {
        "zh-CN": "清空已选的 {n} 张表及字段限制？",
        "en": "Clear the {n} selected tables and their column restrictions?",
    },
    "js.scope.field_picker_title": {
        "zh-CN": "选择字段 · {target}",
        "en": "Select columns · {target}",
    },
    "js.scope.field_picker_subtitle": {
        "zh-CN": "数据源：{name}",
        "en": "Datasource: {name}",
    },
    "js.scope.all_columns": {"zh-CN": "全部字段", "en": "All columns"},
    "js.scope.all_columns_short": {
        "zh-CN": "默认，含以后新增字段",
        "en": "Default; includes future columns",
    },
    "js.scope.list_columns": {"zh-CN": "指定字段", "en": "Selected columns"},
    "js.scope.list_columns_short": {
        "zh-CN": "仅授权勾选的字段",
        "en": "Only checked columns are granted",
    },
    "js.scope.search_columns_placeholder": {
        "zh-CN": "搜索字段名 / 注释",
        "en": "Search column name / comment",
    },
    "js.scope.no_matching_columns": {
        "zh-CN": "没有匹配的字段。",
        "en": "No matching columns.",
    },
    "js.scope.no_columns": {
        "zh-CN": "该表没有可读取的字段。",
        "en": "This table has no readable columns.",
    },
    "js.scope.sensitive": {"zh-CN": "敏感", "en": "Sensitive"},
    "js.scope.total_columns": {"zh-CN": "共 {total} 个字段", "en": "{total} columns"},
    "js.scope.total_columns_loaded": {
        "zh-CN": "共 {total} 个字段，已加载 {loaded}",
        "en": "{total} columns, {loaded} loaded",
    },
    "js.scope.require_one_column": {
        "zh-CN": "指定字段时至少需要选择 1 个字段，或改为「全部字段」。",
        "en": 'When selecting columns, choose at least one, or switch to "all columns".',
    },
    "js.scope.confirm_clear_columns": {
        "zh-CN": "清空已选的 {n} 个字段？",
        "en": "Clear the {n} selected columns?",
    },
    "js.scope.import_title": {
        "zh-CN": "清单导入 · {name}",
        "en": "Import list · {name}",
    },
    "js.scope.import_subtitle": {
        "zh-CN": "每行一条：库.表、库.表.字段，或只写表名（使用默认库）。最多 {limit} 行。",
        "en": (
            "One entry per line: database.table, database.table.column, or just a table name "
            "(uses the default database). Up to {limit} lines."
        ),
    },
    "js.scope.import_placeholder": {
        "zh-CN": "例如：\n{lines}",
        "en": "Example:\n{lines}",
    },
    "js.scope.import_invalid_lines": {
        "zh-CN": "第 {lines} 行格式不正确。",
        "en": "Invalid format on line(s) {lines}.",
    },
    "js.scope.import_empty": {
        "zh-CN": "没有可导入的条目。",
        "en": "Nothing to import.",
    },
    "js.scope.import_too_many": {
        "zh-CN": "条目超过 {limit} 条，请分批导入。",
        "en": "More than {limit} entries; import in batches.",
    },
    "js.scope.import_validating": {
        "zh-CN": "正在校验 {n} 条…",
        "en": "Validating {n} entries…",
    },
    "js.scope.import_ok": {
        "zh-CN": "校验通过 {n} 条，已加入本次选择。",
        "en": "{n} entries validated and added to this selection.",
    },
    "js.scope.import_missing": {
        "zh-CN": "以下 {n} 条在数据源中不存在：",
        "en": "The following {n} entries do not exist in the datasource:",
    },
    "js.scope.show_first_50": {
        "zh-CN": "仅显示前 50 条。",
        "en": "Showing the first 50.",
    },
    "js.scope.import_failed": {
        "zh-CN": "校验失败：{error}",
        "en": "Validation failed: {error}",
    },
    "js.scope.confirm_import_narrow": {
        "zh-CN": "导入后该数据源将从「全部表 / 整库放行」改为「指定 {n} 张表」。确认继续？",
        "en": (
            'After import this datasource switches from "all tables / whole-database" '
            "to {n} selected tables. Continue?"
        ),
    },
    "js.scope.copied_applied": {
        "zh-CN": "已应用：{label}。请确认预览后保存。",
        "en": "Applied {label}. Review the preview, then save.",
    },
    "js.scope.copy_title": {
        "zh-CN": "复制已有 Token 的授权范围",
        "en": "Copy scope from an existing token",
    },
    "js.scope.copy_subtitle": {
        "zh-CN": "选择一个 Token，把它的范围复制到当前编辑器（不会修改被复制的 Token）。",
        "en": (
            "Choose a token to copy its scope into this editor "
            "(the copied token is not modified)."
        ),
    },
    "js.scope.copy_loading": {
        "zh-CN": "正在加载 Token 列表…",
        "en": "Loading tokens…",
    },
    "js.scope.copy_empty": {
        "zh-CN": "暂无可复制的 Token。",
        "en": "No token to copy.",
    },
    "js.scope.enabled": {"zh-CN": "启用", "en": "Enabled"},
    "js.scope.disabled": {"zh-CN": "停用", "en": "Disabled"},
    "js.scope.current_token": {"zh-CN": "当前 Token", "en": "Current token"},
    "js.scope.confirm_copy_token": {
        "zh-CN": "用「{name}」的范围替换当前选择？",
        "en": 'Replace the current selection with the scope of "{name}"?',
    },
    "js.scope.copy_failed": {
        "zh-CN": "复制失败：{error}",
        "en": "Copy failed: {error}",
    },
    "js.scope.storage_failed": {
        "zh-CN": "保存模板失败：浏览器本地存储不可用。",
        "en": "Failed to save the template: browser local storage is unavailable.",
    },
    "js.scope.template_title": {"zh-CN": "授权范围模板", "en": "Scope templates"},
    "js.scope.template_subtitle": {
        "zh-CN": "模板保存在本机浏览器，只用于快速填充，不会上传。",
        "en": "Templates are stored in this browser for quick filling only and are never uploaded.",
    },
    "js.scope.template_name_placeholder": {
        "zh-CN": "模板名称",
        "en": "Template name",
    },
    "js.scope.template_save": {
        "zh-CN": "保存当前范围",
        "en": "Save current scope",
    },
    "js.scope.template_empty": {"zh-CN": "暂无模板。", "en": "No templates yet."},
    "js.scope.untitled": {"zh-CN": "未命名", "en": "Untitled"},
    "js.scope.template_stats": {
        "zh-CN": "数据源 {ds} · 表 {tables} · 字段 {columns}",
        "en": "Datasources {ds} · tables {tables} · columns {columns}",
    },
    "js.scope.confirm_template_apply": {
        "zh-CN": "用模板「{name}」替换当前选择？",
        "en": 'Replace the current selection with template "{name}"?',
    },
    "js.scope.template_label": {"zh-CN": "模板「{name}」", "en": 'Template "{name}"'},
    "js.scope.confirm_template_delete": {
        "zh-CN": "删除模板「{name}」？",
        "en": 'Delete template "{name}"?',
    },
    "js.scope.template_name_required": {
        "zh-CN": "请填写模板名称。",
        "en": "Enter a template name.",
    },
    "js.scope.preview_all_tables": {
        "zh-CN": "全部表（含以后新增）",
        "en": "All tables (including future ones)",
    },
    "js.scope.preview_whole": {
        "zh-CN": "整库放行 {n} 个库（含以后新增）",
        "en": "{n} whole databases (including future tables)",
    },
    "js.scope.preview_list": {"zh-CN": "指定 {n} 张表", "en": "{n} selected tables"},
    "js.scope.preview_title": {"zh-CN": "授权范围预览", "en": "Scope preview"},
    "js.scope.preview_subtitle": {
        "zh-CN": "这是保存后 Token 实际可访问的范围。",
        "en": "The effective access this token will have after saving.",
    },
    "js.scope.preview_all": {
        "zh-CN": "全部数据源 · 全部表 · 全部字段（包含以后新增的对象）。",
        "en": "All datasources · all tables · all columns (including objects added later).",
    },
    "js.scope.preview_no_ds": {
        "zh-CN": "尚未选择任何数据源。",
        "en": "No datasource selected.",
    },
    "js.scope.whole_db_suffix": {
        "zh-CN": "{db}.*（整库）",
        "en": "{db}.* (whole database)",
    },
    "js.scope.column_suffix": {
        "zh-CN": "（字段 {n} 个）",
        "en": " ({n} columns)",
    },
    "js.scope.show_first_100": {
        "zh-CN": "仅显示前 100 张表。",
        "en": "Showing the first 100 tables.",
    },
    "js.scope.confirm_reset": {
        "zh-CN": "将放弃当前所有限制，改为「全部数据源 · 全部表 · 全部字段」。确认继续？",
        "en": (
            'This discards all current restrictions and switches to "all datasources · '
            'all tables · all columns". Continue?'
        ),
    },
    "js.scope.confirm_expand": {
        "zh-CN": "本次保存相比原范围会新增访问权限（数据源 / 表 / 字段）。确认保存？",
        "en": "Compared with the current scope, this save adds access (datasources / tables / columns). Save anyway?",
    },
    "js.scope.confirm_save_none": {
        "zh-CN": "有 {n} 个数据源保存为「无表权限」，其下所有查询都会被拒绝。确认保存？",
        "en": (
            '{n} datasources will be saved with "no table access"; '
            "all queries on them will be denied. Save anyway?"
        ),
    },
}
