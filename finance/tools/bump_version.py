#!/usr/bin/env python3
import re, sys, pathlib
FILE = pathlib.Path("etl/__init__.py")
SRC = FILE.read_text(encoding="utf-8")
m = re.search(r'__version__\s*=\s*"([^"]+)"', SRC)
if not m:
    print("Could not find __version__", file=sys.stderr); sys.exit(1)
major, minor, patch = map(int, m.group(1).split("."))
part = sys.argv[1] if len(sys.argv) > 1 else "patch"
if   part == "major": major, minor, patch = major+1, 0, 0
elif part == "minor": minor, patch = minor+1, 0
elif part == "patch": patch += 1
else:
    print("Usage: bump_version.py [major|minor|patch]"); sys.exit(2)
new_ver = f"{major}.{minor}.{patch}"
FILE.write_text(re.sub(r'__version__\s*=\s*"[^"]+"', f'__version__ = "{new_ver}"', SRC), encoding="utf-8")
print(new_ver)
