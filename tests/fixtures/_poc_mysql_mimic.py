"""PoC: whether mysql-mimic can support the BaseWard streaming export channel.

Four questions:
1. Can a custom IdentityProvider look up a credential by temporary account name and complete
   mysql_native_password authentication (including rejecting a wrong password)?
2. With an async generator in ResultSet.rows, is server resident memory bounded for a 1M-row export?
3. If the client disconnects midway, is the server generator cancelled (which decides whether the
   upstream query leaks)?
4. Can session statements such as SHOW/USE/SET be answered by built-in middleware, and can
   SQLAlchemy+pymysql connect without changes?
"""

from __future__ import annotations

import asyncio
import ctypes
import time

import pymysql
from mysql_mimic import (
    ColumnType,
    IdentityProvider,
    MysqlServer,
    ResultColumn,
    ResultSet,
    Session,
    User,
)
from mysql_mimic.auth import NativePasswordAuthPlugin

HOST = "127.0.0.1"
PORT = 13306
USERNAME = "gw_exp_a1b2c3d4"
PASSWORD = "tmp_9f3kQ7xZ2mP5rT8v"
BOUND_DATABASE = "demo"
ROWS_TOTAL = 1_000_000

generator_cancelled = asyncio.Event()
generator_rows_sent = 0


def rss_mb() -> float:
    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_uint32),
            ("PageFaultCount", ctypes.c_uint32),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    pmc = ProcessMemoryCounters()
    pmc.cb = ctypes.sizeof(ProcessMemoryCounters)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    handle = kernel32.GetCurrentProcess()
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int
    psapi.GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ProcessMemoryCounters), ctypes.c_uint32
    ]
    ok = psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb)
    if not ok:
        raise RuntimeError(
            f"GetProcessMemoryInfo failed, winerror={ctypes.GetLastError()}"
        )
    if pmc.WorkingSetSize == 0:
        raise RuntimeError("GetProcessMemoryInfo returned 0 WorkingSetSize")
    return pmc.WorkingSetSize / 1024 / 1024


max_rss_during_stream = 0.0
cancel_rows_sent = 0


class PocIdentity(IdentityProvider):
    """Simulate looking up the temporary credential table by username."""

    def get_plugins(self):
        return [NativePasswordAuthPlugin()]

    async def get_user(self, username: str):
        if username != USERNAME:
            return None
        return User(
            name=username,
            auth_string=NativePasswordAuthPlugin.create_auth_string(PASSWORD),
            auth_plugin=NativePasswordAuthPlugin.name,
        )


async def big_rows():
    global generator_rows_sent, max_rss_during_stream
    try:
        for i in range(ROWS_TOTAL):
            yield (i, "payload-" + str(i))
            generator_rows_sent = i + 1
            if i % 50_000 == 0:
                max_rss_during_stream = max(max_rss_during_stream, rss_mb())
    finally:
        generator_cancelled.set()


CANCEL_TOTAL = 5_000_000
DRAIN_EVERY = 1000
cancel_stop_reason = "completed"


class PocSession(Session):
    async def throttled_rows(self):
        """Await drain() every DRAIN_EVERY rows to get backpressure and disconnect awareness."""
        global cancel_rows_sent, cancel_stop_reason
        stream = self._connection.stream if self._connection else None
        try:
            for i in range(CANCEL_TOTAL):
                yield (i, "row")
                cancel_rows_sent = i + 1
                if i and i % DRAIN_EVERY == 0 and stream is not None:
                    if stream.writer.transport.is_closing():
                        cancel_stop_reason = "transport closing"
                        return
                    await stream.drain()
            cancel_stop_reason = "completed"
        except (ConnectionResetError, BrokenPipeError, OSError) as exc:
            cancel_stop_reason = f"drain raised {type(exc).__name__}"
        except asyncio.CancelledError:
            cancel_stop_reason = "task cancelled"
            raise
        finally:
            generator_cancelled.set()

    async def use(self, database: str) -> None:
        if database != BOUND_DATABASE:
            raise PermissionError(f"database not bound to this credential: {database}")
        self.database = database

    async def query(self, expression, sql, attrs):
        text = sql.strip().upper()
        if text.startswith("SELECT * FROM BIG_EXPORT"):
            return ResultSet(
                rows=big_rows(),
                columns=[
                    ResultColumn("id", ColumnType.LONGLONG),
                    ResultColumn("payload", ColumnType.STRING),
                ],
            )
        if text.startswith("SELECT * FROM CANCELTEST"):
            return ResultSet(
                rows=self.throttled_rows(),
                columns=[
                    ResultColumn("id", ColumnType.LONGLONG),
                    ResultColumn("payload", ColumnType.STRING),
                ],
            )
        return await super().query(expression, sql, attrs)


