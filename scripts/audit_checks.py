#!/usr/bin/env python3
"""Run the site-wide snapshot audit pipeline.

Usage: python scripts/audit_checks.py URL --out-dir DIR [pipeline options]
Output: merged findings/site-checks.json and findings/site-checks.md, plus a
compact JSON summary on stdout.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
from snapshot_io import list_snapshots, load_snapshot, now_iso  # noqa: E402

CATEGORIES = [
    "Technical SEO",
    "On-Page SEO",
    "Content Quality",
    "Schema / Structured Data",
    "Performance (CWV)",
]
ONPAGE = (
    "meta-description-",
    "title-",
    "duplicate-h1-title",
    "links-no-anchor",
    "links-url-as-anchor",
    "low-text-ratio",
    "h1-changed",
    "serp-title-mismatch",
    "nofollow-external",
    "meta-description-changed",
    "title-changed",
)
CONTENT = ("word-count-changed", "ai-content-signals", "pages-added")
TECH = (
    "links-to-broken",
    "links-to-redirect",
    "one-dofollow-inlink",
    "no-internal-inlinks",
    "external-",
    "robots-syntax",
    "pages-blocked",
    "status-changed",
    "indexability-changed",
    "pages-removed",
    "js-",
    "indexnow-",
    "referring-domains-",
)
SEVERITY = {name: i for i, name in enumerate(("Critical", "High", "Medium", "Low", "Info"))}


def category_for(fid: str) -> str:
    if fid.startswith("schema-"):
        return "Schema / Structured Data"
    if fid.startswith(ONPAGE):
        return "On-Page SEO"
    if fid.startswith(CONTENT):
        return "Content Quality"
    if fid.startswith(TECH):
        return "Technical SEO"
    return "Technical SEO"


def run_step(command: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(command, cwd=str(SCRIPTS.parent), capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def _read_result(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _finding_rows(results: list[dict]) -> tuple[dict, dict]:
    categories = {name: {"name": name, "findings": []} for name in CATEGORIES}
    counts: dict[str, int] = {}
    themes: dict = {}
    for result in results:
        themes.update(result.get("summary", {}).get("themes", {}))
        for item in result.get("findings", []):
            count = int(item.get("count", 0) or 0)
            if count <= 0:
                continue
            severity = item.get("severity", "Info")
            counts[severity] = counts.get(severity, 0) + count
            pages = item.get("pages", [])
            scope = item.get("scope", "all")
            title = item.get("title", item.get("id", "Finding"))
            if scope != "all":
                title += " (" + str(scope) + ")"
            urls = [str(url) for url in pages[:5]]
            description = str(count) + " pages" + ((": " + ", ".join(urls)) if urls else "")
            categories[category_for(str(item.get("id", "")))]["findings"].append(
                {
                    "title": title,
                    "severity": severity,
                    "description": description,
                    "recommendation": item.get("recommendation", ""),
                    "check": result.get("check"),
                    "id": item.get("id"),
                    "scope": scope,
                    "count": count,
                    "evidence_urls": urls,
                }
            )
    for category in categories.values():
        category["findings"].sort(
            key=lambda row: (SEVERITY.get(row["severity"], 99), -row["count"])
        )
    return {"categories": list(categories.values()), "counts": counts}, themes


def _markdown(
    site: str,
    generated: str,
    page_count: int,
    baseline: str | None,
    themes: dict,
    counts: dict,
    categories: list,
    skipped: list[str],
) -> str:
    lines = [
        "# Site checks",
        "",
        "- Site: " + site,
        "- Date: " + generated,
        "- Pages crawled: " + str(page_count),
        "- Baseline: " + (baseline or "first crawl, no baseline"),
        "",
        "## Themes",
        "",
        "| Theme | Health |",
        "|---|---:|",
    ]
    lines.extend("| " + str(name) + " | " + str(value) + "% |" for name, value in themes.items())
    lines.extend(["", "## Severity counts", "", "| Severity | Count |", "|---|---:|"])
    lines.extend(
        "| " + key + " | " + str(value) + " |"
        for key, value in sorted(counts.items(), key=lambda x: SEVERITY.get(x[0], 99))
    )
    fixes, recommendations = [], set()
    for category in categories:
        for row in category["findings"]:
            recommendation = row["recommendation"]
            if (
                row["severity"] in ("High", "Medium")
                and recommendation
                and recommendation not in recommendations
            ):
                recommendations.add(recommendation)
                fixes.append(row)
    if fixes:
        lines.extend(["", "## Fix first", ""])
        lines.extend(
            "- **" + row["title"] + "** (" + str(row["count"]) + "): " + row["recommendation"]
            for row in fixes[:15]
        )
    for category in categories:
        if not category["findings"]:
            continue
        lines.extend(
            [
                "",
                "## " + category["name"],
                "",
                "| Severity | Finding | Pages | Example URL |",
                "|---|---|---:|---|",
            ]
        )
        for row in category["findings"]:
            url = row["evidence_urls"][0] if row["evidence_urls"] else ""
            lines.append(
                "| "
                + row["severity"]
                + " | "
                + row["title"].replace("|", "\\|")
                + " | "
                + str(row["count"])
                + " | "
                + url
                + " |"
            )
    if skipped:
        lines.extend(["", "## Not checked", ""])
        lines.extend("- " + value for value in skipped)
    return "\n".join(lines) + "\n"


def main() -> int:
    p = argparse.ArgumentParser(description="Run all site-wide SEO checks from one crawl.")
    p.add_argument("url")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--max-pages", type=int, default=500)
    p.add_argument("--delay", type=float, default=0.5)
    p.add_argument("--no-external", action="store_true")
    p.add_argument("--external-limit", type=int)
    p.add_argument("--timeout", type=float)
    p.add_argument("--no-retry", action="store_true")
    js = p.add_mutually_exclusive_group()
    js.add_argument("--js-pages", type=int, default=10)
    js.add_argument("--no-js", action="store_true")
    p.add_argument("--serp", action="append", default=[])
    p.add_argument("--refdomains")
    p.add_argument("--skip-crawl")
    args = p.parse_args()
    out = Path(args.out_dir).resolve()
    snaps, findings_dir = out / "snapshots", out / "findings"
    snaps.mkdir(parents=True, exist_ok=True)
    findings_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, dict] = {}

    def step(name: str, argv: list[str], output: Path | None = None, progress: int = 1) -> int:
        print("step " + str(progress) + "/8 " + name, file=sys.stderr)
        started = time.monotonic()
        code, stdout, stderr = run_step([sys.executable, *argv])
        manifest[name] = {
            "exit_code": code,
            "stderr_tail": stderr[-300:],
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
        if output and code == 0 and not output.exists() and stdout.strip():
            try:
                json.loads(stdout)
                output.write_text(stdout, encoding="utf-8")
            except ValueError:
                pass
        return code

    if args.skip_crawl:
        snapshot = Path(args.skip_crawl).resolve()
    else:
        before = set(list_snapshots(snaps))
        cmd = [
            str(SCRIPTS / "crawl_snapshot.py"),
            "crawl",
            args.url,
            "--max-pages",
            str(args.max_pages),
            "--delay",
            str(args.delay),
            "--out",
            str(snaps),
        ]
        if args.refdomains:
            cmd += ["--refdomains", args.refdomains]
        if step("crawl", cmd, progress=1):
            print(json.dumps({"error": "crawl failed", "manifest": {"steps": manifest}}))
            return 1
        produced = [path for path in list_snapshots(snaps) if path not in before]
        snapshot = (
            produced[-1]
            if produced
            else (list_snapshots(snaps)[-1] if list_snapshots(snaps) else None)
        )
        if snapshot is None:
            print(
                json.dumps({"error": "crawl produced no snapshot", "manifest": {"steps": manifest}})
            )
            return 1

    onpage, links = findings_dir / "onpage.json", findings_dir / "links.json"
    schema, themes_path = findings_dir / "schema.json", findings_dir / "themes.json"
    step(
        "onpage",
        [str(SCRIPTS / "crawl_snapshot.py"), "onpage", str(snapshot), "--out", str(onpage)],
        onpage,
        2,
    )
    link_cmd = [str(SCRIPTS / "link_audit.py"), str(snapshot)] + (
        ["--no-external"] if args.no_external else []
    )
    if args.external_limit is not None:
        link_cmd += ["--external-limit", str(args.external_limit)]
    if args.timeout is not None:
        link_cmd += ["--timeout", str(args.timeout)]
    if args.no_retry:
        link_cmd.append("--no-retry")
    link_cmd += ["--out", str(links)]
    step("link_audit", link_cmd, links, 3)
    step(
        "schema",
        [str(SCRIPTS / "schema_validate.py"), str(snapshot), "--out", str(schema)],
        schema,
        4,
    )
    step(
        "themes",
        [
            str(SCRIPTS / "crawl_snapshot.py"),
            "themes",
            str(snapshot),
            "--links",
            str(links),
            "--schema",
            str(schema),
            "--out",
            str(themes_path),
        ],
        themes_path,
        5,
    )
    if not args.no_js:
        code = step(
            "js_impact",
            [
                str(SCRIPTS / "js_impact.py"),
                str(snapshot),
                "--max-pages",
                str(args.js_pages),
                "--out",
                str(findings_dir / "js-impact.json"),
            ],
            findings_dir / "js-impact.json",
            6,
        )
        if code == 2:
            manifest["js"]["status"] = "skipped: no browser"
    else:
        manifest["js_impact"] = {"status": "skipped: disabled"}
    if args.serp:
        step(
            "serp_title_check",
            [
                str(SCRIPTS / "serp_title_check.py"),
                str(snapshot),
                *sum((["--serp", f] for f in args.serp), []),
                "--out",
                str(findings_dir / "serp-titles.json"),
            ],
            findings_dir / "serp-titles.json",
            7,
        )

    all_snapshots = list_snapshots(snaps)
    baseline = None
    diff_path = findings_dir / "changes.json"
    earlier = [
        path
        for path in all_snapshots
        if path.resolve() != snapshot.resolve() and path.name < snapshot.name
    ]
    if earlier:
        baseline = str(earlier[-1])
        step(
            "diff",
            [
                str(SCRIPTS / "crawl_snapshot.py"),
                "diff",
                baseline,
                str(snapshot),
                "--out",
                str(diff_path),
            ],
            diff_path,
            8,
        )
    else:
        manifest["diff"] = {"status": "first crawl, no baseline"}
    index_cmd = [str(SCRIPTS / "indexnow_submit.py"), "--check", args.url]
    if diff_path.exists():
        index_cmd += ["--changes", str(diff_path)]
    index_cmd += ["--out", str(findings_dir / "indexnow.json")]
    step("indexnow", index_cmd, findings_dir / "indexnow.json", 8)

    try:
        snap_data = load_snapshot(snapshot)
        site = str(snap_data.get("site") or args.url)
        page_count = len(snap_data["pages"])
    except Exception:
        site, page_count = args.url, 0
    results = [
        _read_result(path)
        for path in findings_dir.glob("*.json")
        if path.name not in ("site-checks.json",)
    ]
    merged, themes = _finding_rows(results)
    failed = [
        name + ": failed (exit " + str(item["exit_code"]) + ")"
        for name, item in manifest.items()
        if item.get("exit_code", 0) != 0
    ]
    skipped = failed + [
        name + ": " + str(item["status"]) for name, item in manifest.items() if item.get("status")
    ]
    generated = now_iso()
    payload = {
        "site": site,
        "generated_at": generated,
        "snapshot": str(snapshot),
        "baseline": baseline,
        "themes": themes,
        "counts": merged["counts"],
        "categories": merged["categories"],
        "manifest": {"steps": manifest},
    }
    (findings_dir / "site-checks.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (findings_dir / "site-checks.md").write_text(
        _markdown(
            site,
            generated,
            page_count,
            baseline,
            themes,
            merged["counts"],
            merged["categories"],
            skipped,
        ),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "json": str(findings_dir / "site-checks.json"),
                "markdown": str(findings_dir / "site-checks.md"),
                "counts": merged["counts"],
                "themes": themes,
                "failed_steps": failed,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
