"""Build a --require-hashes lockfile from a pip --dry-run --report JSON."""

import json
import pathlib
import sys

report = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
out = pathlib.Path(sys.argv[2])

rows = []
missing = []
for item in report["install"]:
    meta = item["metadata"]
    name = meta["name"]
    version = meta["version"]
    sha = item.get("download_info", {}).get("archive_info", {}).get("hashes", {}).get("sha256")
    if not sha:
        missing.append(name)
        continue
    rows.append((name.lower(), name, version, sha))

rows.sort()

header = """# Raseed dependency lockfile.
#
# Generated, do not hand-edit. Every package in this file traces back to an
# entry on the allowlist in section 23.1 of the brief, or is a transitive
# dependency of one. Adding a direct dependency means editing pyproject.toml
# and regenerating, not appending here.
#
# Install with:
#     python -m pip install --require-hashes -r requirements.txt
#
# Regenerate with:
#     python -m pip freeze --exclude-editable | findstr /v "^pip==" > pinned.txt
#     python -m pip install --dry-run --ignore-installed --report report.json -r pinned.txt
#     python tools/mklock.py report.json requirements.txt
#
# Platform note: the hashes below are for the wheels pip selected on
# CPython 3.14, Windows x86_64. Some of these packages ship per-platform
# wheels (pydantic-core, pypdfium2, cryptography, greenlet, pillow, RapidFuzz,
# cffi, MarkupSafe, librt). Installing on Linux or macOS will fail the hash
# check and the lockfile must be regenerated there, or extended with the
# additional per-platform hashes.
"""

lines = [header]
for _, name, version, sha in rows:
    lines.append(f"{name}=={version} \\\n    --hash=sha256:{sha}")

out.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"wrote {len(rows)} pinned packages to {out}")
if missing:
    print(f"MISSING HASHES: {missing}")
