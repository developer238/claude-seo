#!/usr/bin/env python3
"""Compare server HTML with rendered DOM for JavaScript-dependent SEO.

Usage: python scripts/js_impact.py <snapshot.json[.gz] | URL ...>
Output: a ``js-impact`` check envelope as JSON, optionally written with --out.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
from snapshot_io import (  # noqa: E402
    SnapshotError, envelope, finding, is_html_page, is_internal, load_snapshot,
    normalize_url, now_iso, write_json,
)
from render_page import render_page  # noqa: E402


def _pick_pages(snapshot: dict, limit: int) -> list[str]:
    pages = snapshot["pages"]
    eligible = sorted(
        url for url, page in pages.items()
        if is_html_page(page) and page.get("indexable") is True
    )
    root = normalize_url(snapshot.get("site") or "")
    chosen = [root] if root in eligible else []
    rest = [url for url in eligible if url not in chosen]
    slots = max(0, limit - len(chosen))
    if slots >= len(rest):
        chosen.extend(rest)
    elif slots:
        indexes = [round(i * (len(rest) - 1) / (slots - 1)) for i in range(slots)] if slots > 1 else [0]
        chosen.extend(rest[i] for i in indexes)
    return chosen


def _metrics(html: str, base: str, site_host: str) -> dict:
    soup = BeautifulSoup(html or "", "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else None
    description = soup.find("meta", attrs={"name": lambda v: isinstance(v, str) and v.lower() == "description"})
    canonical = soup.find("link", rel=lambda v: v and "canonical" in ([x.lower() for x in v] if isinstance(v, list) else str(v).lower().split()))
    h1s = [x.get_text(" ", strip=True) for x in soup.find_all("h1")]
    text_soup = BeautifulSoup(html or "", "lxml")
    for node in text_soup.find_all(["script", "style", "noscript"]):
        node.decompose()
    words = len(text_soup.get_text(" ", strip=True).split())
    links = set()
    for a in soup.select("a[href]"):
        target = normalize_url(urljoin(base, a["href"]))
        if is_internal(target, site_host) or target.startswith(("http://", "https://")):
            links.add(target)
    blocks, types = [], set()
    for node in soup.find_all("script", attrs={"type": lambda v: isinstance(v, str) and v.lower().split(";")[0].strip() == "application/ld+json"}):
        try:
            parsed = json.loads(node.string or node.get_text())
        except (ValueError, TypeError):
            continue
        blocks.append(parsed)
        stack = [parsed]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                typ = item.get("@type")
                types.update(typ if isinstance(typ, list) else ([typ] if typ else []))
                stack.extend(v for v in item.values() if isinstance(v, (dict, list)))
            elif isinstance(item, list):
                stack.extend(item)
    return {
        "title": title, "description": description.get("content") if description else None,
        "canonical": normalize_url(urljoin(base, canonical.get("href", ""))) if canonical and canonical.get("href") else None,
        "h1": h1s[0] if h1s else None, "h1_list": h1s, "words": words,
        "links": links, "jsonld_count": len(blocks), "jsonld_types": types,
        "images": len(soup.find_all("img")), "h2": len(soup.find_all("h2")),
    }


def _detail(raw: dict, rendered: dict) -> dict:
    return {
        "raw": {k: sorted(v) if isinstance(v, set) else v for k, v in raw.items()},
        "rendered": {k: sorted(v) if isinstance(v, set) else v for k, v in rendered.items()},
    }


def run(urls: list[str], *, site: str, source: dict, max_pages: int = 15,
        viewport: str = "desktop", timeout_ms: int = 15000) -> tuple[dict, bool]:
    rows: dict[str, dict] = {}
    failed = []
    spa_count = 0
    browser_missing = False
    for url in urls:
        result = render_page(url, mode="always", viewport=viewport, timeout_ms=timeout_ms)
        final_url = result.get("url") or url
        raw_html, rendered_html = result.get("raw_content") or "", result.get("content") or ""
        if result.get("is_spa"):
            spa_count += 1
        err = result.get("error")
        if err and ("playwright is required" in err.lower() or "executable doesn't exist" in err.lower() or "browser" in err.lower() and "install" in err.lower()):
            browser_missing = True
        if err or not rendered_html.strip():
            failed.append({"url": final_url, "error": err or "empty rendered content"})
        rows[final_url] = {
            "raw": _metrics(raw_html, final_url, site),
            "rendered": _metrics(rendered_html, final_url, site) if rendered_html.strip() else _metrics("", final_url, site),
            "error": err,
            "empty_rendered": not rendered_html.strip(),
        }

    specs = {
        "js-content-only": ("High", "Visible content added after JavaScript", "Render important page content on the server."),
        "js-links-only": ("Medium", "Links added after JavaScript", "Make important links available in server HTML."),
        "js-schema-only": ("Medium", "Structured data added after JavaScript", "Include structured data in server HTML."),
        "js-head-differs": ("Medium", "Head content differs after JavaScript", "Keep title, description, canonical and H1 consistent in server HTML."),
        "js-render-failed": ("Info", "JavaScript rendering failed", "Check the page and rendering diagnostics."),
    }
    affected: dict[str, list] = {key: [] for key in specs}
    links_total = 0
    for url, row in rows.items():
        raw, rendered = row["raw"], row["rendered"]
        detail = _detail(raw, rendered)
        if rendered["words"] - raw["words"] >= 50 and rendered["words"] >= raw["words"] * 1.2:
            affected["js-content-only"].append({"url": url, **detail})
        added = sorted(rendered["links"] - raw["links"])
        if added:
            links_total += len(added)
            affected["js-links-only"].append({"url": url, "targets": added[:20], **detail})
        if rendered["jsonld_count"] > raw["jsonld_count"] or rendered["jsonld_types"] - raw["jsonld_types"]:
            affected["js-schema-only"].append({"url": url, **detail})
        diffs = [key for key in ("title", "description", "canonical", "h1", "h1_list") if raw[key] != rendered[key]]
        if diffs:
            affected["js-head-differs"].append({"url": url, "differing": diffs, **detail})
        if row["error"] or row["empty_rendered"]:
            affected["js-render-failed"].append({"url": url, "error": row["error"], **detail})

    findings = []
    for fid, details in affected.items():
        if details:
            severity, title, recommendation = specs[fid]
            if fid == "js-content-only" and not any(d["raw"]["words"] < 100 and d["rendered"]["words"] > 300 for d in details):
                severity = "Medium"
            findings.append(finding(fid, severity, title, [d["url"] for d in details], recommendation=recommendation, details=details))
    counts = {fid: len(items) for fid, items in affected.items()}
    summary = {
        "pages_compared": len(rows), "pages_failed": len(failed), "finding_page_counts": counts,
        "total_js_only_links": links_total, "is_spa": spa_count,
        "verdict": "no JavaScript dependence found" if not any(counts.values()) else "JavaScript-dependent SEO elements found",
    }
    snap = {"site": site}
    output = envelope("js-impact", snap, str(source.get("snapshot", "")), summary, findings)
    output["source"] = source
    return output, browser_missing


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare raw HTML with rendered DOM for JS-only SEO elements.")
    parser.add_argument("inputs", nargs="+", help="snapshot JSON or one or more URLs")
    parser.add_argument("--max-pages", type=int, default=15)
    parser.add_argument("--viewport", choices=("desktop", "mobile"), default="desktop")
    parser.add_argument("--timeout-ms", type=int, default=15000)
    parser.add_argument("--out")
    args = parser.parse_args()
    if args.max_pages < 1 or args.timeout_ms < 1:
        parser.error("--max-pages and --timeout-ms must be positive")
    try:
        if len(args.inputs) == 1 and (args.inputs[0].endswith(".json") or args.inputs[0].endswith(".json.gz")):
            path = args.inputs[0]
            snap = load_snapshot(path)
            urls = _pick_pages(snap, args.max_pages)
            source = {"snapshot": path, "captured_at": snap.get("captured_at")}
            site = snap.get("site") or ""
        else:
            urls = args.inputs
            from urllib.parse import urlsplit
            parts = urlsplit(urls[0])
            site = f"{parts.scheme}://{parts.netloc}"
            source = {"urls": urls}
        if not urls:
            raise SnapshotError("no indexable 200 HTML pages selected")
        output, missing_browser = run(urls, site=site, source=source, max_pages=args.max_pages, viewport=args.viewport, timeout_ms=args.timeout_ms)
        if missing_browser:
            write_json({"error": "Playwright or Chromium is missing. Run the install step: pip install -r requirements.txt && playwright install chromium"}, args.out)
            return 2
        write_json(output, args.out)
        return 0
    except (SnapshotError, OSError, ValueError) as exc:
        write_json({"error": str(exc)}, args.out)
        return 2


if __name__ == "__main__":
    sys.exit(main())
