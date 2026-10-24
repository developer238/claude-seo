#!/usr/bin/env python3
"""
IndexNow submitter and read-only site check.

POSTs a list of URLs to the IndexNow open API. Amazon, Bing, Naver,
Seznam.cz, Yandex, and Yep consume the same endpoint. Google does not
participate per the IndexNow FAQ, so this is specifically a non-Google
indexing signal.

Setup
=====
1. Generate a 32+ character host key. Any random string works.
2. Place the key in a file named ``<key>.txt`` at the root of your
   site, served at ``https://example.com/<key>.txt``; the file body
   is the key itself. IndexNow crawls this once to prove host ownership.
3. Pass the key + the key-location URL to this script (or set the
   ``INDEXNOW_KEY`` and ``INDEXNOW_KEY_LOCATION`` env vars).

Usage
=====
::

    python scripts/indexnow_submit.py \\
        --host example.com \\
        --key abcd1234… \\
        --key-location https://example.com/abcd1234.txt \\
        --urls https://example.com/new-post https://example.com/updated-page

    # Or read URLs from a file (one per line):
    python scripts/indexnow_submit.py \\
        --host example.com --key abcd1234… --key-location https://… \\
        --urls-file changed.txt

    # Read-only check, no submission is made:
    python scripts/indexnow_submit.py --check https://example.com --changes changes.json

Successful submissions are appended to --log, INDEXNOW_LOG, or
~/.config/claude-seo/indexnow-log.jsonl. Use --no-log to disable the ledger.

Spec: https://www.indexnow.org/documentation
FAQ: https://www.indexnow.org/faq
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
from snapshot_io import bare_host, finding, normalize_url, now_iso, write_json  # noqa: E402
from url_safety import URLSafetyError, safe_requests_get, validate_url_strict  # noqa: E402

# The umbrella endpoint forwards to every participating engine. Individual
# engine endpoints exist, but api.indexnow.org dispatches automatically.
# One POST covers all six listed IndexNow participants.
INDEXNOW_ENDPOINT = "https://api.indexnow.org/indexnow"
_NETWORK_LOCK = threading.Lock()


def _safe_get(url: str, *, timeout: int):
    with _NETWORK_LOCK:
        return safe_requests_get(url, timeout=timeout)


def _normalized_host(value: str) -> str:
    parsed = urlparse(value if "://" in value else f"//{value}")
    return (parsed.hostname or value).lower().rstrip(".")


def _belongs_to_host(url: str, host: str) -> bool:
    parsed_host = _normalized_host(url)
    declared_host = _normalized_host(host)
    return parsed_host == declared_host or parsed_host.endswith(f".{declared_host}")


def submit(
    host: str,
    key: str,
    key_location: str,
    urls: Iterable[str],
    *,
    timeout: int = 30,
) -> dict:
    """Submit a batch of URLs to IndexNow. Returns a result dict."""
    import requests

    url_list = list(urls)
    if not url_list:
        return {"ok": False, "error": "empty url list", "submitted": 0}
    if len(url_list) > 10000:
        return {"ok": False, "error": "IndexNow spec caps batch at 10000 URLs", "submitted": 0}
    if len(key) < 8 or len(key) > 128:
        return {"ok": False, "error": "key must be 8-128 chars per IndexNow spec", "submitted": 0}

    # Every submitted URL must belong to the declared host. IndexNow rejects
    # cross-host submissions to prevent abuse.
    bad = [u for u in url_list if not _belongs_to_host(u, host)]
    if bad:
        return {
            "ok": False,
            "error": f"{len(bad)} URLs don't belong to host {host!r}",
            "examples": bad[:3],
            "submitted": 0,
        }

    # Validate every submitted URL through url_safety so we don't ship
    # private-IP links to the API by accident.
    for u in url_list:
        try:
            validate_url_strict(u)
        except URLSafetyError as exc:
            return {"ok": False, "error": f"url_safety refused {u!r}: {exc}", "submitted": 0}

    payload = {
        "host": host,
        "key": key,
        "keyLocation": key_location,
        "urlList": url_list,
    }

    try:
        resp = requests.post(
            INDEXNOW_ENDPOINT,
            json=payload,
            timeout=timeout,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
    except requests.exceptions.RequestException as exc:
        return {"ok": False, "error": f"HTTP error: {exc}", "submitted": 0}

    return {
        "ok": resp.status_code in (200, 202),
        "status_code": resp.status_code,
        "submitted": len(url_list),
        "endpoint": INDEXNOW_ENDPOINT,
        "engines": ["Amazon", "Bing", "Naver", "Seznam.cz", "Yandex", "Yep"],
        "response_body_preview": (resp.text or "")[:200],
    }


def verify_key_published(host: str, key: str, key_location: str) -> dict:
    """Fetch the published key file and confirm it matches.

    IndexNow requires the key to be retrievable at the keyLocation URL.
    Missing or wrong content silently rejects every later submission, so
    this pre-flight catches the common ownership-setup mistake.
    """
    try:
        resp = _safe_get(key_location, timeout=15)
    except URLSafetyError as exc:
        return {"ok": False, "error": f"url_safety: {exc}"}
    except Exception as exc:  # noqa: BLE001 (surface every transport error verbatim)
        return {"ok": False, "error": f"fetch failed: {exc}"}
    if resp.status_code != 200:
        return {
            "ok": False,
            "status_code": resp.status_code,
            "error": "keyLocation returned non-200",
        }
    if resp.text.strip() != key:
        return {
            "ok": False,
            "status_code": 200,
            "error": "keyLocation contents do not match key",
            "expected_length": len(key),
            "actual_length": len(resp.text.strip()),
        }
    if host not in key_location:
        return {"ok": False, "error": "keyLocation host does not match declared host"}
    return {"ok": True, "status_code": 200}


def _load_urls(arg_urls: list[str], urls_file: str | None) -> list[str]:
    items: list[str] = list(arg_urls or [])
    if urls_file:
        for line in Path(urls_file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                items.append(line)
    return items


def _ledger_path(arg: str | None, disabled: bool = False) -> Path | None:
    if disabled:
        return None
    return Path(
        arg or os.environ.get("INDEXNOW_LOG") or "~/.config/claude-seo/indexnow-log.jsonl"
    ).expanduser()


def _read_ledger(path: Path | None) -> list[dict]:
    rows = []
    if path and path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    rows.append(row)
            except ValueError:
                continue
    return rows


def _same_url(a: str, b: str) -> bool:
    try:
        x, y = normalize_url(a), normalize_url(b)
        from urllib.parse import urlsplit

        px, py = urlsplit(x), urlsplit(y)
        return (
            px.scheme == py.scheme
            and bare_host(px.hostname or "") == bare_host(py.hostname or "")
            and (px.path.rstrip("/") or "/") == (py.path.rstrip("/") or "/")
            and px.query == py.query
        )
    except Exception:
        return False


def _check(url: str, changes: str | None, key_arg: str | None, ledger_path: Path | None) -> dict:
    from urllib.parse import urljoin, urlsplit

    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("--check requires an http(s) URL")
    host = parsed.hostname
    ledger = _read_ledger(ledger_path)
    host_rows = [
        r for r in ledger if bare_host(_normalized_host(str(r.get("host", "")))) == bare_host(host)
    ]
    key = key_arg or os.environ.get("INDEXNOW_KEY")
    key_file = None
    valid = False
    if key and re.fullmatch(r"[A-Za-z0-9-]{8,128}", key):
        key_file = os.environ.get("INDEXNOW_KEY_LOCATION") or urljoin(url, key + ".txt")
        try:
            resp = _safe_get(key_file, timeout=15)
            valid = resp.status_code == 200 and resp.text.strip() == key
        except Exception:
            pass
    candidates = []
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if not valid:
        for row in reversed(host_rows):
            recorded = row.get("key_file")
            if recorded:
                key_file = str(recorded)
                candidates.append(key_file)
                break
    if key and not key_file:
        key_file = urljoin(origin + "/", key + ".txt")
    if key_file:
        candidates.append(key_file)
    for page in (urljoin(origin + "/", "robots.txt"), urljoin(origin + "/", "")):
        try:
            body = _safe_get(page, timeout=15).text
            candidates.extend(
                urljoin(origin + "/", "/" + m + ".txt")
                for m in re.findall(r"/([A-Za-z0-9-]{8,128})\.txt", body)
            )
        except Exception:
            pass
    candidates.extend(
        urljoin(origin + "/", p)
        for p in ("indexnow.txt", "indexnow-key.txt", ".well-known/indexnow.txt")
    )
    seen = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            if bare_host(urlsplit(candidate).hostname or "") != bare_host(host):
                continue
            resp = _safe_get(candidate, timeout=15)
            body = resp.text.strip()
            if (
                resp.status_code == 200
                and re.fullmatch(r"[A-Za-z0-9-]{8,128}", body)
                and body in candidate
            ):
                key, key_file, valid = body, candidate, True
                break
        except Exception:
            pass
    try:
        probe = _safe_get(INDEXNOW_ENDPOINT, timeout=15)
        endpoint_status = probe.status_code
        reachable = True
    except Exception:
        endpoint_status, reachable = None, False
    summary = {
        "key_found": bool(key),
        "key": (key[:6] + "...") if key else None,
        "key_file_url": key_file,
        "key_file_valid": valid,
        "endpoint_status": endpoint_status,
        "endpoint_reachable": reachable,
        "ledger_path": str(ledger_path) if ledger_path else None,
        "ledger_entries_for_host": len(host_rows),
        "last_submission_ts": max((str(r.get("ts", "")) for r in host_rows), default=None),
    }
    findings = []
    if not key_file:
        findings.append(
            finding(
                "indexnow-no-key",
                "Low",
                "No IndexNow key file found",
                [f"{parsed.scheme}://{parsed.netloc}/"],
                recommendation="Add a key file at the site root and submit changed URLs on publish.",
            )
        )
    if changes:
        data = json.loads(Path(changes).read_text(encoding="utf-8"))
        changed = data.get("summary", {}).get("changed_urls", [])
        old = data.get("source", {}).get("old", {}).get("captured_at")
        new = data.get("source", {}).get("new", {}).get("captured_at")
        details, never, stale = [], 0, 0
        for changed_url in changed:
            matches = [
                (r.get("ts", ""), u)
                for r in ledger
                for u in r.get("urls", [])
                if _same_url(changed_url, u)
            ]
            if not matches:
                never += 1
                details.append({"url": changed_url, "status": "never", "last_submitted": None})
            else:
                last = max((str(m[0]) for m in matches), default="")
                cutoff = old or new or ""
                if cutoff and last < cutoff:
                    stale += 1
                    details.append({"url": changed_url, "status": "stale", "last_submitted": last})
        summary["unsubmitted"] = {"changed": len(changed), "never": never, "stale": stale}
        if details:
            findings.append(
                finding(
                    "indexnow-unsubmitted-changes",
                    "Low",
                    "Changed pages not submitted since the previous crawl",
                    [d["url"] for d in details],
                    recommendation="Submit changed URLs to IndexNow on publish.",
                    details=details,
                )
            )
    return {
        "check": "indexnow",
        "version": 1,
        "site": f"{parsed.scheme}://{parsed.netloc}",
        "generated_at": now_iso(),
        "source": {"url": url},
        "summary": summary,
        "findings": findings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Submit URLs to IndexNow.")
    parser.add_argument(
        "--host",
        help="Site host (without scheme), e.g. example.com",
    )
    parser.add_argument(
        "--key",
        default=os.environ.get("INDEXNOW_KEY"),
        help="IndexNow host key (or set INDEXNOW_KEY).",
    )
    parser.add_argument(
        "--key-location",
        default=os.environ.get("INDEXNOW_KEY_LOCATION"),
        help="Public URL that serves the key file (or set INDEXNOW_KEY_LOCATION).",
    )
    parser.add_argument(
        "--urls",
        nargs="*",
        default=[],
        help="URLs to submit (space-separated).",
    )
    parser.add_argument(
        "--urls-file",
        help="File path with one URL per line (lines starting with # ignored).",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Check that the key file is published correctly; do not submit.",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--check", metavar="URL", help="Read-only IndexNow check, no submission.")
    parser.add_argument("--changes", help="crawl_snapshot.py diff JSON")
    parser.add_argument("--out", help="Write check JSON to this path")
    parser.add_argument("--log", help="Submission ledger path")
    parser.add_argument("--no-log", action="store_true", help="Disable submission ledger")
    args = parser.parse_args()

    if args.check:
        if args.host or args.key_location or args.urls or args.urls_file or args.verify_only:
            print("Error: --check cannot be combined with submission arguments.", file=sys.stderr)
            return 2
        try:
            result = _check(args.check, args.changes, args.key, _ledger_path(args.log, args.no_log))
            write_json(result, args.out)
            return 0
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2

    if not args.host:
        print("Error: --host required for submission.", file=sys.stderr)
        return 2

    if not args.key or not args.key_location:
        print("Error: --key and --key-location required (or set env vars).", file=sys.stderr)
        return 2

    if args.verify_only:
        result = verify_key_published(args.host, args.key, args.key_location)
        if args.json:
            json.dump(result, sys.stdout, indent=2)
            sys.stdout.write("\n")
        else:
            status = "OK" if result["ok"] else "FAIL"
            print(f"Key verification: {status}")
            if "error" in result:
                print(f"  {result['error']}")
        return 0 if result["ok"] else 1

    urls = _load_urls(args.urls, args.urls_file)
    if not urls:
        print("Error: pass at least one URL via --urls or --urls-file.", file=sys.stderr)
        return 2

    # Pre-flight: verify key publication before sending the batch.
    verify = verify_key_published(args.host, args.key, args.key_location)
    if not verify["ok"]:
        print(f"Pre-flight failed: {verify.get('error', 'unknown')}", file=sys.stderr)
        if args.json:
            json.dump({"ok": False, "verify": verify}, sys.stdout, indent=2)
            sys.stdout.write("\n")
        return 1

    result = submit(args.host, args.key, args.key_location, urls)
    if result.get("ok"):
        log_path = _ledger_path(args.log, args.no_log)
        if log_path:
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with log_path.open("a", encoding="utf-8") as fh:
                    fh.write(
                        json.dumps(
                            {
                                "ts": now_iso(),
                                "host": args.host,
                                "urls": urls,
                                "status": result["status_code"],
                                "endpoint": result["endpoint"],
                                "key_hint": args.key[:6],
                                **({"key_file": args.key_location} if args.key_location else {}),
                            }
                        )
                        + "\n"
                    )
            except OSError as exc:
                print(f"Warning: could not write IndexNow ledger: {exc}", file=sys.stderr)
    if args.json:
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        status = "OK" if result["ok"] else "FAIL"
        print(f"IndexNow: {status} (status={result.get('status_code')})")
        print(f"  Submitted: {result['submitted']} URLs to {result['endpoint']}")
        if result.get("engines"):
            print(f"  Engines:   {', '.join(result['engines'])}")
        if not result["ok"]:
            print(f"  Error: {result.get('error', 'see response_body_preview')}")

    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
