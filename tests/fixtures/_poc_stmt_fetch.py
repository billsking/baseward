"""Probe: does mysql-mimic lose rows on the binary (prepared-statement) protocol?

Runs both COM_STMT_EXECUTE modes against a session that produces a known number
of rows:
  inline  (flags=CURSOR_TYPE_NO_CURSOR)  -> rows stream back immediately
  cursor  (flags=CURSOR_TYPE_READ_ONLY)  -> rows pulled via COM_STMT_FETCH batches

pymysql is used only for handshake + auth; the COM_STMT_* packets are hand-rolled.
"""
import asyncio
import struct
import os
import sys
import threading

import pymysql
from mysql_mimic import MysqlServer, ResultSet, ResultColumn, Session
from mysql_mimic.auth import IdentityProvider, NativePasswordAuthPlugin, User
from mysql_mimic.types import ColumnType

TOTAL = int(os.environ.get("PROBE_TOTAL", "1000"))
FETCH_SIZE = int(os.environ.get("PROBE_FETCH", "100"))
PORT = 13311
USERNAME = "exp_probe"
PASSWORD = "probe-password"

COM_STMT_PREPARE = 0x16
COM_STMT_EXECUTE = 0x17
COM_STMT_FETCH = 0x1C

CURSOR_TYPE_NO_CURSOR = 0x00
CURSOR_TYPE_READ_ONLY = 0x01
SERVER_STATUS_CURSOR_EXISTS = 0x0040
SERVER_STATUS_LAST_ROW_SENT = 0x0080

produced = 0


class PocIdentity(IdentityProvider):
    def get_plugins(self):
        return [NativePasswordAuthPlugin()]

    async def get_user(self, username):
        if username != USERNAME:
            return None
        return User(
            name=username,
            auth_string=NativePasswordAuthPlugin.create_auth_string(PASSWORD),
            auth_plugin=NativePasswordAuthPlugin.name,
        )


class ProbeSession(Session):
    async def query(self, expression, sql, attrs):
        global produced
        produced = 0

        async def rows():
            global produced
            for i in range(TOTAL):
                produced += 1
                yield (i, f"r{i}")

        return ResultSet(
            rows=rows(),
            columns=[
                ResultColumn("id", ColumnType.LONGLONG),
                ResultColumn("val", ColumnType.VAR_STRING),
            ],
        )


def parse_status(d):
    """EOF packet (0xFE,len5) carries status at 3:5; OK packet (0x00) at 5:7."""
    if d[0] == 0xFE and len(d) == 5:
        return struct.unpack("<H", d[3:5])[0]
    if d[0] == 0x00 and len(d) >= 7:
        return struct.unpack("<H", d[5:7])[0]
    raise SystemExit(f"unrecognised terminator first={d[0]:#x} len={len(d)} {d!r}")


def is_row(d):
    """Binary result row: 0x00 header + null bitmap + LONGLONG(8) + lenenc str."""
    return d[0] == 0x00 and len(d) >= 12


def prepare(conn):
    conn._next_seq_id = 0
    conn.write_packet(bytes([COM_STMT_PREPARE]) + b"SELECT id, val FROM t")
    d = conn._read_packet().get_all_data()
    if d[0] != 0x00:
        raise SystemExit(f"prepare failed: {d[:80]!r}")
    stmt_id = struct.unpack("<I", d[1:5])[0]
    num_params = struct.unpack("<H", d[7:9])[0]
    if num_params != 0:
        raise SystemExit("probe assumes a statement with no parameters")
    return stmt_id


def execute(conn, stmt_id, cursor_flag):
    """Send COM_STMT_EXECUTE; drain column defs; return the trailing status flags."""
    conn._next_seq_id = 0
    conn.write_packet(struct.pack("<BIBI", COM_STMT_EXECUTE, stmt_id, cursor_flag, 1))
    d = conn._read_packet().get_all_data()
    if d[0] == 0xFF:
        raise SystemExit(f"execute error packet: {d[:120]!r}")
    for _ in range(d[0]):  # lenenc column count, then that many column defs
        conn._read_packet()
    return parse_status(conn._read_packet().get_all_data())


