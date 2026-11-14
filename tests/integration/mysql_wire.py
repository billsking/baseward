"""Minimal MySQL protocol client: text and binary protocols (including COM_STMT_FETCH).

pymysql does not support server-side cursors, so AC25 can only be verified at the packet level. This
implements only the subset needed: handshake (mysql_native_password), COM_QUERY,
COM_STMT_PREPARE / EXECUTE / FETCH. CLIENT_DEPRECATE_EOF is not negotiated, so terminators are
classic 0xFE EOF packets, which makes row packets easy to tell apart.
"""

from __future__ import annotations

import hashlib
import socket
import struct

CLIENT_PROTOCOL_41 = 0x00000200
CLIENT_SECURE_CONNECTION = 0x00008000
CLIENT_PLUGIN_AUTH = 0x00080000
CLIENT_PLUGIN_AUTH_LENENC_CLIENT_DATA = 0x00200000

SERVER_STATUS_CURSOR_EXISTS = 0x0040
SERVER_STATUS_LAST_ROW_SENT = 0x0080

COM_QUERY = 0x03
COM_STMT_PREPARE = 0x16
COM_STMT_EXECUTE = 0x17
COM_STMT_FETCH = 0x1C
COM_QUIT = 0x01

CURSOR_TYPE_READ_ONLY = 0x01

FIELD_TYPE_LONG = 3
FIELD_TYPE_LONGLONG = 8


class WireError(Exception):
    def __init__(self, errno: int, message: str, sqlstate: str | None = None) -> None:
        super().__init__(f"({errno}) {message}")
        self.errno = errno
        self.message = message
        self.sqlstate = sqlstate


def _lenenc_int(value: int) -> bytes:
    if value < 251:
        return bytes([value])
    if value < 1 << 16:
        return b"\xfc" + struct.pack("<H", value)
    if value < 1 << 24:
        return b"\xfd" + struct.pack("<I", value)[:3]
    return b"\xfe" + struct.pack("<Q", value)


def _read_lenenc_int(data: bytes, pos: int) -> tuple[int, int]:
    first = data[pos]
    if first < 0xFB:
        return first, pos + 1
    if first == 0xFC:
        return struct.unpack("<H", data[pos + 1 : pos + 3])[0], pos + 3
    if first == 0xFD:
        return struct.unpack("<I", data[pos + 1 : pos + 4] + b"\x00")[0], pos + 4
    if first == 0xFE:
        return struct.unpack("<Q", data[pos + 1 : pos + 9])[0], pos + 9
    raise WireError(0, f"非法 length-encoded 前缀：{first}")


def _read_lenenc_str(data: bytes, pos: int) -> tuple[bytes | None, int]:
    if data[pos] == 0xFB:
        return None, pos + 1
    length, pos = _read_lenenc_int(data, pos)
    return data[pos : pos + length], pos + length


def _native_password(password: bytes, scramble: bytes) -> bytes:
    if not password:
        return b""
    stage1 = hashlib.sha1(password).digest()
    stage2 = hashlib.sha1(stage1).digest()
    mixed = hashlib.sha1(scramble + stage2).digest()
    return bytes(a ^ b for a, b in zip(stage1, mixed))


def _is_eof(packet: bytes) -> bool:
    return packet[:1] == b"\xfe" and len(packet) < 9


