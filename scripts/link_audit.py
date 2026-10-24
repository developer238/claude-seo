#!/usr/bin/env python3
"""Audit internal and external links from a crawl snapshot.

Usage: python scripts/link_audit.py SNAPSHOT [--no-external] [--out FILE]
Output: check envelope JSON with internal and external link findings.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from collections import defaultdict
from urllib.parse import urljoin, urlsplit

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
from snapshot_io import (  # noqa: E402
    SnapshotError,
    envelope,
    finding,
    is_html_page,
    load_snapshot,
    normalize_url,
    write_json,
)  # noqa: E402
from url_safety import URLSafetyError, safe_requests_get, safe_requests_head, validate_url_strict  # noqa: E402

_UA = "Mozilla/5.0 (compatible; claude-seo-link-audit/1.0)"
_HOST_LOCK = threading.Lock()
_HOST_LAST = {}
_NET_LOCK = threading.Lock()


def _probe(url, method, ua, timeout):
    # url_safety pins DNS with a process-wide patch and refuses concurrent use.
    with _NET_LOCK:
        validate_url_strict(url)
        fetch = safe_requests_head if method == "HEAD" else safe_requests_get
        return fetch(
            url,
            timeout=timeout,
            allow_redirects=False,
            stream=(method == "GET"),
            headers={"User-Agent": ua},
        )


def _polite_probe(url, method, ua, timeout, delay):
    host = (urlsplit(url).hostname or "").lower()
    with _HOST_LOCK:
        wait = delay - (time.monotonic() - _HOST_LAST.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        _HOST_LAST[host] = time.monotonic()
    return _probe(url, method, ua, timeout)


def _follow_external(url, timeout, delay):
    chain, current = [], url
    for hop in range(6):
        response = _polite_probe(current, "HEAD", _UA, timeout, delay)
        if response.status_code >= 400 or response.status_code == 405:
            response.close()
            response = _polite_probe(current, "GET", _UA, timeout, delay)
        status, headers = response.status_code, dict(response.headers)
        response.close()
        location = headers.get("Location")
        if status not in (301, 302, 303, 307, 308) or not location:
            return chain, current, status, headers, None
        chain.append({"url": current, "status": status})
        current = urljoin(current, location)
        if hop == 5:
            return chain, current, status, headers, "too many redirects"


def _classify_external(status, headers, chain, error):
    if error:
        return "error"
    blocked = status in (401, 403, 429, 999) or (
        status in (503, 520, 521, 522, 523, 524, 525, 526, 527, 528, 529)
        and any(token in str(headers).lower() for token in ("cloudflare", "cf-mitigated"))
    )
    if blocked:
        return "redirect" if chain else "blocked"
    if status >= 500:
        return "5xx"
    if status >= 400:
        return "4xx"
    return "redirect" if chain else "ok"


def _external(url, timeout, delay):
    import requests

    chain, current, error, headers = [], url, None, {}
    try:
        chain, current, status, headers, error = _follow_external(url, timeout, delay)
        cls = _classify_external(status, headers, chain, error)
    except URLSafetyError as exc:
        cls, status, error = "error", None, str(exc)
    except requests.exceptions.Timeout as exc:
        cls, status, error = "timeout", None, str(exc)
    except requests.exceptions.RequestException as exc:
        cls, status, error = "error", None, str(exc)
    except Exception as exc:
        cls, status, error = "error", None, str(exc)
    return {
        "class": cls,
        "status": chain[0]["status"] if chain else status,
        "final_url": current,
        "final_status": status,
        "hops": chain,
        "via_redirect": bool(chain),
        "error": error,
    }


def audit(
    snapshot,
    snapshot_path,
    *,
    no_external=False,
    external_limit=500,
    timeout=12,
    concurrency=8,
    per_host_delay=0.5,
):
    pages = snapshot["pages"]
    sources = [(u, p) for u, p in pages.items() if is_html_page(p)]
    findings, broken = [], {"indexable": [], "non-indexable": []}
    redirects = {"indexable": [], "non-indexable": []}
    inbound = defaultdict(set)
    inbound_anchor = defaultdict(dict)
    external_refs = defaultdict(dict)
    links_checked = 0
    for source, page in sources:
        scope = "indexable" if page.get("indexable") else "non-indexable"
        for link in page.get("links", []):
            target = link.get("target", "")
            if link.get("internal"):
                links_checked += 1
                if normalize_url(target) == normalize_url(source):
                    continue
                dest = pages.get(normalize_url(target)) or pages.get(target)
                if not dest or dest.get("indexability_reason") == "robots-blocked":
                    continue
                status = dest.get("status")
                if status is None or (isinstance(status, int) and (status >= 400 or status < 200)):
                    broken[scope].append(
                        (
                            source,
                            {
                                "target": target,
                                "status": status,
                                "redirect_to": dest.get("redirect_to"),
                                "anchor": link.get("text", ""),
                                "kind": "internal",
                            },
                        )
                    )
                elif isinstance(status, int) and 300 <= status < 400:
                    redirects[scope].append(
                        (
                            source,
                            {
                                "target": target,
                                "status": status,
                                "redirect_to": dest.get("redirect_to"),
                                "anchor": link.get("text", ""),
                                "kind": "internal",
                            },
                        )
                    )
                if not link.get("nofollow") and dest and is_html_page(dest):
                    inbound[normalize_url(target)].add(source)
                    inbound_anchor[normalize_url(target)].setdefault(source, link.get("text", ""))
            elif not no_external and urlsplit(target).scheme.lower() in ("http", "https"):
                external_refs[normalize_url(target)][source] = link.get("text", "")
    for fid, data, severities, title in [
        ("links-to-broken", broken, ("High", "Medium"), "Internal links point to broken pages")
    ]:
        for scope, index in (("indexable", 0), ("non-indexable", 1)):
            grouped = defaultdict(list)
            for source, target in data[scope]:
                grouped[source].append(target)
            if grouped:
                details = [{"source": s, "targets": targets[:20]} for s, targets in grouped.items()]
                findings.append(
                    finding(
                        fid,
                        severities[index],
                        title,
                        [d["source"] for d in details],
                        scope=scope,
                        recommendation="Update the href to the final URL.",
                        details=details,
                    )
                )
    start = normalize_url(snapshot.get("site", ""))
    incoming_by_scope = {"indexable": [], "non-indexable": []}
    orphans = []
    for url, page in sources:
        key = normalize_url(url)
        count = len(inbound[key] - {key})
        if key != start and count == 0 and page.get("indexable"):
            orphans.append(url)
        if key != start and count == 1:
            scope = "indexable" if page.get("indexable") else "non-indexable"
            source = next(iter(inbound[key]))
            incoming_by_scope[scope].append(
                {"url": url, "linked_from": [source], "anchor": inbound_anchor[key].get(source, "")}
            )
    for scope, details in incoming_by_scope.items():
        if details:
            findings.append(
                finding(
                    "one-dofollow-inlink",
                    "Low",
                    "Pages have exactly one dofollow internal inlink",
                    [d["url"] for d in details],
                    scope=scope,
                    details=details,
                )
            )
    if orphans:
        findings.append(
            finding(
                "no-internal-inlinks",
                "Low",
                "Indexable pages have no dofollow internal inlinks",
                orphans,
                details=[{"url": u} for u in orphans],
            )
        )
    external_summary = {}
    if not no_external:
        urls = list(external_refs)
        truncated = len(urls) > external_limit
        urls = urls[:external_limit]
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            results = list(pool.map(lambda u: (u, _external(u, timeout, per_host_delay)), urls))
        by_class = defaultdict(list)
        for url, result in results:
            linked_from = sorted(external_refs[url])
            result.update(
                {"url": url, "linked_from": linked_from[:10], "linked_from_count": len(linked_from)}
            )
            by_class[result["class"]].append(result)
            if result["class"] == "redirect":
                for source in external_refs[url]:
                    page = pages[source]
                    scope = "indexable" if page.get("indexable") else "non-indexable"
                    redirects[scope].append(
                        (
                            source,
                            {
                                "target": url,
                                "status": result["status"],
                                "redirect_to": result["final_url"],
                                "anchor": external_refs[url][source],
                                "kind": "external",
                            },
                        )
                    )
        ext_ids = {
            "4xx": ("external-4xx", "Medium", "External links return 4xx responses"),
            "redirect": ("external-3xx", "Low", "External links redirect"),
            "5xx": ("external-5xx", "Low", "External links return 5xx responses"),
            "timeout": ("external-timeout", "Low", "External links time out"),
            "error": ("external-error", "Low", "External links have connection errors"),
            "blocked": (
                "external-blocked",
                "Info",
                "External links are blocked for bots, not necessarily broken; confirm manually",
            ),
        }
        for cls, (fid, severity, title) in ext_ids.items():
            if by_class[cls]:
                details = by_class[cls]
                pages_found = sorted({p for d in details for p in external_refs[d["url"]]})
                unique_urls = len(details)
                if cls == "blocked":
                    title = (
                        f"External URLs blocked for bots, not necessarily broken: "
                        f"{unique_urls} URL{'s' if unique_urls != 1 else ''}, "
                        f"linked from {len(pages_found)} pages; confirm manually"
                    )
                findings.append(
                    finding(
                        fid,
                        severity,
                        title,
                        pages_found,
                        count=unique_urls if cls == "blocked" else len(pages_found),
                        details=details,
                    )
                )
                findings[-1]["unique_urls"] = unique_urls
        external_summary = {
            "classes": {
                k: {
                    "urls": len(v),
                    "source_pages": len({p for d in v for p in external_refs[d["url"]]}),
                }
                for k, v in by_class.items()
            },
            "urls_checked": len(results),
            "truncated": truncated,
            "blocked_urls": [d["url"] for d in by_class["blocked"][:30]],
        }
    for scope, index in (("indexable", 0), ("non-indexable", 1)):
        grouped = defaultdict(list)
        internal_pages, external_pages = set(), set()
        for source, target in redirects[scope]:
            grouped[source].append(target)
            (internal_pages if target["kind"] == "internal" else external_pages).add(source)
        if grouped:
            details = [
                {"source": source, "targets": targets[:20]} for source, targets in grouped.items()
            ]
            findings.append(
                finding(
                    "links-to-redirect",
                    ("Medium", "Low")[index],
                    "Links point to redirects" + (" (internal links only)" if no_external else ""),
                    list(grouped),
                    scope=scope,
                    recommendation="Update the href to the final URL.",
                    details=details,
                )
            )
    internal_counts = defaultdict(lambda: defaultdict(int))
    for item in findings:
        if item["id"].startswith("links-to-") or item["id"] in (
            "one-dofollow-inlink",
            "no-internal-inlinks",
        ):
            internal_counts[item["id"]][item["scope"]] = item["count"]
    internal_pages = {}
    external_pages = {}
    for scope in ("indexable", "non-indexable"):
        internal_pages[scope] = len(
            {source for source, target in redirects[scope] if target["kind"] == "internal"}
        )
        external_pages[scope] = len(
            {source for source, target in redirects[scope] if target["kind"] == "external"}
        )
        internal_counts["links-to-redirect"][scope] = len(
            {source for source, _ in redirects[scope]}
        )
    internal_counts["links_to_redirect_internal_pages"] = internal_pages
    internal_counts["links_to_redirect_external_pages"] = external_pages
    summary = {
        "internal": {
            "findings": dict(internal_counts),
            "pages_checked": len(sources),
            "links_checked": links_checked,
        },
        "external": external_summary,
    }
    return envelope("links", snapshot, snapshot_path, summary, findings)


def main():
    parser = argparse.ArgumentParser(description="Audit links in a crawl snapshot")
    parser.add_argument("snapshot")
    parser.add_argument("--no-external", action="store_true")
    parser.add_argument("--external-limit", type=int, default=500)
    parser.add_argument("--timeout", type=float, default=12)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--per-host-delay", type=float, default=0.5)
    parser.add_argument("--out")
    args = parser.parse_args()
    try:
        if (
            args.external_limit < 0
            or args.timeout <= 0
            or args.concurrency < 1
            or args.per_host_delay < 0
        ):
            raise SnapshotError("limits must be non-negative and timeout/concurrency positive")
        snapshot = load_snapshot(args.snapshot)
        write_json(
            audit(
                snapshot,
                args.snapshot,
                no_external=args.no_external,
                external_limit=args.external_limit,
                timeout=args.timeout,
                concurrency=args.concurrency,
                per_host_delay=args.per_host_delay,
            ),
            args.out,
        )
    except (SnapshotError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
