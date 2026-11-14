"""AC17 / AC12 export-channel benchmark (client runs in a separate process so its memory is not counted in server RSS).

Server mode (default):

    python tests/perf/bench_export.py

Client mode (spawned as a subprocess by server mode; can also be run manually):

    BENCH_EXPORT_PASSWORD=... python tests/perf/bench_export.py --mode client \
        --port 3307 --user exp_xxx --database db --sql "SELECT ..."

The script reads `.env.test` (TEST_DB_*), creates temporary management and business databases, and
drops them afterwards. Output goes to docs/perf-report-export.md; no password or password-bearing
connection string is ever printed.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import socket
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings
from app.mysql.streaming import YIELD_EVERY
from tests.integration.conftest import (
    _create_database,
    _drop_databases,
    _parse_env_file,
    _url,
)
from tests.meminfo import rss_bytes
from tests.perf import bench_gateway

MB = 1024 * 1024
MEMORY_LIMIT = 64 * MB
MEMORY_DRIFT_LIMIT = 32 * MB
AC12_THRESHOLD_MS = 100.0

WIDE_DDL = """
CREATE TABLE {table} (
  id INT NOT NULL PRIMARY KEY,
  label VARCHAR(64) NULL,
  amount DECIMAL(12,2) NULL,
  payload TEXT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

SEED_WIDE = (
    "INSERT INTO {table} (id, label, amount, payload) "
    "SELECT seq, CONCAT('row-', seq), seq / 100, REPEAT('x', 128) "
    "FROM (SELECT seq FROM {helper} ORDER BY seq LIMIT {rows}) s"
)


def _load_test_config() -> dict[str, Any]:
    path = PROJECT_ROOT / ".env.test"
    if not path.exists():
        raise SystemExit("缺少 .env.test（参考 .env.test.example）")
    values = _parse_env_file(path)
    missing = [
        key for key in ("TEST_DB_HOST", "TEST_DB_USER", "TEST_DB_PASSWORD") if not values.get(key)
    ]
    if missing:
        raise SystemExit(f".env.test 缺少必填项：{', '.join(missing)}")
    return {
        "host": values["TEST_DB_HOST"],
        "port": int(values.get("TEST_DB_PORT", "3306")),
        "user": values["TEST_DB_USER"],
        "password": values["TEST_DB_PASSWORD"],
    }


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])




# ---------- Client process ----------


def run_client(args: argparse.Namespace) -> int:
    import pymysql
    from pymysql.cursors import SSCursor

    password = os.environ.get("BENCH_EXPORT_PASSWORD")
    if not password:
        print(json.dumps({"event": "error", "error": "缺少 BENCH_EXPORT_PASSWORD"}))
        return 2
    connection = pymysql.connect(
        host="127.0.0.1",
        port=args.port,
        user=args.user,
        password=password,
        database=args.database,
        connect_timeout=10,
        read_timeout=600,
        write_timeout=600,
        autocommit=True,
        cursorclass=SSCursor,
    )
    started = time.perf_counter()
    rows = 0
    next_sample = args.sample_every
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(args.sql)
            while True:
                batch = cursor.fetchmany(1000)
                if not batch:
                    break
                rows += len(batch)
                while rows >= next_sample:
                    print(
                        json.dumps(
                            {
                                "event": "progress",
                                "rows": rows,
                                "elapsed": time.perf_counter() - started,
                            }
                        ),
                        flush=True,
                    )
                    next_sample += args.sample_every
        finally:
            cursor.close()
    finally:
        connection.close()
    print(
        json.dumps(
            {
                "event": "done",
                "rows": rows,
                "seconds": time.perf_counter() - started,
            }
        ),
        flush=True,
    )
    return 0


# ---------- Server process ----------


async def _seed_table(engine, table: str, rows: int) -> None:
    # Engine-neutral series: MariaDB's seq_1_to_N pseudo-table does not exist on MySQL or the
    # distributed engines, so double a helper table with plain INSERT ... SELECT instead.
    helper = f"{table}_seed"
    async with engine.begin() as conn:
        await conn.exec_driver_sql(WIDE_DDL.format(table=table))
        await conn.exec_driver_sql(f"CREATE TABLE {helper} (seq INT NOT NULL PRIMARY KEY)")
        await conn.exec_driver_sql(
            f"INSERT INTO {helper} (seq) VALUES (1),(2),(3),(4),(5),(6),(7),(8),(9),(10)"
        )
        count = 10
        while count < rows:
            step = min(count, rows - count)
            await conn.exec_driver_sql(
                f"INSERT INTO {helper} (seq) "
                f"SELECT seq + {count} FROM {helper} ORDER BY seq LIMIT {step}"
            )
            count += step
        await conn.exec_driver_sql(SEED_WIDE.format(table=table, helper=helper, rows=rows))
        await conn.exec_driver_sql(f"DROP TABLE {helper}")