def run_inline(conn, stmt_id):
    execute(conn, stmt_id, CURSOR_TYPE_NO_CURSOR)
    got = 0
    while True:
        d = conn._read_packet().get_all_data()
        if d[0] == 0xFF:
            raise SystemExit(f"error packet: {d[:120]!r}")
        if is_row(d):
            got += 1
            continue
        parse_status(d)
        return got, 1


def run_cursor(conn, stmt_id):
    status = execute(conn, stmt_id, CURSOR_TYPE_READ_ONLY)
    if not status & SERVER_STATUS_CURSOR_EXISTS:
        raise SystemExit("server ignored CURSOR_TYPE_READ_ONLY")
    got_total = 0
    rounds = 0
    last_row_sent = False
    while not last_row_sent:
        rounds += 1
        conn._next_seq_id = 0
        conn.write_packet(struct.pack("<BII", COM_STMT_FETCH, stmt_id, FETCH_SIZE))
        got = 0
        while True:
            d = conn._read_packet().get_all_data()
            if d[0] == 0xFF:
                raise SystemExit(f"error packet: {d[:120]!r}")
            if is_row(d):
                got += 1
                continue
            st = parse_status(d)
            got_total += got
            print(f"    round {rounds}: asked {FETCH_SIZE} got {got} status=0x{st:04x}")
            last_row_sent = bool(st & SERVER_STATUS_LAST_ROW_SENT)
            break
    return got_total, rounds


def apply_fetch_fix():
    """Reimplement Connection.handle_stmt_fetch without the off-by-one row drop.

    Upstream 3.0.5 pulls the next packet *before* checking the quota, so the
    packet that trips `count >= num_rows` is discarded from the shared async
    generator and never reaches the client.
    """
    from mysql_mimic import packets, types
    from mysql_mimic.connection import Connection
    from mysql_mimic.utils import cooperative_iterate

    async def handle_stmt_fetch(self, data: bytes) -> None:
        com = packets.parse_handle_stmt_fetch(data)
        stmt = self.get_stmt(com.stmt_id)
        assert stmt.cursor is not None
        agen = cooperative_iterate(stmt.cursor).__aiter__()
        count = 0
        exhausted = False
        while count < com.num_rows:
            try:
                packet = await agen.__anext__()
            except StopAsyncIteration:
                exhausted = True
                break
            await self.stream.write(packet, drain=False)
            count += 1
        await self.stream.drain()
        await self.stream.write(
            self.ok_or_eof(
                flags=(
                    types.ServerStatus.SERVER_STATUS_LAST_ROW_SENT
                    if exhausted
                    else types.ServerStatus.SERVER_STATUS_CURSOR_EXISTS
                )
            )
        )

    Connection.handle_stmt_fetch = handle_stmt_fetch
    print("  [patch applied: Connection.handle_stmt_fetch]")


def main():
    mode = next((a for a in sys.argv[1:] if not a.startswith("--")), "both")
    modes = ["inline", "cursor"] if mode == "both" else [mode]

    if "--fixed" in sys.argv:
        apply_fetch_fix()

    srv_loop = asyncio.new_event_loop()

    async def _start():
        s = MysqlServer(session_factory=ProbeSession, identity_provider=PocIdentity())
        await s.start_server(host="127.0.0.1", port=PORT)
        return s

    srv_loop.run_until_complete(_start())
    threading.Thread(target=srv_loop.run_forever, daemon=True).start()

    results = []
    for m in modes:
        conn = pymysql.connect(
            host="127.0.0.1",
            port=PORT,
            user=USERNAME,
            password=PASSWORD,
            database="d",
        )
        stmt_id = prepare(conn)
        print(f"  mode={m} stmt_id={stmt_id}")
        runner = run_cursor if m == "cursor" else run_inline
        got, rounds = runner(conn, stmt_id)
        conn.close()
        results.append((m, got, rounds))

    print()
    for m, got, rounds in results:
        print(
            f"  {m:6s} produced={TOTAL} received={got} rounds={rounds} "
            f"LOST={TOTAL - got}"
        )

    srv_loop.call_soon_threadsafe(srv_loop.stop)


if __name__ == "__main__":
    main()
