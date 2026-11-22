# BaseWard 项目规则

## 项目概览

BaseWard 是一个 MCP 原生的数据库网关：位于 AI Agent 与 MySQL 系数据库之间，提供 token 级数据源/库/表/列授权、基于 AST 的默认拒绝 SQL 策略、列级脱敏、fail-closed 审计，以及基于 MySQL 协议的短期导出凭证。

- 入口：`app/main.py` 的 `create_app()`（`app = create_app()` 在模块级执行）
- 依赖容器：`app/services.py` 的 `Services`，挂载在 `app.state.services`
- HTTP 端点：`POST /mcp`（Streamable HTTP，无状态）、`/admin`、`/healthz`
- 导出通道：`app/mysql/`（MySQL 协议代理，默认 `:3307`，由 `GATEWAY_MYSQL_EXPORT_ENABLED` 控制）
- 技术栈：Python 3.12+、FastAPI、官方 `mcp` / FastMCP、sqlglot、SQLAlchemy 2 async、mysql-mimic、Jinja2
- 管理库：默认 SQLite（`data/gateway.db`），也可用 MySQL / PostgreSQL

## 常用命令

```powershell
.venv\Scripts\activate
uvicorn app.main:app --host 0.0.0.0 --port 8080

pytest -q -m "not integration"      # 单元测试，无需 MySQL（CI 同款）
pytest -q -m integration            # 集成测试，需要 .env.test 指向专用测试库
pytest tests/unit -q --cov=app      # 覆盖率
ruff check .                        # lint（仅规则集 E4/E7/E9/F）
python scripts/gen_master_key.py    # 生成 GATEWAY_MASTER_KEY
```

- `pytest.ini` 设置了 `asyncio_mode = auto`，异步测试无需 `@pytest.mark.asyncio`。
- 集成测试会自动跳过：未配置 `.env.test` 时不执行；测试只会创建/删除 `dbgw_test_*` 临时库。
- OpenClaw 桥接插件在 `integrations/openclaw-baseward`，单独使用 npm：`npm install && npm test && npm run type-check`。

## 架构约束（改动时必须遵守）

- **单一读取路径**：MCP 与 MySQL 导出通道必须共用 `app/query/pipeline.py` 的策略 → 授权 → 脱敏 → 审计编排。`app/mcp/pipeline.py` 与 `app/mysql/` 只负责参数校验、结果投递和错误映射，不要在通道层复制编排逻辑。
- **默认拒绝**：任何 SQL 判定都基于 sqlglot AST（`app/policy/engine.py`），禁止用关键字正则做安全判断。新增语句类型必须显式加入允许清单。
- **授权分层**：`app/policy/scope.py` 负责表/列授权与 `SELECT *` 改写；`app/tokens/scope.py` 是四级 scope 模型；`app/datasources/databases.py` 是"可用库 + 每库读写策略"的唯一入口。
- **fail-closed 审计**：审计不可用时必须阻断查询。审计写入在 `app/query/pipeline.py`（`start` / `conclude_*`），通道层不得重复写审计；`GatewayError.audited` 为真时不得二次落库。
- **脱敏在网关侧完成**：`app/masking/` 在结果离开网关前生效；错误信息不得泄露数据值，数据库异常走 `app/sanitize.py` 的 `map_db_error` 转成通用错误码。
- **导出通道惰性导入**：`app.mysql.*` 仅在 `GATEWAY_MYSQL_EXPORT_ENABLED` 为真时导入（见 `Services.export_context/limiter` 与 `main.py` 的 lifespan），不要把它变成顶层依赖。

## 代码约定

- 注释与 docstring 用英文；面向用户的文案、日志与错误消息用中文（如 "数据源已停用：..."）。
- 所有对外错误文案必须能在 `app/i18n/` 中翻译成英文：服务错误放 `app/i18n/errors.py` 的 `ERROR_MESSAGES`，界面文案放 `app/i18n/messages.py` 的 `MESSAGES`（键按 `zh-CN` / `en` 双语提供）。新增中文提示务必同步补英文，否则英文界面会回退成中文。
- 异常统一继承 `app/errors.py` 的 `GatewayError`；通道层再映射为各自的错误结构（MCP 返回 `{"error": {"code", "message"}}`）。
- 配置只通过 `app/config.py` 的 `Settings`（`GATEWAY_` 前缀）读取，不要直接读 `os.environ`；新增配置项需同步 `.env.example` 与 `docs/configuration.md` / `docs/configuration.zh-CN.md`。
- 导出通道的并发、行数、时限等配额统一取自 `Settings`，并用 `app/mysql/control.py` 的限流器，不要另造开关。
- 代码风格：4 空格缩进、`from __future__ import annotations`；ruff 仅启用 E4/E7/E9/F（不强制行宽，仓库内实际约 100 列）；公开函数保持类型标注。
- 访问来源 IP 一律用 `app/netip.py`（含可信代理处理），不要直接读 `X-Forwarded-For`。

## 测试与数据库

- 单元测试放 `tests/unit/`，不得依赖外部 MySQL；集成测试放 `tests/integration/`，模块级写 `pytestmark = pytest.mark.integration`。
- 涉及策略、脱敏、授权、审计的改动必须补单元测试；涉及真实 SQL 执行语义的补集成测试。
- 迁移脚本放 `migrations/xxxx_name.sql`，按序号递增；启动时会重放 `migrations/*.sql`，所以脚本必须可重复执行（幂等）。
- 新的表/字段同时更新 `app/db/models.py` 与迁移脚本。
- 管理端模板与静态资源在 `app/admin/`，打包依赖 `pyproject.toml` 的 `package-data`，新增文件名后缀需同步。

## 文档与提交

- 用户文档中英双语成对存在：`docs/foo.md` 与 `docs/foo.zh-CN.md`，改一侧必须同步另一侧。`docs/perf-report*.md` 仅中文，由 `tests/perf/` 生成，不手工编辑。
- 面向用户的行为变更要写 `CHANGELOG.md`。
- 提交信息：祈使句、小写、带类型前缀（`feat:` / `fix:` / `docs:` / `test:` / `ci:` / `chore:`）；一个 PR 只做一件事。
- 安全漏洞不要开公开 issue，遵循 `SECURITY.md` 的私密上报流程。
- 不要提交 `.env`、`.env.test`、`data/` 下的运行库或任何密钥；主密钥由 `scripts/gen_master_key.py` 生成。