async def _wait_http(server, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not server.started:
        if time.monotonic() >= deadline:
            raise RuntimeError("HTTP 服务未在超时时间内启动")
        await asyncio.sleep(0.05)


async def _wait_audit(engine, expected_rows: int, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT status, row_count FROM audit_log "
                        "WHERE event_type='mysql_export' ORDER BY id DESC LIMIT 1"
                    )
                )
            ).first()
        if row is not None:
            entry = {"status": row[0], "row_count": int(row[1] or 0)}
            if entry["status"] in {"success", "error", "interrupted"}:
                if entry["status"] != "success" or entry["row_count"] == expected_rows:
                    return entry
        if time.monotonic() >= deadline:
            raise RuntimeError(f"审计未达到预期：{row}")
        await asyncio.sleep(0.1)


def _spawn_client(args: argparse.Namespace, password: str, sql: str, sample_every: int):
    env = dict(os.environ)
    env["BENCH_EXPORT_PASSWORD"] = password
    env.setdefault("PYTHONIOENCODING", "utf-8")
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--mode",
        "client",
        "--port",
        str(args.export_port),
        "--user",
        args.account,
        "--database",
        args.database,
        "--sql",
        sql,
        "--sample-every",
        str(sample_every),
    ]
    return asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )


async def _collect_client(
    process,
    samples: list[tuple[int, int]],
    on_progress=None,
) -> dict:
    assert process.stdout is not None
    result: dict[str, Any] = {}
    while True:
        line = await process.stdout.readline()
        if not line:
            break
        payload = json.loads(line.decode("utf-8").strip())
        if payload["event"] == "progress":
            samples.append((int(payload["rows"]), rss_bytes()))
            if on_progress is not None:
                on_progress(payload)
        elif payload["event"] == "done":
            result = payload
    stderr = b""
    if process.stderr is not None:
        stderr = await process.stderr.read()
    code = await process.wait()
    if code != 0:
        raise RuntimeError(f"客户端进程失败 code={code}：{stderr.decode('utf-8', 'replace')[-400:]}")
    return result


async def _loop_lag_probe(stop: asyncio.Event, interval: float = 0.02) -> list[float]:
    """Sample event-loop stalls: exports share the loop with MCP, so a stall is the lower bound of MCP queueing time."""
    lags: list[float] = []
    while not stop.is_set():
        started = time.monotonic()
        await asyncio.sleep(interval)
        lags.append(max(0.0, time.monotonic() - started - interval))
    return lags


def _lag_summary(lags: list[float]) -> dict:
    if not lags:
        return {"samples": 0, "max": None, "p95": None}
    ordered = sorted(lags)
    return {
        "samples": len(ordered),
        "max": round(ordered[-1] * 1000, 2),
        "p95": round(ordered[int(len(ordered) * 0.95) - 1] * 1000, 2),
    }


async def _run_ac12(
    http_port: int, env: dict, label: str, requests: int = 200, concurrency: int = 10
) -> dict:
    """The AC12 client must be a separate process: in-process it would compete with the gateway for the same event loop."""
    json_path = _temp_path(f"dbgw-ac12-{label}", ".json")
    report_path = _temp_path(f"dbgw-ac12-{label}", ".md")
    child_env = dict(os.environ)
    child_env["BENCH_TOKEN"] = env["token"]
    child_env["BENCH_DIRECT_PASSWORD"] = env["db_password"]
    command = [
        sys.executable,
        str(Path(bench_gateway.__file__).resolve()),
        "--base-url",
        f"http://127.0.0.1:{http_port}",
        "--datasource",
        env["datasource"],
        "--direct-host",
        env["db_host"],
        "--direct-port",
        str(env["db_port"]),
        "--direct-user",
        env["db_user"],
        "--direct-database",
        env["biz_db"],
        "--requests",
        str(requests),
        "--concurrency",
        str(concurrency),
        "--output",
        str(report_path),
        "--json",
        str(json_path),
    ]
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=child_env,
    )
    try:
        _, stderr = await process.communicate()
        # bench_gateway signals its verdict through the exit code; only a missing JSON counts as an execution failure.
        if not json_path.exists():
            tail = stderr.decode("utf-8", "replace")[-400:]
            raise RuntimeError(f"AC12 客户端失败 code={process.returncode}：{tail}")
        return json.loads(json_path.read_text(encoding="utf-8"))
    finally:
        for path in (json_path, report_path):
            path.unlink(missing_ok=True)


