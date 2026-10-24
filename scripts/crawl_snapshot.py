#!/usr/bin/env python3
"""Bounded crawler and checks for claude-seo snapshot/1.

Usage: crawl URL [options], onpage SNAPSHOT, or diff OLD NEW.
Output: snapshot JSON or the shared check envelope.
Static asset URLs are skipped and do not count toward the page limit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qsl, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)
from snapshot_io import (  # noqa: E402
    FORMAT,
    SnapshotError,
    envelope,
    finding,
    is_html_page,
    is_internal,
    is_nofollow,
    list_snapshots,
    load_snapshot,
    normalize_url,
    now_iso,
    save_snapshot,
    snapshot_filename,
    write_json,
)
from url_safety import DEFAULT_USER_AGENT, decode_body, safe_requests_get, validate_url_strict  # noqa: E402

MAX_BODY = 3 * 1024 * 1024
STATIC_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".avif",
    ".svg",
    ".ico",
    ".bmp",
    ".tiff",
    ".css",
    ".js",
    ".mjs",
    ".map",
    ".json",
    ".xml",
    ".txt",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".eot",
    ".mp4",
    ".webm",
    ".mp3",
    ".wav",
    ".ogg",
    ".zip",
    ".gz",
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
}

# url_safety pins DNS through a process-wide patch and refuses concurrent use,
# so the network call itself is serialised. Parsing and the politeness delay
# still overlap across worker threads.
_FETCH_LOCK = threading.Lock()


def fetch(url, timeout=30, user_agent=None):
    """Replaceable SSRF-safe GET, without redirect following."""
    with _FETCH_LOCK:
        r = safe_requests_get(
            url,
            timeout=timeout,
            allow_redirects=False,
            stream=True,
            headers={"User-Agent": user_agent or DEFAULT_USER_AGENT},
        )
    body = bytearray()
    truncated = False
    for chunk in r.iter_content(65536):
        if len(body) + len(chunk) > MAX_BODY:
            body.extend(chunk[: MAX_BODY - len(body)])
            truncated = True
            break
        body.extend(chunk)
    out = {
        "status": r.status_code,
        "headers": dict(r.headers),
        "body": bytes(body),
        "truncated": truncated,
    }
    r.close()
    return out


def extract_html(html, page_url, site_host, headers=None):
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    desc = next(
        (
            m.get("content", "").strip()
            for m in soup.find_all("meta")
            if m.get("name", "").lower() == "description"
        ),
        "",
    )
    h1 = [x.get_text(" ", strip=True) for x in soup.find_all("h1")]
    c = soup.find(
        "link",
        rel=lambda v: (
            v
            and "canonical"
            in ([x.lower() for x in v] if isinstance(v, list) else v.lower().split())
        ),
    )
    canonical = urljoin(page_url, c["href"]) if c and c.get("href") else None
    b = soup.find("base", href=True)
    base = urljoin(page_url, b["href"]) if b else page_url
    robots = []
    for m in soup.find_all("meta"):
        if m.get("name", "").lower() in ("robots", "googlebot") and m.get("content"):
            robots.append(m["content"].strip())
    text_soup = BeautifulSoup(str(soup), "html.parser")
    for node in text_soup.find_all(
        ["script", "style", "noscript", "nav", "header", "footer", "aside"]
    ):
        node.decompose()
    main = text_soup.find("main") or text_soup.body or text_soup
    text = " ".join(main.get_text(" ", strip=True).split())
    links = []
    seen = set()
    for a in soup.find_all("a", href=True):
        raw = a["href"].strip()
        if (
            not raw
            or raw.startswith("#")
            or raw.lower().startswith(("mailto:", "tel:", "javascript:", "data:"))
        ):
            continue
        target = normalize_url(urljoin(base, raw))
        if target in seen:
            continue
        seen.add(target)
        links.append(
            {
                "target": target,
                "internal": is_internal(target, site_host),
                "nofollow": is_nofollow(a.get("rel")),
                "text": a.get_text(" ", strip=True)[:100],
            }
        )
    jsonld = []
    errors = []
    for i, s in enumerate(
        soup.find_all("script", type=lambda v: v and v.lower() == "application/ld+json"), 1
    ):
        raw = re.sub(r"^\s*<!--|-->\s*$", "", (s.string or s.get_text()).strip())
        try:
            jsonld.append(json.loads(raw))
        except (ValueError, TypeError) as e:
            errors.append("block %d: %s" % (i, e))
    return {
        "title": title,
        "title_len": len(title),
        "meta_description": desc,
        "meta_description_len": len(desc),
        "h1": h1,
        "word_count": len(text.split()),
        "content_hash": hashlib.sha1(text.encode()).hexdigest(),
        "lang": soup.html.get("lang") if soup.html else None,
        "canonical": canonical,
        "meta_robots": ", ".join(dict.fromkeys(robots)) or None,
        "x_robots_tag": (headers or {}).get("X-Robots-Tag") or (headers or {}).get("x-robots-tag"),
        "links": links,
        "jsonld": jsonld,
        "jsonld_errors": errors,
    }


def indexability(p):
    if p.get("status") != 200:
        return False, "non-200"
    if "title" not in p:
        return False, "non-html"
    if re.search(r"\b(noindex|none)\b", p.get("meta_robots") or "", re.I):
        return False, "noindex-meta"
    if re.search(r"\b(noindex|none)\b", p.get("x_robots_tag") or "", re.I):
        return False, "noindex-header"
    c = p.get("canonical")
    if c:
        a, b = urlsplit(normalize_url(c)), urlsplit(normalize_url(p["url"]))
        ah = (a.hostname or "").lower()
        bh = (b.hostname or "").lower()
        if ah.startswith("www."):
            ah = ah[4:]
        if bh.startswith("www."):
            bh = bh[4:]
        if (
            a.scheme != b.scheme
            or ah != bh
            or a.path.rstrip("/") != b.path.rstrip("/")
            or a.query != b.query
        ):
            return False, "canonical-other"
    return True, "ok"


def is_crawlable_url(url):
    """Reject static assets and paths likely to be generated crawl traps."""
    parts = urlsplit(url)
    if Path(parts.path.lower()).suffix in STATIC_EXTENSIONS:
        return False
    if len(parse_qsl(parts.query, keep_blank_values=True)) > 6:
        return False
    segments = [segment for segment in parts.path.split("/") if segment]
    return not any(segments.count(segment) >= 4 for segment in set(segments))


def seed_urls(start, args, host, origin):
    """Return start and bounded sitemap URL seeds, expanding sitemap indexes."""
    robots = RobotFileParser()
    robots.set_url(origin + "/robots.txt")
    sitemap_urls = []
    robots_status = None
    if not args.no_robots:
        try:
            rr = fetch(origin + "/robots.txt", args.timeout, args.user_agent)
            robots_status = rr["status"]
        except Exception as exc:
            raise RuntimeError(
                "robots.txt unavailable (status or error); retry later or pass --no-robots"
            ) from exc
        if robots_status >= 500 or 300 <= robots_status < 400 or robots_status < 200:
            raise RuntimeError(
                "robots.txt unavailable (status or error); retry later or pass --no-robots"
            )
        if robots_status == 200:
            lines = rr["body"].decode("utf-8", "replace").splitlines()
            robots.parse(lines)
            sitemap_urls = [
                m.group(1)
                for line in lines
                if (m := re.match(r"\s*Sitemap\s*:\s*(\S+)", line, re.I))
            ]
        else:
            robots.parse([])
    else:
        robots.parse([])
    candidates = list(dict.fromkeys(sitemap_urls + [origin + "/sitemap.xml"]))
    expanded = []
    checked = set()
    while candidates and len(checked) < 16 and len(expanded) < args.max_pages:
        sitemap_url = candidates.pop(0)
        try:
            sitemap_url = normalize_url(sitemap_url)
            if sitemap_url in checked or not is_internal(sitemap_url, host):
                continue
            checked.add(sitemap_url)
            response = fetch(sitemap_url, args.timeout, args.user_agent)
            if response["status"] != 200:
                continue
            import xml.etree.ElementTree as ET

            root = ET.fromstring(response["body"])
            locs = [
                el.text.strip()
                for el in root.iter()
                if el.tag.endswith("loc") and el.text and el.text.strip()
            ]
            is_index = root.tag.endswith("sitemapindex")
            for loc in locs:
                target = normalize_url(loc)
                if not is_internal(target, host):
                    continue
                if is_index and target not in checked:
                    candidates.append(target)
                elif not is_index and is_crawlable_url(target):
                    expanded.append(target)
                    if len(expanded) >= args.max_pages:
                        break
        except Exception:
            continue
    urls = [url for url in [start] + expanded if is_crawlable_url(url)]
    return robots, list(dict.fromkeys(urls)), robots_status


def fetch_record(url, host, robots, args):
    """Fetch and parse one URL; return its record and discovered targets."""
    if not args.no_robots and not robots.can_fetch(args.user_agent or DEFAULT_USER_AGENT, url):
        return (
            url,
            {
                "url": url,
                "status": None,
                "final_url": url,
                "redirect_to": None,
                "redirect_chain": [],
                "content_type": None,
                "indexable": False,
                "indexability_reason": "robots-blocked",
            },
            [],
        )
    for attempt in range(2):
        try:
            response = fetch(url, args.timeout, args.user_agent)
            if response["status"] == 429 and not attempt:
                time.sleep(5)
                continue
            break
        except Exception:
            if attempt:
                raise
    time.sleep(max(args.delay, 0))
    headers = response["headers"]
    content_type = headers.get("Content-Type", "")
    page = {
        "url": url,
        "status": response["status"],
        "final_url": url,
        "redirect_to": None,
        "redirect_chain": [],
        "content_type": content_type,
    }
    if response.get("truncated"):
        page["truncated"] = True
    targets = []
    if 300 <= response["status"] < 400 and headers.get("Location"):
        page["redirect_to"] = normalize_url(urljoin(url, headers["Location"]))
        if is_internal(page["redirect_to"], host) and is_crawlable_url(page["redirect_to"]):
            targets.append(page["redirect_to"])
    elif response["status"] == 200 and "html" in content_type.lower():
        page.update(extract_html(decode_body(response["body"], content_type), url, host, headers))
        targets = [link["target"] for link in page["links"] if link["internal"]]
    indexable, reason = indexability(page)
    page.update(indexable=indexable, indexability_reason=reason)
    return url, page, targets


def add_redirect_chains(pages):
    """Set redirect chains and final URLs, with a five-hop loop guard."""
    for page in pages.values():
        target = page.get("redirect_to")
        visited = {page["url"]}
        chain = []
        final_url = page.get("redirect_to") or page["url"]
        for _ in range(5):
            if not target or target in visited or target not in pages:
                break
            visited.add(target)
            next_page = pages[target]
            chain.append({"url": target, "status": next_page.get("status")})
            final_url = target
            target = next_page.get("redirect_to")
        page["redirect_chain"] = chain
        if page.get("redirect_to"):
            page["final_url"] = final_url


def build_snapshot(start, host, origin, pages, args, robots_status):
    snapshot = {
        "format": FORMAT,
        "site": origin,
        "host": host,
        "captured_at": now_iso(),
        "tool": {"name": "crawl_snapshot.py", "version": "1"},
        "settings": {
            "max_pages": args.max_pages,
            "concurrency": args.concurrency,
            "robots": not args.no_robots,
            "robots_status": robots_status,
            "delay": args.delay,
            "user_agent": args.user_agent or DEFAULT_USER_AGENT,
        },
        "pages": pages,
    }
    if args.refdomains:
        raw = Path(args.refdomains).read_text(encoding="utf-8")
        try:
            domains = json.loads(raw)
        except ValueError:
            domains = raw.splitlines()
        snapshot["referring_domains"] = {
            "source": "file",
            "captured_at": now_iso(),
            "domains": sorted({str(x).strip().lower() for x in domains if str(x).strip()}),
        }
    return snapshot


def crawl_site(start, args):
    validate_url_strict(start)
    start = normalize_url(start)
    parts = urlsplit(start)
    host = parts.hostname
    origin = parts.scheme + "://" + parts.netloc
    robots, seeds, robots_status = seed_urls(start, args, host, origin)
    queue = deque(seeds)
    queued = set(seeds)
    pages = {}

    started = time.monotonic()
    errors = 0
    while queue and len(pages) < args.max_pages:
        batch = []
        while queue and len(batch) < args.concurrency and len(pages) + len(batch) < args.max_pages:
            u = queue.popleft()
            if u not in pages:
                batch.append(u)
        with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
            futures = [pool.submit(fetch_record, u, host, robots, args) for u in batch]
            for u, f in zip(batch, futures):
                try:
                    key, p, targets = f.result()
                except Exception as e:
                    errors += 1
                    key = u
                    p = {
                        "url": u,
                        "status": None,
                        "final_url": u,
                        "redirect_to": None,
                        "redirect_chain": [],
                        "content_type": None,
                        "indexable": False,
                        "indexability_reason": "non-200",
                        "error": str(e),
                    }
                    targets = []
                pages[key] = p
                for t in targets:
                    try:
                        t = normalize_url(t)
                    except Exception:
                        continue
                    if (
                        is_internal(t, host)
                        and is_crawlable_url(t)
                        and t not in queued
                        and len(queued) < args.max_pages * 3
                    ):
                        queued.add(t)
                        queue.append(t)
                print("crawled %d/%d %s" % (len(pages), args.max_pages, key), file=sys.stderr)
    add_redirect_chains(pages)
    snap = build_snapshot(start, host, origin, pages, args, robots_status)
    return snap, errors, time.monotonic() - started


def onpage(path, max_desc):
    snap = load_snapshot(path)
    pages = snap["pages"]
    html = {u: p for u, p in pages.items() if is_html_page(p)}
    over = []
    missing = []
    titles = []
    for u, p in html.items():
        desc = p.get("meta_description") or ""
        idx = bool(p.get("indexable"))
        if len(desc) > max_desc:
            over.append({"url": u, "length": len(desc), "indexable": idx})
        if idx and not desc:
            missing.append(u)
        if idx and not (p.get("title") or "").strip():
            titles.append(u)
    over.sort(key=lambda x: (-x["length"], x["url"]))
    candidates = [
        finding(
            "meta-description-too-long",
            "Low",
            "Meta descriptions exceed the configured character limit",
            [x["url"] for x in over],
            scope="all",
            details=over,
        )
    ]
    candidates += [
        finding(
            "meta-description-missing",
            "Medium",
            "Indexable pages have no meta description",
            missing,
            scope="indexable",
        ),
        finding(
            "title-missing", "High", "Indexable pages have no title", titles, scope="indexable"
        ),
    ]
    fs = [item for item in candidates if item["count"]]
    return envelope(
        "onpage",
        snap,
        path,
        {
            "html_pages": len(html),
            "indexable": sum(bool(p.get("indexable")) for p in html.values()),
            "non_indexable": sum(not bool(p.get("indexable")) for p in html.values()),
            "meta_description_too_long": len(over),
            "meta_description_too_long_indexable": sum(x["indexable"] for x in over),
            "meta_description_too_long_non_indexable": sum(not x["indexable"] for x in over),
            "meta_description_missing": len(missing),
            "title_missing": len(titles),
            "at_155": sum(len(p.get("meta_description") or "") > 155 for p in html.values()),
        },
        fs,
    )


def diff(old_path, new_path, wc_pct, wc_min):
    old, new = load_snapshot(old_path), load_snapshot(new_path)
    op, np = old["pages"], new["pages"]
    oh = {u: p for u, p in op.items() if is_html_page(p)}
    nh = {u: p for u, p in np.items() if is_html_page(p)}
    common = sorted(set(oh) & set(nh))
    changed = {}
    fs = []
    counts = {
        fid: 0
        for fid in (
            "title-changed",
            "meta-description-changed",
            "h1-changed",
            "word-count-changed",
            "pages-added",
            "pages-removed",
            "status-changed",
            "indexability-changed",
            "referring-domains-lost",
            "referring-domains-new",
        )
    }

    def add(fid, sev, title, details, scope="all"):
        details.sort(key=lambda x: x["url"] if isinstance(x, dict) and "url" in x else str(x))
        counts[fid] = len(details)
        if details:
            fs.append(
                finding(
                    fid,
                    sev,
                    title,
                    [x["url"] for x in details if isinstance(x, dict) and "url" in x] or [],
                    count=len(details),
                    scope=scope,
                    details=details,
                )
            )

    for field, fid, title in [
        ("title", "title-changed", "Page titles changed"),
        ("meta_description", "meta-description-changed", "Meta descriptions changed"),
        ("h1", "h1-changed", "H1 content changed"),
    ]:
        ds = []
        for u in common:
            a, b = oh[u].get(field), nh[u].get(field)
            if field == "h1":
                a = " ".join(" ".join(a or []).split())
                b = " ".join(" ".join(b or []).split())
            else:
                a = " ".join(str(a or "").split())
                b = " ".join(str(b or "").split())
            if a != b:
                ds.append({"url": u, "old": a, "new": b})
                changed[u] = 1
        add(fid, "Info", title, ds)
    ds = []
    for u in common:
        a, b = oh[u].get("word_count", 0), nh[u].get("word_count", 0)
        delta = b - a
        if abs(delta) >= wc_min and (abs(delta) / max(abs(a), 1) * 100) >= wc_pct:
            ds.append({"url": u, "old": a, "new": b, "delta": delta})
            changed[u] = 1
    add("word-count-changed", "Info", "Visible word counts changed", ds)
    added = sorted(set(nh) - set(oh))
    removed = sorted(set(oh) - set(nh))
    add("pages-added", "Info", "200 HTML pages added", [{"url": u} for u in added])
    add("pages-removed", "Medium", "200 HTML pages removed", [{"url": u} for u in removed])
    changed.update({u: 1 for u in added})
    ds = []
    for u in sorted(set(op) | set(np)):
        a = op.get(u, {}).get("status")
        b = np.get(u, {}).get("status")
        if a != b:
            ds.append({"url": u, "old": a, "new": b})
    status_severity = (
        "Medium"
        if any(d["old"] == 200 and isinstance(d["new"], int) and d["new"] >= 300 for d in ds)
        else "Info"
    )
    counts["status-changed"] = len(ds)
    if ds:
        fs.append(
            finding(
                "status-changed",
                status_severity,
                "HTTP status changed",
                [d["url"] for d in ds],
                count=len(ds),
                details=ds,
            )
        )
    ds = []
    for u in sorted(set(op) & set(np)):
        if op[u].get("indexable") and not np[u].get("indexable"):
            ds.append(
                {
                    "url": u,
                    "old": op[u].get("indexability_reason"),
                    "new": np[u].get("indexability_reason"),
                }
            )
    add("indexability-changed", "High", "Indexability was lost", ds)
    note = None
    if "referring_domains" in old and "referring_domains" in new:
        a = set(old["referring_domains"].get("domains", []))
        b = set(new["referring_domains"].get("domains", []))
        lost = sorted(a - b)
        fresh = sorted(b - a)
        add(
            "referring-domains-lost",
            "Medium",
            "Referring domains lost",
            [{"domain": x} for x in lost],
        )
        add(
            "referring-domains-new",
            "Info",
            "Referring domains gained",
            [{"domain": x} for x in fresh],
        )
    elif ("referring_domains" in old) != ("referring_domains" in new):
        note = "Referring domains are present in only one snapshot"
    summary = {"counts": counts, "changed_urls": sorted(changed)}
    if note:
        summary["referring_domains_note"] = note
    return {
        "check": "diff",
        "version": 1,
        "site": new.get("site"),
        "generated_at": now_iso(),
        "source": {
            "old": {"path": str(old_path), "captured_at": old.get("captured_at")},
            "new": {"path": str(new_path), "captured_at": new.get("captured_at")},
        },
        "summary": summary,
        "findings": fs,
    }


def main():
    parser = argparse.ArgumentParser(description="Crawl and check site snapshots")
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("crawl")
    c.add_argument("url")
    c.add_argument("--max-pages", type=int, default=500)
    c.add_argument(
        "--concurrency",
        type=int,
        default=3,
        help="Worker count; requests are serialised, concurrency overlaps parsing and delay",
    )
    c.add_argument("--delay", type=float, default=0.5)
    c.add_argument("--timeout", type=int, default=30)
    c.add_argument("--no-robots", action="store_true")
    c.add_argument("--user-agent")
    c.add_argument("--refdomains")
    c.add_argument("--out")
    o = sub.add_parser("onpage")
    o.add_argument("snapshot")
    o.add_argument("--max-desc", type=int, default=160)
    o.add_argument("--out")
    d = sub.add_parser("diff")
    d.add_argument("paths", nargs="*")
    d.add_argument("--latest")
    d.add_argument("--wc-pct", type=float, default=10)
    d.add_argument("--wc-min", type=int, default=50)
    d.add_argument("--out")
    args = parser.parse_args()
    try:
        if args.cmd == "crawl":
            if args.max_pages < 1 or args.concurrency < 1:
                raise ValueError("max-pages and concurrency must be positive")
            snap, errors, duration = crawl_site(args.url, args)
            if (
                not snap["pages"]
                or snap["pages"].get(normalize_url(args.url), {}).get("status") is None
            ):
                raise RuntimeError("start URL could not be fetched")
            out = args.out or ("%s-audit/snapshots" % snap["host"])
            p = Path(out)
            path = (
                p
                if out.endswith((".json", ".json.gz"))
                else p / snapshot_filename(snap["captured_at"])
            )
            save_snapshot(snap, path)
            print(
                json.dumps(
                    {
                        "path": str(path),
                        "pages": len(snap["pages"]),
                        "internal_errors": errors,
                        "duration": round(duration, 2),
                    }
                )
            )
            return 0
        if args.cmd == "onpage":
            result = onpage(args.snapshot, args.max_desc)
        else:
            if args.latest:
                paths = list_snapshots(args.latest)
                if len(paths) < 2:
                    print(
                        json.dumps(
                            {
                                "error": "no baseline snapshot to diff against",
                                "snapshots": [str(x) for x in paths],
                            }
                        ),
                        file=sys.stderr,
                    )
                    return 2
                old, new = paths[-2:]
            elif len(args.paths) == 2:
                old, new = args.paths
            else:
                raise ValueError("diff requires OLD NEW or --latest DIR")
            result = diff(old, new, args.wc_pct, args.wc_min)
        write_json(result, args.out)
        return 0
    except (SnapshotError, ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    except Exception as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1 if args.cmd == "crawl" else 2


if __name__ == "__main__":
    sys.exit(main())
