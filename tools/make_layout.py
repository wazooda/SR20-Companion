"""Generate layout.json for an MSFS package folder.

Usage:  python -I tools/make_layout.py <package_dir>

layout.json lists every file in the package (except itself and manifest.json)
with its size and last-write time as a Windows FILETIME (100 ns ticks since
1601-01-01), which is what the sim expects.
"""
import json
import os
import sys

EPOCH_DIFF_100NS = 116444736000000000  # 1601-01-01 -> 1970-01-01


def filetime(path: str) -> int:
    return int(os.stat(path).st_mtime * 10_000_000) + EPOCH_DIFF_100NS


def main(pkg: str) -> None:
    pkg = os.path.abspath(pkg)
    entries = []
    for root, dirs, files in os.walk(pkg):
        dirs.sort()  # deterministic walk order so layout.json diffs are meaningful
        for name in sorted(files):
            full = os.path.join(root, name)
            rel = os.path.relpath(full, pkg).replace(os.sep, "/")
            if rel in ("layout.json", "manifest.json"):
                continue
            entries.append({"path": rel, "size": os.path.getsize(full), "date": filetime(full)})
    out = os.path.join(pkg, "layout.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"content": entries}, fh, indent=2)
    print(f"wrote {out} ({len(entries)} files)")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
