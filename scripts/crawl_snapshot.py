#!/usr/bin/env python3
"""Bounded crawler and checks for claude-seo snapshot/1.

Usage: crawl URL [options], onpage SNAPSHOT, diff OLD NEW, or themes SNAPSHOT.
Output: snapshot JSON or a shared check envelope.
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
        started = time.perf_counter()
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
        "response_ms": int((time.perf_counter() - started) * 1000),
    }
    r.close()
    return out


def extract_html(html, page_url, site_host, headers=None, html_bytes=None, response_ms=0):
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
    for node in text_soup.find_all(["script", "style", "noscript"]):
        node.decompose()
    body = text_soup.body or text_soup
    text = " ".join(body.get_text(" ", strip=True).split())
    raw_size = html_bytes if html_bytes is not None else len(html.encode("utf-8"))
    visible_size = len(text.encode("utf-8"))
    heading_texts = [
        " ".join(node.get_text(" ", strip=True).split())
        for node in soup.find_all(["h2", "h3", "h4"])
    ]
    question_count = sum(
        heading.endswith("?")
        or re.match(r"^(who|what|when|where|why|how|can|is|does|do|are|should)\b", heading, re.I)
        is not None
        for heading in heading_texts
    )
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
        link = {
            "target": target,
            "internal": is_internal(target, site_host),
            "nofollow": is_nofollow(a.get("rel")),
            "text": a.get_text(" ", strip=True)[:100],
        }
        labels = [a.get("aria-label", "").strip(), a.get("title", "").strip()]
        labelledby = a.get("aria-labelledby", "").split()
        labels.extend(
            soup.find(id=label_id).get_text(" ", strip=True)
            for label_id in labelledby
            if soup.find(id=label_id)
        )
        labels.extend(img.get("alt", "").strip() for img in a.find_all("img"))
        labels.extend(svg_title.get_text(" ", strip=True) for svg_title in a.select("svg title"))
        if not any(labels) and not link["text"]:
            link["no_anchor"] = True
        links.append(link)
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
    byline = bool(
        soup.find("meta", attrs={"name": re.compile("^author$", re.I)})
        or soup.find(attrs={"rel": lambda rel: rel and "author" in rel})
        or soup.find(attrs={"itemprop": re.compile("^author$", re.I)})
        or soup.select_one(".author, [class*='author'], [class*='byline']")
        or re.search(r"\bBy\s*:?\s+[A-Z][^\n]{1,80}", text, re.I)
    )
    has_date = bool(
        soup.find("time", datetime=True)
        or soup.find(
            "meta", attrs={"property": re.compile(r"^article:(published|modified)_time$", re.I)}
        )
    )

    def contains_date(value):
        if isinstance(value, dict):
            return any(
                key in ("datePublished", "dateModified") or contains_date(item)
                for key, item in value.items()
            )
        if isinstance(value, list):
            return any(contains_date(item) for item in value)
        return False

    has_date = has_date or contains_date(jsonld)
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
        "html_bytes": raw_size,
        "text_ratio": round(visible_size / raw_size, 3) if raw_size else 0.0,
        "response_ms": response_ms,
        "h2_count": len(soup.find_all("h2")),
        "question_headings": question_count,
        "list_count": len(soup.find_all(["ul", "ol", "table"])),
        "has_byline": bool(byline),
        "has_date": has_date,
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
    robots_record = {"url": origin + "/robots.txt", "status": None, "bytes": 0, "text": None}
    if not args.no_robots:
        try:
            rr = fetch(origin + "/robots.txt", args.timeout, args.user_agent)
            robots_status = rr["status"]
            robots_record.update(
                status=robots_status,
                bytes=len(rr["body"]),
                text=rr["body"][: 64 * 1024].decode("utf-8", "replace"),
            )
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
    return robots, list(dict.fromkeys(urls)), robots_status, robots_record


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
        page.update(
            extract_html(
                decode_body(response["body"], content_type),
                url,
                host,
                headers,
                html_bytes=len(response["body"]),
                response_ms=response.get("response_ms", 0),
            )
        )
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


def build_snapshot(start, host, origin, pages, args, robots_status, robots_record):
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
        "robots": robots_record,
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
    robots, seeds, robots_status, robots_record = seed_urls(start, args, host, origin)
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
    snap = build_snapshot(start, host, origin, pages, args, robots_status, robots_record)
    return snap, errors, time.monotonic() - started


def validate_robots(text):
    """Return structured robots.txt syntax and policy diagnostics."""
    known = {
        "user-agent",
        "allow",
        "disallow",
        "sitemap",
        "crawl-delay",
        "host",
        "clean-param",
        "request-rate",
        "visit-time",
        "content-signal",
    }
    issues = []

    def issue(line, raw, code, message, severity):
        issues.append(
            {"line": line, "text": raw, "code": code, "message": message, "severity": severity}
        )

    if text.startswith("\ufeff"):
        issue(1, text.splitlines()[0], "bom", "UTF-8 BOM may cause robots.txt to be ignored", "Low")
    if len(text.encode("utf-8")) > 500 * 1024:
        issue(0, "", "oversized", "File exceeds 500 KiB; Google ignores the excess", "Low")
    active_agent = False
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.lstrip("\ufeff").strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            issue(number, raw, "missing-colon", "Line has no directive separator", "High")
            continue
        directive, value = (part.strip() for part in line.split(":", 1))
        key = directive.lower()
        if key == "noindex":
            issue(
                number,
                raw,
                "noindex-unsupported",
                "noindex is unsupported in robots.txt since 2019",
                "Low",
            )
            continue
        if key not in known:
            issue(
                number,
                raw,
                "unknown-directive",
                "non-standard directive, ignored by Google",
                "Info",
            )
            continue
        if key == "user-agent":
            active_agent = True
        elif key in {"allow", "disallow", "crawl-delay"} and not active_agent:
            issue(
                number, raw, "rule-before-user-agent", "Rule appears before any User-agent", "High"
            )
        if key in {"allow", "disallow"} and value and not value.startswith(("/", "*")):
            issue(
                number,
                raw,
                "invalid-rule-value",
                "Allow/Disallow value should start with / or *",
                "Low",
            )
        if key == "sitemap":
            try:
                sitemap_url = urlsplit(value)
                valid_sitemap = sitemap_url.scheme.lower() in {"http", "https"} and bool(
                    sitemap_url.hostname
                )
                _ = sitemap_url.port
            except ValueError:
                valid_sitemap = False
            if not valid_sitemap or any(char.isspace() for char in value):
                issue(
                    number,
                    raw,
                    "invalid-sitemap",
                    "Sitemap must be an absolute HTTP or HTTPS URL",
                    "High",
                )
        if key == "content-signal":
            seen = set()
            for pair in value.split(","):
                if "=" not in pair:
                    issue(
                        number,
                        raw,
                        "invalid-content-signal",
                        "Content-Signal entries must use key=value",
                        "High",
                    )
                    continue
                signal_key, signal_value = (part.strip().lower() for part in pair.split("=", 1))
                if signal_key in seen:
                    issue(
                        number,
                        raw,
                        "duplicate-content-signal-key",
                        "Duplicate Content-Signal key",
                        "Low",
                    )
                seen.add(signal_key)
                if signal_key not in {"search", "ai-input", "ai-train"} or signal_value not in {
                    "yes",
                    "no",
                }:
                    issue(
                        number,
                        raw,
                        "invalid-content-signal",
                        "Unknown Content-Signal key or value",
                        "High",
                    )
    return issues


def is_url_anchor(text):
    value = (text or "").strip().lower()
    return value.startswith(("http://", "https://", "www."))


def _jsonld_types(blocks):
    """All @type values in JSON-LD blocks, including @graph members."""
    found = set()
    stack = list(blocks)
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, dict):
            kind = node.get("@type")
            found.update(kind if isinstance(kind, list) else [kind])
            if isinstance(node.get("@graph"), list):
                stack.extend(node["@graph"])
    found.discard(None)
    return {str(k) for k in found}


def onpage(path, max_desc, min_text_ratio=0.10):
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
    counts = {
        "meta-description-too-long": len(over),
        "meta-description-missing": len(missing),
        "title-missing": len(titles),
    }
    robots = snap.get("robots", {})
    robots_issues = validate_robots(robots.get("text") or "")
    if robots.get("bytes", 0) > 500 * 1024 and not any(
        x["code"] == "oversized" for x in robots_issues
    ):
        robots_issues.append(
            {
                "line": 0,
                "text": "",
                "code": "oversized",
                "message": "File exceeds 500 KiB; Google ignores the excess",
                "severity": "Low",
            }
        )
    status = robots.get("status")
    if isinstance(status, int) and status >= 500:
        robots_issues.append(
            {
                "line": 0,
                "text": "",
                "code": "robots-5xx",
                "message": "robots.txt returned a server error",
                "severity": "High",
            }
        )
    elif status == 404:
        robots_issues.append(
            {
                "line": 0,
                "text": "",
                "code": "robots-404",
                "message": "No robots.txt",
                "severity": "Info",
            }
        )
    counts["robots-syntax"] = len(robots_issues)
    if robots_issues:
        rank = {"Info": 1, "Low": 2, "High": 3}
        severity = max((item["severity"] for item in robots_issues), key=rank.get)
        fs.append(
            finding(
                "robots-syntax",
                severity,
                "Robots.txt syntax or policy issues",
                [],
                count=len(robots_issues),
                scope="all",
                details=robots_issues,
            )
        )

    no_anchor = {}
    no_anchor_total = 0
    url_anchors = {}
    url_anchor_total = 0
    nofollow_external = {}
    nofollow_external_pages = {}
    nofollow_total = 0
    duplicate_titles = []
    low_ratio = []
    blocked = {"robots": [], "noindex": []}
    ai_content = []
    for url, p in html.items():
        links = p.get("links", [])
        empty_links = [
            {"target": link["target"], "internal": link.get("internal", False)}
            for link in links
            if link.get("no_anchor")
        ]
        if p.get("indexable") and empty_links:
            no_anchor[url] = empty_links
            no_anchor_total += len(empty_links)
        bare_url_links = [
            {"target": link["target"], "text": link.get("text", "")}
            for link in links
            if is_url_anchor(link.get("text"))
        ]
        if bare_url_links:
            url_anchors[url] = bare_url_links
            url_anchor_total += len(bare_url_links)
        external_nofollow = [
            link for link in links if not link.get("internal") and link.get("nofollow")
        ]
        if external_nofollow:
            nofollow_total += len(external_nofollow)
            nofollow_external_pages[url] = [
                {"target": link["target"], "internal": False} for link in external_nofollow
            ]
            for link in external_nofollow:
                target_host = urlsplit(link["target"]).hostname or ""
                nofollow_external[target_host] = nofollow_external.get(target_host, 0) + 1
        h1s = p.get("h1") or []
        title = " ".join((p.get("title") or "").split()).casefold()
        first_h1 = " ".join(h1s[0].split()).casefold() if h1s else ""
        if is_html_page(p) and first_h1 and title == first_h1:
            duplicate_titles.append({"url": url, "title": p.get("title"), "h1": h1s[0]})
        ratio = p.get("text_ratio", 0)
        if ratio < min_text_ratio:
            low_ratio.append({"url": url, "ratio": ratio, "html_bytes": p.get("html_bytes", 0)})
        path = urlsplit(url).path
        is_article = path.startswith(("/blog", "/news")) or bool(
            _jsonld_types(p.get("jsonld", [])) & {"Article", "BlogPosting", "NewsArticle"}
        )
        if p.get("indexable") and path not in {"", "/"}:
            failed = []
            if p.get("word_count", 0) < 300:
                failed.append("thin")
            if p.get("h2_count", 0) < 2:
                failed.append("weak-structure")
            if not p.get("jsonld"):
                failed.append("no-structured-data")
            if not p.get("question_headings", 0) and not p.get("list_count", 0):
                failed.append("no-question-headings-or-lists")
            if is_article and not p.get("has_byline") and not p.get("has_date"):
                failed.append("no-byline-or-date")
            if len(failed) >= 3:
                ai_content.append(
                    {"url": url, "failed": failed, "score": round((5 - len(failed)) / 5 * 100)}
                )
    for url, page in pages.items():
        reason = page.get("indexability_reason")
        if reason == "robots-blocked":
            blocked["robots"].append({"url": url, "reason": reason})
        elif reason in {"noindex-meta", "noindex-header"}:
            blocked["noindex"].append({"url": url, "reason": reason})
    counts.update(
        {
            "links-no-anchor": len(no_anchor),
            "links-url-as-anchor": len(url_anchors),
            "duplicate-h1-title": len(duplicate_titles),
            "low-text-ratio": len(low_ratio),
            "nofollow-external": len(nofollow_external_pages),
            "pages-blocked": len(blocked["robots"]) + len(blocked["noindex"]),
            "pages-blocked-robots": len(blocked["robots"]),
            "pages-blocked-noindex": len(blocked["noindex"]),
            "ai-content-signals": len(ai_content),
        }
    )
    if no_anchor:
        fs.append(
            finding(
                "links-no-anchor",
                "Low",
                "Pages contain links without accessible anchor text",
                sorted(no_anchor),
                count=len(no_anchor),
                details=[{"url": u, "links": no_anchor[u]} for u in sorted(no_anchor)],
            )
        )
    if url_anchors:
        fs.append(
            finding(
                "links-url-as-anchor",
                "Low",
                "Pages use bare URLs as anchor text",
                sorted(url_anchors),
                details=[{"url": url, "links": url_anchors[url]} for url in sorted(url_anchors)],
            )
        )
    if duplicate_titles:
        fs.append(
            finding(
                "duplicate-h1-title",
                "Low",
                "First H1 duplicates the page title",
                [x["url"] for x in duplicate_titles],
                details=duplicate_titles,
            )
        )
    if low_ratio:
        fs.append(
            finding(
                "low-text-ratio",
                "Low",
                "Pages have a low visible text ratio",
                [x["url"] for x in low_ratio],
                details=low_ratio,
            )
        )
    if nofollow_external_pages:
        fs.append(
            finding(
                "nofollow-external",
                "Info",
                "Pages link to external destinations with nofollow",
                sorted(nofollow_external_pages),
                count=len(nofollow_external_pages),
                details=[
                    {"url": url, "links": nofollow_external_pages[url]}
                    for url in sorted(nofollow_external_pages)
                ],
            )
        )
    for scope, details in blocked.items():
        if details:
            fs.append(
                finding(
                    "pages-blocked",
                    "Info",
                    "Pages blocked from indexing: " + scope,
                    [x["url"] for x in details],
                    scope=scope,
                    details=details,
                )
            )
    if ai_content:
        fs.append(
            finding(
                "ai-content-signals",
                "Low",
                "Pages have multiple weak content signals",
                [x["url"] for x in ai_content],
                details=ai_content,
            )
        )
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
            "counts": counts,
            "links_no_anchor_total": no_anchor_total,
            "links_url_as_anchor_total": url_anchor_total,
            "nofollow_external_total": nofollow_total,
            "nofollow_external_hosts": [
                {"host": host, "links": count}
                for host, count in sorted(
                    nofollow_external.items(), key=lambda item: (-item[1], item[0])
                )[:10]
            ],
            "low_text_ratio": len(low_ratio),
            "ai_content_signals": len(ai_content),
        },
        fs,
    )


def _theme_result(items, failures, note=None):
    checked = len(items)
    failed = len(failures)
    return {
        "percent": round((checked - failed) / checked * 100, 1) if checked else None,
        "items_checked": checked,
        "items_failed": failed,
        "failing_items": failures[:20],
        **({"note": note} if note else {}),
    }


def themes(snapshot_path, links_path=None, schema_path=None):
    snap = load_snapshot(snapshot_path)
    pages = snap["pages"]
    host = snap.get("host", "")
    html = {url: p for url, p in pages.items() if is_html_page(p)}

    crawl_items = [url for url in pages if is_internal(url, host)]
    crawl_failures = []
    for url in crawl_items:
        page = pages[url]
        status = page.get("status")
        reasons = []
        if status is None or isinstance(status, int) and status >= 400:
            reasons.append("bad-status")
        if page.get("indexability_reason") == "robots-blocked":
            reasons.append("robots-blocked")
        if len(page.get("redirect_chain", [])) > 1:
            reasons.append("redirect-chain")
        if reasons:
            crawl_failures.append({"url": url, "issues": reasons})

    markup_result = _theme_result([], [])
    if schema_path:
        schema_data = json.loads(Path(schema_path).read_text(encoding="utf-8"))
        summary = schema_data.get("summary", {})
        checked = summary.get("items_checked")
        failed = summary.get("items_failed")
        if isinstance(checked, int) and isinstance(failed, int):
            failing = []
            for item in schema_data.get("findings", []):
                failing.extend(item.get("details", []))
                if len(failing) >= 20:
                    break
            markup_result = {
                "percent": round((checked - failed) / checked * 100, 1) if checked else None,
                "items_checked": checked,
                "items_failed": failed,
                "failing_items": failing[:20],
            }
        else:
            markup_result = {
                "percent": None,
                "items_checked": None,
                "items_failed": None,
                "failing_items": [],
                "note": "Schema input lacks items_checked/items_failed summary.",
            }
    else:
        markup_result = {
            "percent": None,
            "items_checked": None,
            "items_failed": None,
            "failing_items": [],
            "note": "Pass --schema for markup theme data.",
        }

    incoming = {url: [] for url in html}
    broken_sources = {}
    for source, page in html.items():
        for link in page.get("links", []):
            target = link.get("target")
            if not link.get("internal"):
                continue
            target_page = pages.get(target)
            if target in incoming:
                incoming[target].append((source, not link.get("nofollow")))
            status = target_page.get("status") if target_page else None
            issue = (
                "broken-link"
                if status is None or isinstance(status, int) and status >= 400
                else "redirect-link"
                if isinstance(status, int) and 300 <= status < 400
                else None
            )
            if issue:
                broken_sources.setdefault(source, set()).add(issue)
    internal_failures = []
    for url, page in html.items():
        issues = set(broken_sources.get(url, set()))
        inlinks = incoming.get(url, [])
        if not inlinks:
            issues.add("no-inlink")
        elif sum(1 for _, dofollow in inlinks if dofollow) == 1:
            issues.add("one-dofollow-inlink")
        if any(link.get("no_anchor") for link in page.get("links", [])):
            issues.add("links-no-anchor")
        if any(is_url_anchor(link.get("text")) for link in page.get("links", [])):
            issues.add("links-url-as-anchor")
        if issues:
            internal_failures.append({"url": url, "issues": sorted(issues)})

    https_failures = []
    for url in crawl_items:
        page = pages[url]
        issues = []
        if (page.get("final_url") or url).lower().startswith("http://"):
            issues.append("http-final-url")
        if any(
            link.get("internal") and link.get("target", "").lower().startswith("http://")
            for link in page.get("links", [])
        ):
            issues.append("http-internal-link")
        if issues:
            https_failures.append({"url": url, "issues": issues})

    performance_failures = []
    for url, page in html.items():
        issues = []
        if page.get("response_ms", 0) > 1500:
            issues.append("slow-response")
        if page.get("html_bytes", 0) > 1.5 * 1024 * 1024:
            issues.append("large-html")
        if issues:
            performance_failures.append({"url": url, "issues": issues})

    results = {
        "crawlability": _theme_result(crawl_items, crawl_failures),
        "markup": markup_result,
        "internal_linking": _theme_result(list(html), internal_failures),
        "https": _theme_result(crawl_items, https_failures),
        "performance": _theme_result(list(html), performance_failures),
    }
    overall = [
        {
            "theme": name,
            "percent": result["percent"],
            "items_checked": result["items_checked"],
            "items_failed": result["items_failed"],
        }
        for name, result in results.items()
        if result["percent"] is not None
    ]
    overall.sort(key=lambda item: (item["percent"], item["theme"]))
    source = {"snapshot": str(snapshot_path), "captured_at": snap.get("captured_at")}
    if links_path:
        source["links"] = str(links_path)
    if schema_path:
        source["schema"] = str(schema_path)
    return {
        "check": "themes",
        "version": 1,
        "site": snap.get("site"),
        "generated_at": now_iso(),
        "source": source,
        "summary": {
            "themes": {name: result["percent"] for name, result in results.items()},
            "overall": overall,
        },
        "findings": [],
        "themes": results,
    }


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
    o.add_argument("--min-text-ratio", type=float, default=0.10)
    o.add_argument("--out")
    t = sub.add_parser("themes")
    t.add_argument("snapshot")
    t.add_argument("--links")
    t.add_argument("--schema")
    t.add_argument("--out")
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
            result = onpage(args.snapshot, args.max_desc, args.min_text_ratio)
        elif args.cmd == "themes":
            result = themes(args.snapshot, args.links, args.schema)
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
