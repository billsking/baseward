#!/usr/bin/env python
"""Generate a base64 random value (32 bytes) for GATEWAY_MASTER_KEY.

Usage:
    python scripts/gen_master_key.py
The output contains only the key itself, ready for .env or a container secret.
"""

from __future__ import annotations

import base64
import secrets

KEY_BYTES = 32


def generate_master_key() -> str:
    return base64.b64encode(secrets.token_bytes(KEY_BYTES)).decode("ascii")


def main() -> None:
    print(generate_master_key())


if __name__ == "__main__":
    main()
