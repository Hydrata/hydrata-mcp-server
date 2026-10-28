"""Fail unless pyproject.toml's version equals server.json's (registry metadata)."""

import json
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    py = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    sj = json.loads((ROOT / "server.json").read_text())["version"]
    if py != sj:
        print(f"version mismatch: pyproject.toml={py} server.json={sj}", file=sys.stderr)
        return 1
    print(f"version parity ok: {py}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
