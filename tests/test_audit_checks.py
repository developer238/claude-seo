from __future__ import annotations

import json
from pathlib import Path

from scripts import audit_checks


def test_merge_mapping_sorting_and_zero_counts():
    rows = [
        {
            "check": "onpage",
            "summary": {"themes": {"https": 90}},
            "findings": [
                {
                    "id": "links-no-anchor",
                    "severity": "Low",
                    "title": "Anchor",
                    "count": 2,
                    "pages": ["u1", "u2"],
                },
                {
                    "id": "schema-syntax",
                    "severity": "High",
                    "title": "Schema",
                    "count": 1,
                    "pages": ["u3"],
                },
                {
                    "id": "title-issue",
                    "severity": "Critical",
                    "title": "Title",
                    "scope": "indexable",
                    "count": 3,
                },
                {"id": "links-to-broken", "severity": "Info", "title": "Zero", "count": 0},
            ],
        }
    ]
    merged, themes = audit_checks._finding_rows(rows)
    assert themes == {"https": 90}
    assert merged["counts"] == {"Low": 2, "High": 1, "Critical": 3}
    by_name = {x["name"]: x["findings"] for x in merged["categories"]}
    assert by_name["On-Page SEO"][0]["title"] == "Title (indexable)"
    assert by_name["Schema / Structured Data"][0]["id"] == "schema-syntax"
    assert by_name["On-Page SEO"][1]["id"] == "links-no-anchor"
    assert all(row["id"] != "links-to-broken" for values in by_name.values() for row in values)


def test_recommendation_fallback_and_script_recommendation_precedence():
    merged, _ = audit_checks._finding_rows(
        [
            {
                "findings": [
                    {"id": "links-to-broken", "severity": "High", "count": 2, "title": "Broken"},
                    {
                        "id": "links-to-redirect",
                        "severity": "Medium",
                        "count": 1,
                        "title": "Redirect",
                        "recommendation": "Script advice.",
                    },
                ]
            }
        ]
    )
    findings = merged["categories"][0]["findings"]
    assert findings[0]["recommendation"] == audit_checks.RECOMMENDATIONS["links-to-broken"]
    assert findings[1]["recommendation"] == "Script advice."
    md = audit_checks._markdown(
        "https://e.test", "2026-10-24", 1, None, {}, {}, merged["categories"], []
    )
    assert "**Broken** (2): " + audit_checks.RECOMMENDATIONS["links-to-broken"] in md


def test_pipeline_continues_on_failed_check_and_writes_report(tmp_path, monkeypatch, capsys):
    snap_dir = tmp_path / "snapshots"
    snap_dir.mkdir()
    snap = snap_dir / "snapshot-2026-10-24.json"
    snap.write_text(
        json.dumps(
            {
                "format": "claude-seo-snapshot/1",
                "site": "https://example.com",
                "captured_at": "2026-10-24",
                "pages": {"https://example.com/": {}},
            }
        )
    )
    old = snap_dir / "snapshot-2026-10-23.json"
    old.write_text("{}")
    calls = []

    def fake_run(command):
        calls.append(command)
        name = Path(command[1]).name
        if name == "schema_validate.py":
            return 1, "", "schema failed " + "x" * 400
        if "--out" in command:
            output = Path(command[command.index("--out") + 1])
            if name == "crawl_snapshot.py" and "themes" in command:
                output.write_text(
                    json.dumps(
                        {
                            "check": "themes",
                            "summary": {"themes": {"crawlability": 80}},
                            "findings": [],
                        }
                    )
                )
            elif name == "crawl_snapshot.py" and "onpage" in command:
                output.write_text(
                    json.dumps(
                        {
                            "check": "onpage",
                            "findings": [
                                {
                                    "id": "ai-content-signals",
                                    "severity": "Medium",
                                    "title": "AI signals",
                                    "count": 1,
                                    "pages": ["https://example.com/"],
                                }
                            ],
                        }
                    )
                )
            else:
                output.write_text(json.dumps({"check": name, "findings": []}))
        return 0, "", ""

    monkeypatch.setattr(audit_checks, "run_step", fake_run)
    monkeypatch.setattr(
        "sys.argv",
        [
            "audit_checks.py",
            "https://example.com",
            "--out-dir",
            str(tmp_path),
            "--skip-crawl",
            str(snap),
            "--no-js",
        ],
    )
    assert audit_checks.main() == 0
    data = json.loads((tmp_path / "findings/site-checks.json").read_text())
    report = (tmp_path / "findings/site-checks.md").read_text()
    assert data["baseline"] == str(old)
    assert data["manifest"]["steps"]["schema"]["exit_code"] == 1
    assert len(data["manifest"]["steps"]["schema"]["stderr_tail"]) == 300
    assert "| crawlability | 80% |" in report
    assert "schema: failed" in report
    assert any(
        Path(command[1]).name == "crawl_snapshot.py" and "diff" in command for command in calls
    )
    assert len(calls) >= 5


def test_markdown_first_crawl_baseline_text():
    categories = [
        {
            "name": "Technical SEO",
            "findings": [
                {
                    "severity": "High",
                    "title": "Fix A",
                    "count": 4,
                    "recommendation": "Do this",
                    "evidence_urls": [],
                },
                {
                    "severity": "Medium",
                    "title": "Fix B",
                    "count": 2,
                    "recommendation": "Do this",
                    "evidence_urls": [],
                },
            ],
        },
        {"name": "Empty", "findings": []},
    ]
    md = audit_checks._markdown("https://e.test", "2026-10-24", 3, None, {}, {}, categories, [])
    assert "first crawl, no baseline" in md
    assert "| Theme | Health |" in md
    assert "## Fix first" in md
    assert md.count("Do this") == 1
    assert "## Empty" not in md
    assert "## Not checked" not in md


def test_link_options_pass_through(tmp_path, monkeypatch):
    snap = tmp_path / "snapshot-2026-10-24.json"
    snap.write_text(
        json.dumps({"format": "claude-seo-snapshot/1", "site": "https://example.com", "pages": {}})
    )
    commands = []

    def fake_run(command):
        commands.append(command)
        if "--out" in command:
            Path(command[command.index("--out") + 1]).write_text('{"findings": []}')
        return 0, "", ""

    monkeypatch.setattr(audit_checks, "run_step", fake_run)
    monkeypatch.setattr(
        "sys.argv",
        [
            "audit_checks.py",
            "https://example.com",
            "--out-dir",
            str(tmp_path / "out"),
            "--skip-crawl",
            str(snap),
            "--no-js",
            "--external-limit",
            "22",
            "--timeout",
            "4",
            "--no-retry",
        ],
    )
    audit_checks.main()
    link_command = next(command for command in commands if "link_audit.py" in command[1])
    assert link_command[link_command.index("--external-limit") + 1] == "22"
    assert link_command[link_command.index("--timeout") + 1] == "4.0"
    assert "--no-retry" in link_command
    manifest = json.loads((tmp_path / "out/findings/site-checks.json").read_text())["manifest"]
    assert manifest["steps"]["link_audit"]["elapsed_seconds"] >= 0