class WireConnection:
    def __init__(
        self, host: str, port: int, user: str, password: str, timeout: float = 30
    ) -> None:
        self._sock = socket.create_connection((host, port), timeout)
        self._sock.settimeout(timeout)
        self._seq = 0
        self._scramble = b""
        self._row_columns = 0
        try:
            self._read_handshake()
            self._send_handshake_response(user, password)
        except BaseException:
            # Even a half-built connection must be closed: otherwise the server's active-connection count
            # never reaches zero and wait_closed() hangs at shutdown, hiding the real handshake error.
            self._sock.close()
            raise

    # ---------- Packet layer ----------

    def _recv(self, size: int) -> bytes:
        chunks = []
        remaining = size
        while remaining:
            chunk = self._sock.recv(remaining)
            if not chunk:
                raise ConnectionError("服务端关闭了连接")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _read_packet(self) -> bytes:
        header = self._recv(4)
        length = header[0] | (header[1] << 8) | (header[2] << 16)
        self._seq = (header[3] + 1) & 0xFF
        return self._recv(length)

    def _write_packet(self, payload: bytes) -> None:
        header = struct.pack("<I", len(payload) | (self._seq << 24))
        self._sock.sendall(header + payload)
        self._seq = (self._seq + 1) & 0xFF

    def _command(self, command: int, payload: bytes = b"") -> None:
        self._seq = 0
        self._write_packet(bytes([command]) + payload)

    def _raise_if_error(self, packet: bytes) -> None:
        if packet[:1] == b"\xff":
            errno = struct.unpack("<H", packet[1:3])[0]
            pos = 3
            sqlstate = None
            if packet[pos : pos + 1] == b"#":
                sqlstate = packet[pos + 1 : pos + 6].decode("ascii", "replace")
                pos += 6
            message = packet[pos:].decode("utf-8", "replace")
            raise WireError(errno, message, sqlstate)

    # ---------- Handshake ----------

    def _read_handshake(self) -> None:
        packet = self._read_packet()
        if packet[:1] != b"\x0a":
            raise WireError(0, f"不支持的握手协议：{packet[:1]!r}")
        pos = 1
        end = packet.index(b"\x00", pos)
        self.server_version = packet[pos:end].decode("ascii", "replace")
        pos = end + 1
        pos += 4  # connection id
        scramble = packet[pos : pos + 8]
        pos += 9  # First 8 salt bytes + filler
        capabilities = struct.unpack("<H", packet[pos : pos + 2])[0]
        pos += 2 + 1 + 2  # lower caps + charset + status
        capabilities |= struct.unpack("<H", packet[pos : pos + 2])[0] << 16
        pos += 2
        plugin_len = packet[pos]
        pos += 1 + 10
        if capabilities & CLIENT_PLUGIN_AUTH:
            part2_len = max(13, plugin_len - 8)
            scramble += packet[pos : pos + part2_len]
        # The second part ends with NUL (plugin_len includes it); native password only reads the first 20 bytes.
        self._scramble = scramble.split(b"\x00")[0][:20]

    def _send_handshake_response(self, user: str, password: str) -> None:
        capabilities = (
            CLIENT_PROTOCOL_41
            | CLIENT_SECURE_CONNECTION
            | CLIENT_PLUGIN_AUTH
            | CLIENT_PLUGIN_AUTH_LENENC_CLIENT_DATA
        )
        auth = _native_password(password.encode(), self._scramble)
        payload = b"".join(
            [
                struct.pack("<I", capabilities),
                struct.pack("<I", 0),
                bytes([45]),  # utf8mb4_general_ci
                b"\x00" * 23,
                user.encode() + b"\x00",
                _lenenc_int(len(auth)) + auth,
                b"mysql_native_password\x00",
            ]
        )
        self._write_packet(payload)
        result = self._read_packet()
        self._raise_if_error(result)
        if result[:1] != b"\x00":
            raise WireError(0, f"握手未通过：{result[:20]!r}")

    # ---------- Text protocol ----------

    @staticmethod
    def _read_text_row(packet: bytes) -> list[str | None]:
        values: list[str | None] = []
        pos = 0
        while pos < len(packet):
            raw, pos = _read_lenenc_str(packet, pos)
            values.append(None if raw is None else raw.decode("utf-8", "replace"))
        return values

    def query(self, sql: str) -> list[list[str | None]]:
        self._command(COM_QUERY, sql.encode())
        first = self._read_packet()
        self._raise_if_error(first)
        if first[:1] == b"\x00":
            return []
        column_count, _ = _read_lenenc_int(first, 0)
        for _ in range(column_count):
            self._read_packet()
        self._read_packet()  # EOF
        rows: list[list[str | None]] = []
        while True:
            packet = self._read_packet()
            self._raise_if_error(packet)
            if _is_eof(packet):
                return rows
            rows.append(self._read_text_row(packet))

    # ---------- Binary protocol ----------

    def prepare(self, sql: str) -> int:
        self._command(COM_STMT_PREPARE, sql.encode())
        packet = self._read_packet()
        self._raise_if_error(packet)
        stmt_id = struct.unpack("<I", packet[1:5])[0]
        num_params = struct.unpack("<H", packet[7:9])[0]
        if num_params:
            raise WireError(0, "测试客户端不支持带参数的语句")
        return stmt_id

    def _execute(self, stmt_id: int, flags: int) -> None:
        payload = struct.pack("<I", stmt_id) + bytes([flags]) + struct.pack("<I", 1)
        self._command(COM_STMT_EXECUTE, payload)

    def _read_resultset_header(self) -> int:
        first = self._read_packet()
        self._raise_if_error(first)
        if first[:1] == b"\x00":
            return 0
        column_count, _ = _read_lenenc_int(first, 0)
        for _ in range(column_count):
            self._read_packet()
        terminator = self._read_packet()
        self._raise_if_error(terminator)
        if not _is_eof(terminator):
            raise WireError(0, f"列定义后不是 EOF：{terminator[:10]!r}")
        return column_count

    @staticmethod
    def _read_binary_row(packet: bytes, column_count: int) -> list[int | None]:
        bitmap_len = (column_count + 7 + 2) // 8
        bitmap = packet[1 : 1 + bitmap_len]
        pos = 1 + bitmap_len
        values: list[int | None] = []
        for index in range(column_count):
            if bitmap[index // 8] & (1 << ((index + 2) % 8)):
                values.append(None)
                continue
            values.append(struct.unpack("<i", packet[pos : pos + 4])[0])
            pos += 4
        return values

    def _read_rows(self) -> tuple[list[list[int | None]], int]:
        assert self._row_columns, "必须先调用开始读取结果集的方法"
        column_count = self._row_columns
        rows: list[list[int | None]] = []
        while True:
            packet = self._read_packet()
            self._raise_if_error(packet)
            if _is_eof(packet):
                # EOF packet: 0xFE + warnings(2) + status(2); status sits in bytes 3..4.
                status = struct.unpack("<H", packet[3:5])[0]
                return rows, status
            rows.append(self._read_binary_row(packet, column_count))

    def execute_buffered(self, stmt_id: int) -> list[list[int | None]]:
        """Without a cursor: all rows come back with EXECUTE."""
        self._execute(stmt_id, flags=0)
        self._row_columns = self._read_resultset_header()
        rows, _ = self._read_rows()
        return rows

    def execute_cursor(self, stmt_id: int) -> int:
        """With a read-only cursor: only column definitions are returned; COM_STMT_FETCH pulls rows in batches."""
        self._execute(stmt_id, flags=CURSOR_TYPE_READ_ONLY)
        self._row_columns = self._read_resultset_header()
        return self._row_columns

    def fetch(self, stmt_id: int, count: int) -> tuple[list[list[int | None]], int]:
        self._command(COM_STMT_FETCH, struct.pack("<II", stmt_id, count))
        return self._read_rows()

    def fetch_all(self, stmt_id: int, batch: int) -> list[list[int | None]]:
        rows: list[list[int | None]] = []
        while True:
            batch_rows, status = self.fetch(stmt_id, batch)
            rows.extend(batch_rows)
            if status & SERVER_STATUS_LAST_ROW_SENT:
                return rows

    def close(self) -> None:
        try:
            self._command(COM_QUIT)
        except OSError:
            pass
        finally:
            self._sock.close()
