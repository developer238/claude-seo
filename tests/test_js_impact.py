import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "js_impact.py"
spec = importlib.util.spec_from_file_location("js_impact", SCRIPT)
js_impact = importlib.util.module_from_spec(spec)
spec.loader.exec_module(js_impact)


def result(raw, rendered, **extra):
    return {"url": "https://example.com/", "raw_content": raw, "content": rendered,
            "is_spa": False, "error": None, "render_diagnostics": [], **extra}


def run_pair(monkeypatch, raw, rendered, **extra):
    monkeypatch.setattr(js_impact, "render_page", lambda *a, **k: result(raw, rendered, **extra))
    return js_impact.run(["https://example.com/"], site="https://example.com", source={"urls": ["https://example.com/"]})[0]


def ids(output):
    return {f["id"] for f in output["findings"]}


def test_identical_server_and_rendered_html_has_no_findings(monkeypatch):
    html = "<html><head><title>Page</title></head><body><h1>Page</h1><p>Words here</p></body></html>"
    output = run_pair(monkeypatch, html, html)
    assert output["findings"] == []
    assert output["summary"]["verdict"] == "no JavaScript dependence found"


def test_spa_additions_flag_content_links_schema_and_head(monkeypatch):
    raw = '<html><head><title>Shell</title></head><body><div id="root"></div></body></html>'
    words = " ".join("useful" for _ in range(360))
    rendered = ('<html><head><title>Real page</title><meta name="description" content="Desc">'
                '<link rel="canonical" href="https://example.com/new"></head><body><h1>Real page</h1>'
                f'<p>{words}</p><a href="/new">new</a><a href="https://other.test/x">external</a>'
                '<script type="application/ld+json">{"@type":"Article"}</script></body></html>')
    output = run_pair(monkeypatch, raw, rendered)
    assert {"js-content-only", "js-links-only", "js-schema-only", "js-head-differs"} <= ids(output)
    assert output["summary"]["total_js_only_links"] == 2
    assert output["summary"]["finding_page_counts"]["js-content-only"] == 1


def test_render_failure_and_empty_dom_are_reported(monkeypatch):
    output = run_pair(monkeypatch, "<html><body>server</body></html>", "", error="render failed")
    assert "js-render-failed" in ids(output)
    assert output["summary"]["pages_failed"] == 1