def _temp_path(stem: str, suffix: str) -> Path:
    return Path(tempfile.gettempdir()) / f"{stem}-{os.getpid()}{suffix}"


async def run_bench(args: argparse.Namespace) -> dict:
    import uvicorn

    from app.main import create_app

    config = _load_test_config()
    suffix = os.urandom(4).hex()
    mgmt_db = f"dbgw_bench_{suffix}"
    biz_db = f"dbgw_bench_biz_{suffix}"
    admin_engine = create_async_engine(_url(config), connect_args={"connect_timeout": 5})
    biz_engine = None
    http_port = _free_port()
    args.export_port = _free_port()

    created: list[str] = []
    report: dict[str, Any] = {
        "rows": args.rows,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    server = None
    server_task = None
    try:
        async with admin_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        await _create_database(admin_engine, mgmt_db)
        created.append(mgmt_db)
        await _create_database(admin_engine, biz_db)
        created.append(biz_db)

        biz_engine = create_async_engine(_url(config, biz_db), connect_args={"connect_timeout": 5})
        seed_started = time.monotonic()
        await _seed_table(biz_engine, "bench_wide", args.rows)
        await _seed_table(biz_engine, "bench_conc", args.conc_rows)
        report["seed_seconds"] = round(time.monotonic() - seed_started, 2)

        settings = Settings(
            _env_file=None,
            master_key=base64.b64encode(b"b" * 32).decode("ascii"),
            admin_user="bench-admin",
            admin_password="bench-admin-password",
            db_host=config["host"],
            db_port=int(config.get("port", 3306)),
            db_user=config["user"],
            db_password=config["password"],
            db_name=mgmt_db,
            port=http_port,
            log_level="WARNING",
            mysql_export_enabled=True,
            mysql_export_host="127.0.0.1",
            mysql_export_port=args.export_port,
            mysql_export_allow_cidrs="127.0.0.1/32",
            mysql_export_max_concurrent=args.concurrent,
            mysql_export_max_rows=args.rows + args.conc_rows + 1000,
            mysql_export_max_seconds=600,
            mysql_export_fetch_batch=args.fetch_batch,
        )
        app = create_app(settings)
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=http_port, log_level="warning")
        )
        server.install_signal_handlers = lambda: None
        server_task = asyncio.create_task(server.serve())
        await _wait_http(server)

        services = app.state.services
        datasource = await services.datasources.create(
            name="benchds",
            host=config["host"],
            port=int(config.get("port", 3306)),
            username=config["user"],
            password=config["password"],
            default_database=biz_db,
            remark="压测数据源",
        )
        token, token_row = await services.tokens.create("bench-token")
        account, password = await services.credentials.issue(
            token_id=token_row.id,
            datasource_id=datasource.id,
            database=biz_db,
            ttl_hours=8,
            admin_username="bench-admin",
            client_ip="127.0.0.1",
        )

        env = {
            "token": token,
            "datasource": "benchds",
            "db_host": config["host"],
            "db_port": int(config.get("port", 3306)),
            "db_user": config["user"],
            "db_password": config["password"],
            "biz_db": biz_db,
        }
        args.database = biz_db
        args.account = account

        # Warm-up: establish the export pool and protocol modules.
        await asyncio.to_thread(_warmup, args.export_port, account, password, biz_db)
        # Same-machine no-export baseline: separates "host + remote database" environment cost from export interference.
        report["ac12_baseline"] = await _run_ac12(
            http_port, env, "baseline", args.ac12_requests, args.ac12_concurrency
        )
        # Take the baseline after AC12: MCP connections and buffers are already in place, which is the real idle RSS.
        baseline = rss_bytes()
        report["baseline_bytes"] = baseline

        select_sql = "SELECT id, label, amount, payload FROM bench_wide ORDER BY id"
        samples: list[tuple[int, int]] = []
        ac12_task: asyncio.Task | None = None
        stop_probe = asyncio.Event()
        probe = asyncio.create_task(_loop_lag_probe(stop_probe))
        process = await _spawn_client(args, password, select_sql, args.sample_every)

        def _on_progress(_payload: dict) -> None:
            nonlocal ac12_task
            if ac12_task is None:
                ac12_task = asyncio.create_task(
                    _run_ac12(
                        http_port, env, "export", args.ac12_requests, args.ac12_concurrency
                    )
                )

        result = await _collect_client(process, samples, _on_progress)
        stop_probe.set()
        report["loop_lag"] = _lag_summary(await probe)
        if ac12_task is not None:
            report["ac12"] = await ac12_task
        report["client_seconds"] = round(float(result.get("seconds", 0.0)), 2)
        report["client_rows"] = int(result.get("rows", 0))
        report["samples"] = samples
        report["peak_bytes"] = max((sample for _, sample in samples), default=baseline)
        report["end_bytes"] = rss_bytes()
        audit_engine = create_async_engine(
            _url(config, mgmt_db), connect_args={"connect_timeout": 5}
        )
        try:
            report["audit"] = await _wait_audit(audit_engine, args.rows)
        finally:
            await audit_engine.dispose()

        # Concurrent throughput: max_concurrent exports run together and all should complete normally.
        conc_sql = "SELECT id, label, amount, payload FROM bench_conc ORDER BY id"
        wall_started = time.monotonic()
        processes = [
            await _spawn_client(args, password, conc_sql, args.conc_rows + 1)
            for _ in range(args.concurrent)
        ]
        conc_results = await asyncio.gather(
            *(_collect_client(process, []) for process in processes)
        )
        report["concurrency"] = {
            "workers": args.concurrent,
            "wall_seconds": round(time.monotonic() - wall_started, 2),
            "durations": [round(float(item.get("seconds", 0.0)), 2) for item in conc_results],
            "rows": [int(item.get("rows", 0)) for item in conc_results],
        }

        async with admin_engine.connect() as conn:
            version = (await conn.execute(text("SELECT VERSION()"))).scalar()
        report["mysql_version"] = str(version)
        report["python"] = sys.version.split()[0]
        report["platform"] = sys.platform
        report["settings"] = {
            "fetch_batch": settings.mysql_export_fetch_batch,
            "max_rows": settings.mysql_export_max_rows,
            "max_concurrent": settings.mysql_export_max_concurrent,
            "sample_every": args.sample_every,
            "ac12_requests": args.ac12_requests,
            "ac12_concurrency": args.ac12_concurrency,
        }
        return report
    finally:
        if server is not None:
            server.should_exit = True
        if server_task is not None:
            try:
                await asyncio.wait_for(server_task, timeout=20)
            except Exception:  # noqa: BLE001 - close failures must not mask the benchmark result
                server_task.cancel()
        if biz_engine is not None:
            await biz_engine.dispose()
        await _drop_databases(admin_engine, list(reversed(created)))
        await admin_engine.dispose()


