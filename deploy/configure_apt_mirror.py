"""Optionally switch Debian archive requests to a chosen mirror host."""

from __future__ import annotations

import re
import sys
from pathlib import Path

_HOST_LABEL = re.compile(r"[A-Za-z0-9-]+\Z")
_DEBIAN_URL = re.compile(r"https?://deb\.debian\.org(?=/)")


def configure_apt_mirror(apt_root: Path, mirror_host: str) -> int:
    if not mirror_host:
        return 0

    labels = mirror_host.split(".")
    if len(mirror_host) > 253 or any(
        len(label) > 63
        or not _HOST_LABEL.fullmatch(label)
        or label.startswith("-")
        or label.endswith("-")
        for label in labels
    ):
        raise ValueError("APT_MIRROR_HOST must be a hostname without scheme, path, or port")

    sources_dir = apt_root / "sources.list.d"
    source_files = [
        apt_root / "sources.list",
        *sources_dir.glob("*.list"),
        *sources_dir.glob("*.sources"),
    ]
    replacement_count = 0

    for source_file in source_files:
        if not source_file.is_file():
            continue

        updated_lines = []
        file_replacements = 0
        for line in source_file.read_text(encoding="utf-8").splitlines(keepends=True):
            if line.lstrip().startswith("#"):
                updated_lines.append(line)
                continue
            updated_line, count = _DEBIAN_URL.subn(
                lambda match: f"{match.group(0).split('://', 1)[0]}://{mirror_host}",
                line,
            )
            updated_lines.append(updated_line)
            file_replacements += count

        if file_replacements:
            source_file.write_text("".join(updated_lines), encoding="utf-8")
            replacement_count += file_replacements

    if replacement_count == 0:
        raise ValueError("APT_MIRROR_HOST was set but no deb.debian.org source was found")
    return replacement_count


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: configure_apt_mirror.py HOST", file=sys.stderr)
        return 2

    try:
        count = configure_apt_mirror(Path("/etc/apt"), args[0])
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if count:
        print(f"Configured {count} Debian source URI(s) via {args[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
