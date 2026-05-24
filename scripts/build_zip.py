#!/usr/bin/env python3
"""
Package the claude-seo plugin as an uploadable zip.

Produces dist/claude-seo.zip containing the skill bundle (skills/, agents/,
scripts/, bin/, hooks/, schema/, data/, extensions/, docs/, .claude-plugin/,
CLAUDE.md, README.md, AGENTS.md, CONTRIBUTORS.md, requirements.txt).

Excludes: .git, virtualenvs, caches, all credential files, the dist/ dir
itself, and tests/.
"""

import fnmatch
import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
OUT = DIST / "claude-seo.zip"

EXCLUDE_DIRS = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
    "node_modules", "dist", "build", "tests", ".idea", ".vscode",
}

EXCLUDE_PATTERNS = [
    "*.pyc", "*.pyo", "*.pyd",
    ".env", ".env.*",
    "oauth-token.json", "client_secret*.json", "service_account*.json",
    "*.log", ".DS_Store", "Thumbs.db",
]


def is_excluded(path: Path) -> bool:
    parts = set(path.parts)
    if parts & EXCLUDE_DIRS:
        return True
    name = path.name
    for pat in EXCLUDE_PATTERNS:
        if fnmatch.fnmatch(name, pat):
            return True
    return False


def main():
    DIST.mkdir(exist_ok=True)
    if OUT.exists():
        OUT.unlink()

    count = 0
    with zipfile.ZipFile(OUT, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
            for fname in filenames:
                full = Path(dirpath) / fname
                rel = full.relative_to(ROOT)
                if is_excluded(rel):
                    continue
                zf.write(full, arcname=str(Path("claude-seo") / rel))
                count += 1

    size_mb = OUT.stat().st_size / (1024 * 1024)
    print(f"Wrote {OUT} ({count} files, {size_mb:.2f} MB)")


if __name__ == "__main__":
    main()