def client_phase() -> dict:
    """Run a synchronous client in a thread, simulating a real script."""
    out = {}

    conn = pymysql.connect(
        host=HOST,
        port=PORT,
        user=USERNAME,
        password=PASSWORD,
        database=BOUND_DATABASE,
        charset="utf8mb4",
    )

    def probe(label: str, sql: str, fetch: bool = True) -> None:
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                if fetch:
                    rows = cur.fetchall()
                    out[label] = f"{len(rows)} rows" + (
                        f" first={rows[0]}" if rows else ""
                    )
                else:
                    out[label] = "ok"
        except Exception as exc:  # noqa: BLE001
            out[label] = f"ERROR {type(exc).__name__}: {exc}"[:200]

    probe("select_database", "SELECT DATABASE()")
    probe("set_names", "SET NAMES utf8mb4", fetch=False)
    probe("show_databases", "SHOW DATABASES")
    probe("show_tables", "SHOW TABLES")
    probe("version_comment", "SELECT @@version_comment LIMIT 1")
    probe("select_1", "SELECT 1")
    probe("show_variables", "SHOW VARIABLES LIKE 'max_allowed_packet'")
    probe("sql_mode", "SELECT @@session.sql_mode")
    conn.close()

    # Stream 1M rows with an SSCursor on the client (no buffering)
    conn = pymysql.connect(
        host=HOST,
        port=PORT,
        user=USERNAME,
        password=PASSWORD,
        cursorclass=pymysql.cursors.SSCursor,
    )
    started = time.perf_counter()
    count = 0
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM big_export")
        for _row in cur:
            count += 1
    out["stream_rows"] = count
    out["stream_seconds"] = round(time.perf_counter() - started, 2)
    conn.close()

    # Connect through SQLAlchemy without changes
    try:
        import sqlalchemy

        engine = sqlalchemy.create_engine(
            f"mysql+pymysql://{USERNAME}:{PASSWORD}@{HOST}:{PORT}/{BOUND_DATABASE}"
        )
        with engine.connect() as c:
            out["sqlalchemy_select_1"] = list(c.exec_driver_sql("SELECT 1"))
            out["sqlalchemy_stream"] = sum(1 for _ in c.exec_driver_sql("SELECT * FROM big_export"))
        engine.dispose()
    except Exception as exc:  # noqa: BLE001
        out["sqlalchemy_error"] = f"{type(exc).__name__}: {exc}"[:300]

    # Client disconnects early
    generator_cancelled.clear()
    conn = pymysql.connect(
        host=HOST,
        port=PORT,
        user=USERNAME,
        password=PASSWORD,
        cursorclass=pymysql.cursors.SSCursor,
    )
    # No `with`: SSCursor.close() drains the remaining rows, which would not be an early disconnect
    cur = conn.cursor()
    cur.execute("SELECT * FROM canceltest")
    for i, _row in enumerate(cur):
        if i >= 100:
            break
    try:
        conn.close()
        out["disconnect_close"] = "clean"
    except Exception as exc:  # noqa: BLE001
        out["disconnect_close"] = f"{type(exc).__name__}: {exc}"[:120]
        try:
            conn._sock.close()
        except Exception:  # noqa: BLE001
            pass

    # A wrong password must be rejected
    try:
        pymysql.connect(
            host=HOST, port=PORT, user=USERNAME, password="wrong-password"
        )
        out["wrong_password"] = "ACCEPTED (BAD)"
    except Exception as exc:  # noqa: BLE001
        out["wrong_password"] = f"rejected: {type(exc).__name__}"

    try:
        pymysql.connect(host=HOST, port=PORT, user="unknown_user", password="x")
        out["unknown_user"] = "ACCEPTED (BAD)"
    except Exception as exc:  # noqa: BLE001
        out["unknown_user"] = f"rejected: {type(exc).__name__}"

    return out


async def main() -> None:
    server = MysqlServer(
        session_factory=PocSession, identity_provider=PocIdentity()
    )
    await server.start_server(host=HOST, port=PORT)
    print(f"rss before: {rss_mb():.1f} MB")

    baseline = rss_mb()
    result = await asyncio.to_thread(client_phase)
    peak = rss_mb()

    await asyncio.sleep(2.0)
    cancelled = generator_cancelled.is_set()
    sent_at_cancel = cancel_rows_sent

    print("---- client results ----")
    for key, value in result.items():
        print(f"  {key}: {value}")
    print("---- server side ----")
    print(f"  rss baseline: {baseline:.1f} MB")
    print(f"  rss peak sampled inside row generator: {max_rss_during_stream:.1f} MB")
    print(f"  rss after 1M-row stream: {peak:.1f} MB")
    print(f"  rss delta: {peak - baseline:+.1f} MB")
    print(f"  generator cancelled after client disconnect: {cancelled}")
    print(f"  server rows sent before disconnect: {sent_at_cancel} / {5_000_000}")
    print(f"  stop reason: {cancel_stop_reason}")

    server.close()
    await server.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
