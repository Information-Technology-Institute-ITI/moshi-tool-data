from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    source_path = Path(sys.argv[1])
    target_path = Path(sys.argv[2])
    source = sqlite3.connect(source_path)
    target = sqlite3.connect(target_path)
    try:
        with target:
            source.backup(target)
        result = target.execute("PRAGMA integrity_check").fetchone()
        return 0 if result is not None and result[0] == "ok" else 3
    finally:
        target.close()
        source.close()


if __name__ == "__main__":
    raise SystemExit(main())
