"""兼容入口：``python -m ling.backup_restore.switch_artifact``。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .rollback import switch_artifact as _switch


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("package", type=Path)
    parser.add_argument("target_dir", type=Path)
    args = parser.parse_args()
    print(json.dumps(_switch(args.package, args.target_dir), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
