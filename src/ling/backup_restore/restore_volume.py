"""兼容入口：``python -m ling.backup_restore.restore_volume``。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .rollback import restore_media_archive


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("media_tar", type=Path)
    parser.add_argument("target_dir", type=Path)
    args = parser.parse_args()
    files = restore_media_archive(args.media_tar, args.target_dir)
    print(json.dumps({"restored_files": len(files)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
