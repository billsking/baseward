"""Global test configuration: a minimal environment for importing app.main."""

import base64
import os

os.environ.setdefault("GATEWAY_MASTER_KEY", base64.b64encode(b"k" * 32).decode("ascii"))
os.environ.setdefault("GATEWAY_DB_HOST", "127.0.0.1")
os.environ.setdefault("GATEWAY_DB_USER", "test")
os.environ.setdefault("GATEWAY_DB_PASSWORD", "test")