def _warmup(port: int, account: str, password: str, database: str) -> None:
    import pymysql

    connection = pymysql.connect(
        host="127.0.0.1",
        port=port,
        user=account,
        password=password,
        database=database,
        connect_timeout=10,
        autocommit=True,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT id FROM bench_conc ORDER BY id LIMIT 1")
            cursor.fetchall()
    finally:
        connection.close()


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def _memory_verdict(report: dict) -> dict:
    baseline = report["baseline_bytes"]
    peak = report["peak_bytes"]
    samples = report["samples"]
    peak_delta = peak - baseline
    drift = 0
    if samples:
        drift = (samples[-1][1] - samples[0][1])
    passed = peak_delta <= MEMORY_LIMIT and drift <= MEMORY_DRIFT_LIMIT
    return {
        "peak_delta": peak_delta,
        "drift": drift,
        "passed": passed,
    }


def write_report(report: dict, path: Path) -> bool:
    memory = _memory_verdict(report)
    ac17_ok = (
        memory["passed"]
        and report["client_rows"] == report["rows"]
        and report["audit"]["status"] == "success"
        and report["audit"]["row_count"] == report["rows"]
    )
    ac12 = report.get("ac12")
    ac12_ok = (
        ac12 is not None
        and ac12["overhead"] is not None
        and ac12["overhead"] <= AC12_THRESHOLD_MS
        and not ac12["errors"]
    )
    concurrency = report["concurrency"]
    conc_ok = all(rows == report["rows_to_conc"] for rows in concurrency["rows"])

    lines = [
        "# 导出通道性能与内存报告（AC17 / AC12 / 并发）",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 环境：{report['platform']} / Python {report['python']} / MySQL {report['mysql_version']}",
        f"- 配置：fetch_batch={report['settings']['fetch_batch']}，"
        f"max_rows={report['settings']['max_rows']}，"
        f"max_concurrent={report['settings']['max_concurrent']}",
        f"- 客户端位于独立进程；每 {report['settings']['sample_every']} 行采样一次服务端 RSS。",
        f"- AC12 参数：{report['settings']['ac12_concurrency']} 并发共 "
        f"{report['settings']['ac12_requests']} 次 `SELECT 1`，客户端独立进程。",
        "",
        "## AC17 内存与审计",
        "",
        "| 指标 | 数值 |",
        "| --- | ---: |",
        f"| 数据行数 | {report['rows']} |",
        f"| 空载基线 RSS | {report['baseline_bytes'] / MB:.1f} MB |",
        f"| 峰值 RSS | {report['peak_bytes'] / MB:.1f} MB |",
        f"| 结束后 RSS | {report['end_bytes'] / MB:.1f} MB |",
        f"| 峰值增量 | {memory['peak_delta'] / MB:.1f} MB |",
        f"| 首末采样漂移 | {memory['drift'] / MB:.1f} MB |",
        f"| 判定阈值 | 峰值增量 ≤ {MEMORY_LIMIT / MB:.0f} MB 且漂移 ≤ {MEMORY_DRIFT_LIMIT / MB:.0f} MB |",
        f"| 客户端实收行数 | {report['client_rows']} |",
        f"| 审计状态 / 行数 | {report['audit']['status']} / {report['audit']['row_count']} |",
        f"| 导出耗时 | {report['client_seconds']} s |",
        f"| 结论 | {'通过' if ac17_ok else '不通过'} |",
        "",
        "### 采样明细",
        "",
        "| 已发送行数 | 服务端 RSS (MB) | 增量 (MB) |",
        "| ---: | ---: | ---: |",
    ]
    for rows, rss in report["samples"]:
        lines.append(f"| {rows} | {rss / MB:.1f} | {(rss - report['baseline_bytes']) / MB:.1f} |")

    lines.extend(["", "## AC12 复测（导出进行期间）", ""])
    if ac12 is None:
        lines.append("- 未采集到 AC12 样本。")
    else:
        lines.extend(
            [
                "| 指标 | 经网关 (ms) | 直连 (ms) |",
                "| --- | ---: | ---: |",
            ]
        )
        for key, label in (
            ("avg", "平均值"),
            ("p50", "P50"),
            ("p95", "P95"),
            ("p99", "P99"),
            ("max", "最大值"),
        ):
            lines.append(
                f"| {label} | {_fmt(ac12['gateway'][key])} | {_fmt(ac12['direct'][key])} |"
            )
        lines.extend(
            [
                "",
                f"- P95 额外开销：{_fmt(ac12['overhead'])} ms（阈值 {AC12_THRESHOLD_MS:.0f} ms）",
                f"- 有效样本：{ac12['samples']} 条；错误：{len(ac12['errors'])} 条",
                f"- 结论：{'通过' if ac12_ok else '不通过'}",
            ]
        )
    baseline12 = report.get("ac12_baseline")
    if baseline12 is not None:
        lag = report.get("loop_lag") or {}
        lines.extend(
            [
                "",
                "### 同机对照（无导出时的 AC12）",
                "",
                f"- 网关 P95 {_fmt(baseline12['gateway']['p95'])} ms，"
                f"直连 P95 {_fmt(baseline12['direct']['p95'])} ms，"
                f"额外开销 {_fmt(baseline12['overhead'])} ms"
                f"（这是宿主机 + 远端库的环境成本，不含导出干扰）",
                f"- 导出期间事件循环停顿：P95 {_fmt(lag.get('p95'))} ms，"
                f"最大 {_fmt(lag.get('max'))} ms（{lag.get('samples', 0)} 次采样，间隔 20 ms）",
                f"- 导出相对无导出基线的 P95 增量："
                f"{_fmt(None if ac12 is None or baseline12 is None or ac12['overhead'] is None or baseline12['overhead'] is None else ac12['overhead'] - baseline12['overhead'])} ms",
            ]
        )

    lines.extend(
        [
            "",
            "## 并发吞吐",
            "",
            "| 场景 | 墙钟 (s) | 各导出耗时 (s) | 各导出行数 |",
            "| --- | ---: | --- | --- |",
            f"| {concurrency['workers']} 个导出并发 | {concurrency['wall_seconds']} | "
            f"{' / '.join(str(item) for item in concurrency['durations'])} | "
            f"{' / '.join(str(item) for item in concurrency['rows'])} |",
            "",
            f"- 结论：{'通过' if conc_ok else '不通过'}（每个导出都应读完 {report['rows_to_conc']} 行）",
            "",
            "## 说明",
            "",
            "- 客户端在独立进程中消费结果，服务端 RSS 不含客户端缓冲。",
            "- AC12 客户端同样是独立进程；与网关同进程时两边抢同一个事件循环，测出来的是"
            "客户端自身的排队时间。",
            "- AC12 复测在首个采样点后启动，与导出同时进行；同机对照的两次测量参数一致。",
            "- 判定只看内存增量与 P95 差值，绝对值随机器与数据库负载变化；"
            "AC12 的绝对阈值在宿主机自身就超过阈值时无法达成，此时看「导出相对基线的增量」。",
            f"- 导出期间每 {YIELD_EVERY} 行让出一次事件循环"
            "（`app/mysql/streaming.py` 的 `YIELD_EVERY`），"
            "否则一批 1 万行的编码会把 MCP 请求按住整批。",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return ac17_ok and ac12_ok and conc_ok


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="导出通道 AC17/AC12 压测")
    parser.add_argument("--mode", choices=("run", "client"), default="run")
    parser.add_argument("--rows", type=int, default=1_000_000, help="AC17 导出行数")
    parser.add_argument(
        "--sample-every", type=int, default=50_000, help="每多少行采样一次服务端 RSS"
    )
    parser.add_argument("--conc-rows", type=int, default=100_000, help="并发场景表行数")
    parser.add_argument("--concurrent", type=int, default=3, help="并发导出数量")
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "docs/perf-report-export.md",
        help="报告输出路径",
    )
    parser.add_argument(
        "--fetch-batch", type=int, default=10_000, help="上游 fetchmany 行数（默认同 spec）"
    )
    parser.add_argument("--ac12-requests", type=int, default=200, help="AC12 总请求数")
    parser.add_argument("--ac12-concurrency", type=int, default=10, help="AC12 并发数")
    parser.add_argument("--port", type=int, default=0, help="client 模式：导出端口")
    parser.add_argument("--user", default="", help="client 模式：凭证账号")
    parser.add_argument("--database", default="", help="client 模式：默认库")
    parser.add_argument("--sql", default="", help="client 模式：查询语句")
    args = parser.parse_args(argv)
    if args.mode == "run":
        if args.rows <= 0 or args.conc_rows <= 0 or args.concurrent < 1:
            parser.error("--rows / --conc-rows / --concurrent 必须为正")
        if args.sample_every <= 0:
            parser.error("--sample-every 必须为正")
        if args.ac12_requests <= 0 or args.ac12_concurrency <= 0:
            parser.error("--ac12-requests / --ac12-concurrency 必须为正")
    else:
        if not args.port or not args.user or not args.sql:
            parser.error("client 模式需要 --port / --user / --sql")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.mode == "client":
        return run_client(args)
    report = asyncio.run(run_bench(args))
    report["rows_to_conc"] = args.conc_rows
    passed = write_report(report, args.output)
    memory = _memory_verdict(report)
    print(
        f"行数={report['client_rows']}，峰值增量={memory['peak_delta'] / MB:.1f} MB，"
        f"审计行数={report['audit']['row_count']}，"
        f"AC12 开销={_fmt(report.get('ac12', {}).get('overhead'))} ms"
        f"（无导出基线 {_fmt((report.get('ac12_baseline') or {}).get('overhead'))} ms），"
        f"报告={args.output}，结论={'通过' if passed else '不通过'}"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
