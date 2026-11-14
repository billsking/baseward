# 本机开发与测试

[English](development.md) | [简体中文](development.zh-CN.md)

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
python scripts/gen_master_key.py
copy .env.example .env
# 编辑 .env：填写主密钥与管理员密码即可；管理库默认落到 ./data/gateway.db（SQLite）
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

不配置任何数据库项时，管理库就是 `data/gateway.db`；想用 MySQL 再填 `GATEWAY_DB_*` 或 `GATEWAY_DB_URL`。

## 测试

### 单元测试（无需 MySQL）

```bash
pytest -q -m "not integration"
```

覆盖策略引擎、脱敏规则与 AST 映射、加密/会话、fail-closed 编排、管理端模板渲染等。

### 集成测试（需专用测试 MySQL）

```bash
cp .env.test.example .env.test
# 填写 TEST_DB_*（账号需 CREATE/DROP DATABASE 权限）
pytest -q -m integration
```

夹具会为每个测试创建 `dbgw_test_<随机>` 管理库和业务库，结束后删除；不会连接或修改其他库。多库用例另建第二个业务库，并把它加入数据源的可访问库清单。

### 性能

在网关与测试 MySQL 均可用后：

```bash
python tests/perf/bench_gateway.py --base-url http://127.0.0.1:8080 \
  --token gw_xxx --datasource demo --direct-host 127.0.0.1 --direct-port 3306 \
  --direct-user reader --direct-password '***' --direct-database testdb
```

脚本对 10 并发 × 200 次 `SELECT 1` 统计 P95，直连与经网关的 P95 差值应 ≤100ms；结果写入 `docs/perf-report.md`。

导出通道的 100 万行内存、导出期间的性能干扰与并发吞吐一次跑完（自动建删临时库、服务端自起 uvicorn、客户端独立进程）：

```bash
python tests/perf/bench_export.py            # 默认 100 万行、3 并发、10 并发 × 200 次
python tests/perf/bench_export.py --rows 100000 --ac12-requests 50   # 快速冒烟
```

结果写入 `docs/perf-report-export.md`。
