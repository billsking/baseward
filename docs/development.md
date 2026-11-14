# Local development and testing

[English](development.md) | [简体中文](development.zh-CN.md)

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
python scripts/gen_master_key.py
copy .env.example .env
# edit .env: fill in the master key and the admin password; the management database defaults to ./data/gateway.db (SQLite)
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

With no database settings configured, the management database is `data/gateway.db`; to use MySQL, fill in `GATEWAY_DB_*` or `GATEWAY_DB_URL`.

## Testing

### Unit tests (no MySQL required)

```bash
pytest -q -m "not integration"
```

Covers the policy engine, masking rules and AST mapping, encryption/sessions, fail-closed orchestration, admin console template rendering, and more.

### Integration tests (a dedicated test MySQL is required)

```bash
cp .env.test.example .env.test
# fill in TEST_DB_* (the account needs CREATE/DROP DATABASE privileges)
pytest -q -m integration
```

The fixtures create a `dbgw_test_<random>` management database and upstream database for each test and drop them afterwards; they never connect to or modify other databases. Multi-database cases create a second upstream database and add it to the datasource's accessible-databases list.

### Performance

Once both the gateway and the test MySQL are available:

```bash
python tests/perf/bench_gateway.py --base-url http://127.0.0.1:8080 \
  --token gw_xxx --datasource demo --direct-host 127.0.0.1 --direct-port 3306 \
  --direct-user reader --direct-password '***' --direct-database testdb
```

The script reports P95 over 10 concurrent x 200 `SELECT 1` calls; the P95 difference between the direct connection and the gateway should be ≤100ms. Results are written to `docs/perf-report.md`.

Memory for a 1-million-row export through the export channel, performance interference during exports, and concurrent throughput are all measured in one run (temporary databases are created and dropped automatically, the server starts its own uvicorn, and the client runs in a separate process):

```bash
python tests/perf/bench_export.py            # defaults: 1 million rows, 3 concurrent, 10 concurrent x 200 requests
python tests/perf/bench_export.py --rows 100000 --ac12-requests 50   # quick smoke test
```

Results are written to `docs/perf-report-export.md`.
