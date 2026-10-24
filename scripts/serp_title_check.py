#!/usr/bin/env python3
"""Compare snapshot page titles with stored Google SERP titles.

Usage: python scripts/serp_title_check.py snapshot.json --serp serp.json [--domain example.com]
Output: check envelope with id ``serp-titles`` and title mismatch details.
"""

from __future__ import annotations

import argparse
import difflib
import html
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

_DIR = str(Path(__file__).resolve().parent)
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)
from snapshot_io import (  # noqa: E402
    SnapshotError,
    bare_host,
    finding,
    load_snapshot,
    normalize_url,
    now_iso,
    write_json,
)


def _results(data):
    if isinstance(data, dict):
        if isinstance(data.get("tasks"), list):
            for task in data["tasks"]:
                for result in task.get("result", []) or []:
                    yield result
        elif "keyword" in data and "items" in data:
            yield data
        elif "keyword" in data and "url" in data and "title" in data:
            yield {
                "keyword": data.get("keyword"),
                "datetime": data.get("checked_at"),
                "items": [
                    {
                        "type": "organic",
                        "url": data.get("url"),
                        "title": data.get("title"),
                        "rank_group": data.get("position"),
                    }
                ],
            }
        elif "result" in data:
            yield from _results(data["result"])
    elif isinstance(data, list):
        for item in data:
            yield from _results(item)


def _normal(s):
    s = html.unescape(str(s or ""))
    return re.sub(
        r"\s+",
        " ",
        s.replace(chr(0x2013), "-").replace(chr(0x2014), "-").replace("|", "| ").strip(),
    ).casefold()


def classify(page, serp, minimum):
    p, s = _normal(page), _normal(serp)
    ratio = difflib.SequenceMatcher(None, p, s).ratio()
    if p == s:
        return "exact", ratio
    for ellipsis in ("...", "…"):
        if s.endswith(ellipsis) and p.startswith(s[: -len(ellipsis)]):
            return "truncated", ratio
        if p.endswith(ellipsis) and s.startswith(p[: -len(ellipsis)]):
            return "truncated", ratio
    for sep in (" | ", " - "):
        if p == s.split(sep)[0] or s == p.split(sep)[0]:
            return "brand-only", ratio
    return ("rewritten" if ratio < minimum else "different"), ratio


def _host(url):
    return bare_host(urlsplit(url).hostname or "")


def run(snapshot_path, serp_paths, domain=None, minimum=0.85):
    snap = load_snapshot(snapshot_path)
    sitehost = bare_host(domain or snap.get("host", urlsplit(snap.get("site", "")).hostname or ""))
    pages = snap["pages"]
    lookup = {normalize_url(p.get("url", u)): p for u, p in pages.items() if isinstance(p, dict)}
    observations, missed, newest = {}, set(), {}
    for filename in serp_paths:
        data = json.loads(Path(filename).read_text(encoding="utf-8"))
        latest_file = None
        for group in _results(data):
            dt = group.get("datetime") or group.get("checked_at") or ""
            latest_file = max(latest_file or "", dt)
            for item in group.get("items", []) or []:
                if item.get("type", "organic") != "organic":
                    continue
                u = item.get("url")
                if not u or _host(u) != sitehost:
                    continue
                key = normalize_url(u)
                page = lookup.get(key)
                if not page:
                    key = key.rstrip("/")
                    page = next(
                        (
                            v
                            for k, v in lookup.items()
                            if k.rstrip("/") == key
                            or _host(k) == sitehost
                            and k.rstrip("/") == key.replace("www.", "")
                        ),
                        None,
                    )
                if not page:
                    page = next(
                        (
                            v
                            for v in pages.values()
                            if isinstance(v, dict)
                            and v.get("redirect_to")
                            and normalize_url(v["redirect_to"]) == normalize_url(u)
                        ),
                        None,
                    )
                if not page:
                    missed.add(u)
                    continue
                obs = {
                    "url": page.get("url", u),
                    "page_title": page.get("title", ""),
                    "serp_title": item.get("title", ""),
                    "keyword": group.get("keyword", ""),
                    "position": item.get("rank_group", item.get("position")),
                    "observed": dt,
                }
                pair = (normalize_url(u), obs["keyword"])
                if pair not in observations or dt >= observations[pair]["observed"]:
                    observations[pair] = obs
        newest[str(filename)] = latest_file
    if not observations and not missed:
        raise ValueError("no organic items found for the requested domain")
    mismatch_groups, classes, matched = {}, {}, set()
    for obs in observations.values():
        cls, sim = classify(obs["page_title"], obs["serp_title"], minimum)
        matched.add(obs["url"])
        if cls not in ("exact", "truncated"):
            key = (normalize_url(obs["url"]), obs["serp_title"])
            if key not in mismatch_groups:
                mismatch_groups[key] = {
                    **obs,
                    "class": cls,
                    "similarity": round(sim, 4),
                    "keywords": [],
                    "observations": 0,
                }
            mismatch_groups[key]["keywords"].append(obs["keyword"])
            mismatch_groups[key]["observations"] += 1
            classes[cls] = classes.get(cls, 0) + 1
    mismatches = list(mismatch_groups.values())
    badpages = list(dict.fromkeys(d["url"] for d in mismatches))
    summary = {
        "serp_urls_matched_to_snapshot": len(matched),
        "serp_urls_not_in_snapshot": sorted(missed)[:20],
        "mismatches_by_class": classes,
        "pages_compared": len(matched),
        "pages_mismatching": len(badpages),
        "observations_used": len(observations),
        "serp_files": newest,
        "note": "Google rewrites titles by its own rules. Rewritten titles are advice to align the title to the query or shorten it, not an error.",
    }
    findings = (
        [
            finding(
                "serp-title-mismatch",
                "Medium",
                "Google SERP titles differ from page titles",
                badpages,
                recommendation="Review query alignment and title length; Google may rewrite titles by its own rules.",
                details=mismatches,
            )
        ]
        if mismatches
        else []
    )
    return {
        "check": "serp-titles",
        "version": 1,
        "site": snap.get("site"),
        "generated_at": now_iso(),
        "source": {"snapshot": str(snapshot_path), "captured_at": snap.get("captured_at")},
        "summary": summary,
        "findings": findings,
    }


def main():
    p = argparse.ArgumentParser(description="Compare snapshot titles with stored SERP titles")
    p.add_argument("snapshot")
    p.add_argument("--serp", action="append", required=True)
    p.add_argument("--domain")
    p.add_argument("--min-similarity", type=float, default=0.85)
    p.add_argument("--out")
    a = p.parse_args()
    try:
        write_json(run(a.snapshot, a.serp, a.domain, a.min_similarity), a.out)
        return 0
    except (OSError, ValueError, SnapshotError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
