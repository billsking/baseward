"""Generate requirements.txt from the PEP 621 runtime dependencies."""

from __future__ import annotations

import argparse
import difflib
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEADER = "# Generated from pyproject.toml by scripts/sync_requirements.py; do not edit manually."


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail instead of rewriting on drift")
    args = parser.parse_args()

    with (ROOT / "pyproject.toml").open("rb") as handle:
        dependencies = tomllib.load(handle)["project"]["dependencies"]
    expected = "\n".join([HEADER, *dependencies, ""])
    requirements_path = ROOT / "requirements.txt"
    current = requirements_path.read_text(encoding="utf-8") if requirements_path.exists() else ""

    if current == expected:
        return 0
    if args.check:
        print("requirements.txt 与 pyproject.toml 不一致，请运行 python scripts/sync_requirements.py", file=sys.stderr)
        print(
            "".join(
                difflib.unified_diff(
                    current.splitlines(keepends=True),
                    expected.splitlines(keepends=True),
                    fromfile="requirements.txt",
                    tofile="pyproject.toml dependencies",
                )
            ),
            file=sys.stderr,
        )
        return 1

    requirements_path.write_text(expected, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
