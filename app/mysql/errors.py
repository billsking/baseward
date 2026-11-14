"""Unified format for export-channel client error messages (§6.3)."""

from __future__ import annotations


def format_message(code: str, message: str, request_id: str) -> str:
    return f"[GATEWAY:{code}] {message} (request_id={request_id})"
