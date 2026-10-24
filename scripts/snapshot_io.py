#!/usr/bin/env python3
"""
Shared helpers for the crawl snapshot (``claude-seo-snapshot/1``).

The crawler (``crawl_snapshot.py``) writes it; the audit checks
(``link_audit.py``, ``schema_validate.py``, ``serp_title_check.py``,
``indexnow_submit.py --check``) read it. Format spec:
``skills/seo-audit/references/crawl-snapshot.md``.

Usage::

    python scripts/snapshot_io.py latest <dir>      # newest two snapshot paths, JSON
    python scripts/snapshot_io.py info <snapshot>   # format, host, captured_at, page count
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import posixpath
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

FORMAT = "claude-seo-snapshot/1"
SNAPSHOT_GLOB = "snapshot-*.json*"
NOFOLLOW_TOKENS = ("nofollow", "ugc", "sponsored")
_DEFAULT_PORTS = {"http": 80, "https": 443}


class SnapshotError(ValueError):
    """Raised for unreadable or wrong-format snapshots."""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_url(url: str) -> str:
    """Canonical form used as the ``pages`` key.

    Lowercases scheme and host, drops the fragment and a default port, resolves
    dot segments and makes an empty path ``/``. The trailing slash and the query
    are kept as linked: a redirect from ``/x`` to ``/x/`` must stay detectable.
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.port and parts.port != _DEFAULT_PORTS.get(scheme):
        host = f"{host}:{parts.port}"
    path = parts.path or "/"
    if "//" in path or "/." in path:
        trailing = path.endswith("/") and path != "/"
        path = posixpath.normpath(path)
        if path == ".":
            path = "/"
        if trailing and not path.endswith("/"):
            path += "/"
    return urlunsplit((scheme, host, path, parts.query, ""))


def bare_host(host: str) -> str:
    host = (host or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def is_internal(url: str, site_host: str) -> bool:
    """Same site: the crawl host or its www/apex twin, over http or https."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return False
    return bare_host(parts.hostname or "") == bare_host(site_host.split(":")[0])


def is_nofollow(rel: Any) -> bool:
    tokens = rel.split() if isinstance(rel, str) else (rel or [])
    return any(str(t).lower() in NOFOLLOW_TOKENS for t in tokens)


def load_snapshot(path: str | os.PathLike) -> dict:
    p = Path(path)
    try:
        opener = gzip.open if p.suffix == ".gz" else open
        with opener(p, "rt", encoding="utf-8") as fh:  # type: ignore[operator]
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise SnapshotError(f"cannot read snapshot {p}: {exc}") from exc
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise SnapshotError(f"{p} is not a {FORMAT} file")
    if not isinstance(data.get("pages"), dict):
        raise SnapshotError(f"{p} has no pages object")
    return data


def save_snapshot(snapshot: dict, path: str | os.PathLike) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    opener = gzip.open if p.suffix == ".gz" else open
    tmp = p.with_name(p.name + ".tmp")
    with opener(tmp, "wt", encoding="utf-8") as fh:  # type: ignore[operator]
        json.dump(snapshot, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, p)
    return p


def snapshot_filename(captured_at: Optional[str] = None) -> str:
    day = (captured_at or now_iso())[:10]
    return f"snapshot-{day}.json"


def list_snapshots(directory: str | os.PathLike) -> list[Path]:
    """Snapshot files in a folder, oldest first (by the date in the file name)."""
    d = Path(directory)
    found = [p for p in d.glob(SNAPSHOT_GLOB) if p.suffix in (".json", ".gz")]
    found = [p for p in found if not p.name.endswith(".tmp")]
    return sorted(found, key=lambda p: p.name)


def write_json(data: Any, out: Optional[str]) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if out and out != "-":
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


def finding(
    fid: str,
    severity: str,
    title: str,
    pages: list[str],
    *,
    count: Optional[int] = None,
    scope: str = "all",
    recommendation: str = "",
    details: Optional[list] = None,
    page_cap: int = 200,
) -> dict:
    """One entry of the check output contract (see the reference doc)."""
    return {
        "id": fid,
        "severity": severity,
        "scope": scope,
        "count": len(pages) if count is None else count,
        "title": title,
        "recommendation": recommendation,
        "pages": list(pages)[:page_cap],
        "details": details if details is not None else [],
    }


def envelope(check: str, snapshot: dict, snapshot_path: str, summary: dict, findings: list) -> dict:
    return {
        "check": check,
        "version": 1,
        "site": snapshot.get("site"),
        "generated_at": now_iso(),
        "source": {"snapshot": str(snapshot_path), "captured_at": snapshot.get("captured_at")},
        "summary": summary,
        "findings": findings,
    }


_HTML_OK = re.compile(r"html|xml", re.I)


def is_html_page(page: dict) -> bool:
    """A crawled page with parsed HTML content (status 200, HTML content type)."""
    return (
        page.get("status") == 200
        and bool(_HTML_OK.search(page.get("content_type") or ""))
        and "title" in page
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Crawl snapshot helpers")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_latest = sub.add_parser("latest", help="newest two snapshots in a folder")
    p_latest.add_argument("directory")
    p_info = sub.add_parser("info", help="print snapshot header")
    p_info.add_argument("snapshot")
    args = parser.parse_args()
    try:
        if args.cmd == "latest":
            files = list_snapshots(args.directory)
            print(
                json.dumps(
                    {
                        "newest": str(files[-1]) if files else None,
                        "previous": str(files[-2]) if len(files) > 1 else None,
                    },
                    indent=2,
                )
            )
        else:
            snap = load_snapshot(args.snapshot)
            print(
                json.dumps(
                    {
                        "format": snap["format"],
                        "host": snap.get("host"),
                        "captured_at": snap.get("captured_at"),
                        "pages": len(snap["pages"]),
                    },
                    indent=2,
                )
            )
    except SnapshotError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
