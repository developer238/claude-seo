import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

SCRIPT = Path(__file__).parents[1] / "scripts" / "crawl_snapshot.py"
spec = importlib.util.spec_from_file_location("crawl_snapshot", SCRIPT)
cs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cs)


def snapshot(pages, captured="2026-10-24T00:00:00Z", domains=None):
    data = {
        "format": cs.FORMAT,
        "site": "https://example.com",
        "host": "example.com",
        "captured_at": captured,
        "pages": pages,
    }
    if domains is not None:
        data["referring_domains"] = {"domains": domains}
    return data


def page(url, title="T", description="desc", h1=None, words=100, indexable=True, status=200):
    p = {
        "url": url,
        "status": status,
        "content_type": "text/html",
        "title": title,
        "meta_description": description,
        "h1": h1 or ["H"],
        "word_count": words,
        "indexable": indexable,
        "indexability_reason": "ok" if indexable else "noindex-meta",
    }
    return p


def test_extractor_fields_and_indexability():
    html = """<html lang="en"><head><title> T </title><meta name="description" content=" desc ">
    <meta name="robots" content="noindex"><link rel="canonical" href="/other">
    <script type="application/ld+json">{"@type":"Thing"}</script>
    <script type="application/ld+json">{oops}</script></head><body><h1>A</h1>
    <a href="/x" rel="ugc">X</a><a href="mailto:a@b">mail</a></body></html>"""
    result = cs.extract_html(html, "https://example.com/a", "example.com")
    assert result["title"] == "T" and result["h1"] == ["A"]
    assert result["links"] == [
        {"target": "https://example.com/x", "internal": True, "nofollow": True, "text": "X"}
    ]
    assert result["jsonld"] == [{"@type": "Thing"}] and result["jsonld_errors"]
    p = dict(result, url="https://example.com/a", status=200)
    assert cs.indexability(p) == (False, "noindex-meta")
    p["meta_robots"] = None
    assert cs.indexability(p) == (False, "canonical-other")


def test_extractor_additional_metrics_and_no_anchor_accessibility():
    html = """<html><head><meta name="author" content="A"><meta property="article:published_time" content="2026-01-01"></head>
    <body><main><h2>What is this?</h2><h3>More</h3><ul><li>One</li></ul>
    <a href="/empty"><svg></svg></a><a href="/aria" aria-label="Label"></a>
    <a href="/image"><img alt="Photo"></a><a href="/icon"><svg><title>Icon title</title></svg></a>
    <a href="/by"><span>By Jane Doe</span></a></main></body></html>"""
    result = cs.extract_html(
        html, "https://example.com/p", "example.com", html_bytes=1000, response_ms=23
    )
    links = {link["target"].rsplit("/", 1)[-1]: link for link in result["links"]}
    assert links["empty"]["no_anchor"] is True
    assert all("no_anchor" not in links[key] for key in ("aria", "image", "icon", "by"))
    assert (
        result["h2_count"] == 1 and result["question_headings"] == 1 and result["list_count"] == 1
    )
    assert result["html_bytes"] == 1000 and result["response_ms"] == 23
    assert result["has_byline"] and result["has_date"]


def test_validate_robots_content_signal_and_directives():
    valid = """User-agent: *
Allow: /
Disallow: /_md/
Content-Signal: search=yes, ai-input=yes, ai-train=yes

Sitemap: https://saeedlawfirm.com/sitemap.xml
Agentmap: https://saeedlawfirm.com/.well-known/ai-catalog.json"""
    issues = cs.validate_robots(valid)
    assert not [item for item in issues if item["severity"] == "High"]
    assert (
        next(item for item in issues if item["code"] == "unknown-directive")["severity"] == "Info"
    )
    bad = cs.validate_robots(
        "Allow: x\nContent-Signal: ai-robots=maybe\nSitemap: /map.xml\nno colon"
    )
    assert {item["code"] for item in bad} >= {
        "rule-before-user-agent",
        "invalid-rule-value",
        "invalid-content-signal",
        "invalid-sitemap",
        "missing-colon",
    }


def test_onpage_new_findings(tmp_path):
    root = "https://example.com/"
    article = "https://example.com/blog/post"
    pages = {
        root: dict(
            page(root),
            links=[
                {
                    "target": "https://outside.example/x",
                    "internal": False,
                    "nofollow": True,
                    "no_anchor": True,
                },
                {"target": "https://example.com/missing", "internal": True, "nofollow": False},
            ],
        ),
        article: dict(
            page(article, title="Same | Brand", h1=["Same"], words=12),
            h2_count=0,
            jsonld=[],
            question_headings=0,
            list_count=0,
            has_byline=False,
            has_date=False,
            html_bytes=1000,
            text_ratio=0.02,
            indexability_reason="ok",
        ),
        "https://example.com/blocked": {
            "url": "https://example.com/blocked",
            "status": None,
            "indexable": False,
            "indexability_reason": "robots-blocked",
        },
        "https://example.com/noindex": {
            "url": "https://example.com/noindex",
            "status": 200,
            "content_type": "text/html",
            "title": "x",
            "indexable": False,
            "indexability_reason": "noindex-header",
        },
    }
    snap = snapshot(pages)
    snap["robots"] = {
        "url": "https://example.com/robots.txt",
        "status": 200,
        "bytes": 200,
        "text": "Unknown: x",
    }
    path = tmp_path / "new-findings.json"
    cs.save_snapshot(snap, path)
    result = cs.onpage(str(path), 160)
    ids = [item["id"] for item in result["findings"]]
    assert {
        "robots-syntax",
        "links-no-anchor",
        "duplicate-h1-title",
        "low-text-ratio",
        "nofollow-external",
        "pages-blocked",
        "ai-content-signals",
    } <= set(ids)
    assert result["summary"]["counts"]["links-no-anchor"] == 1
    assert result["summary"]["nofollow_external_total"] == 1
    assert result["summary"]["counts"]["pages-blocked-robots"] == 1
    assert result["summary"]["counts"]["pages-blocked-noindex"] == 1


def test_themes_theme_percentages(tmp_path):
    root, child, orphan, broken = (
        "https://example.com/",
        "https://example.com/child",
        "https://example.com/orphan",
        "https://example.com/gone",
    )
    pages = {
        root: dict(
            page(root),
            final_url=root,
            redirect_chain=[],
            html_bytes=100,
            text_ratio=0.2,
            response_ms=200,
            links=[
                {"target": child, "internal": True, "nofollow": False},
                {"target": broken, "internal": True, "nofollow": False},
                {"target": "http://example.com/insecure", "internal": True, "nofollow": False},
            ],
        ),
        child: dict(
            page(child),
            final_url=child,
            redirect_chain=[],
            html_bytes=100,
            text_ratio=0.2,
            response_ms=200,
            links=[],
        ),
        orphan: dict(
            page(orphan),
            final_url=orphan,
            redirect_chain=[],
            html_bytes=100,
            text_ratio=0.2,
            response_ms=200,
            links=[],
        ),
        broken: {
            "url": broken,
            "status": 404,
            "final_url": broken,
            "redirect_chain": [],
            "indexability_reason": "non-200",
        },
    }
    snap_path = tmp_path / "themes.json"
    schema_path = tmp_path / "schema.json"
    cs.save_snapshot(snapshot(pages), snap_path)
    schema_path.write_text(
        json.dumps({"summary": {"items_checked": 4, "items_failed": 1}, "findings": []})
    )
    result = cs.themes(str(snap_path), schema_path=str(schema_path))
    assert result["check"] == "themes"
    assert result["summary"]["themes"]["crawlability"] == 75.0
    assert result["summary"]["themes"]["markup"] == 75.0
    assert result["themes"]["internal_linking"]["percent"] == 0.0
    assert result["themes"]["https"]["percent"] == 75.0
    assert result["themes"]["performance"]["percent"] == 100.0
    assert result["summary"]["overall"][0]["theme"] == "internal_linking"


def test_onpage_findings(tmp_path):
    pages = {
        "https://example.com/a": dict(
            page("https://example.com/a", description="x" * 161), title=""
        ),
        "https://example.com/b": page("https://example.com/b", description="", indexable=False),
    }
    path = tmp_path / "s.json"
    cs.save_snapshot(snapshot(pages), path)
    result = cs.onpage(str(path), 160)
    found = {x["id"]: x for x in result["findings"]}
    assert found["meta-description-too-long"]["count"] == 1
    assert found["meta-description-too-long"]["details"][0]["length"] == 161
    assert "meta-description-missing" not in found
    assert found["title-missing"]["pages"] == ["https://example.com/a"]
    assert result["summary"]["meta_description_missing"] == 0


def test_diff_findings(tmp_path):
    a, b = "https://example.com/a", "https://example.com/b"
    old = snapshot(
        {a: page(a), b: page(b), "https://example.com/gone": page("https://example.com/gone")},
        domains=["lost.com"],
    )
    newpages = {
        a: page(a, "New", "new", ["Changed"], 200, False),
        b: page(b, status=404),
        "https://example.com/new": page("https://example.com/new"),
    }
    new = snapshot(newpages, domains=["new.com"])
    oldp, newp = tmp_path / "old.json", tmp_path / "new.json"
    cs.save_snapshot(old, oldp)
    cs.save_snapshot(new, newp)
    result = cs.diff(str(oldp), str(newp), 10, 50)
    fs = {x["id"]: x for x in result["findings"]}
    for fid in (
        "title-changed",
        "meta-description-changed",
        "h1-changed",
        "word-count-changed",
        "pages-added",
        "indexability-changed",
        "referring-domains-lost",
        "referring-domains-new",
    ):
        assert fid in fs and fs[fid]["count"] == 1
    assert fs["pages-removed"]["count"] == 2
    assert fs["status-changed"]["count"] == 3
    assert "pages-removed" in fs
    assert result["summary"]["counts"]["referring-domains-lost"] == 1
    assert result["summary"]["changed_urls"] == [a, "https://example.com/new"]


def test_crawl_fake_site(monkeypatch, tmp_path):
    site = {
        "https://example.com/robots.txt": (
            200,
            {"Content-Type": "text/plain"},
            b"User-agent: *\nDisallow: /private\n",
        ),
        "https://example.com/": (
            200,
            {"Content-Type": "text/html"},
            b'<a href="/slash">slash</a><a href="/missing">bad</a><a href="http://www.example.com/private">blocked</a>',
        ),
        "https://example.com/slash": (
            308,
            {"Content-Type": "text/html", "Location": "/slash/"},
            b"",
        ),
        "https://example.com/slash/": (200, {"Content-Type": "text/html"}, b"<title>ok</title>"),
        "https://example.com/missing": (404, {"Content-Type": "text/html"}, b""),
    }
    calls = []

    def fake(url, timeout=30, user_agent=None):
        calls.append(url)
        status, headers, body = site.get(url, (404, {"Content-Type": "text/html"}, b""))
        return {"status": status, "headers": headers, "body": body}

    monkeypatch.setattr(cs, "fetch", fake)
    monkeypatch.setattr(cs, "validate_url_strict", lambda url: (url, "203.0.113.1"))
    args = SimpleNamespace(
        no_robots=False,
        timeout=1,
        user_agent="TestBot",
        max_pages=10,
        concurrency=1,
        delay=0,
        refdomains=None,
    )
    snap, errors, _ = cs.crawl_site("https://example.com/", args)
    assert errors == 0
    assert snap["pages"]["https://example.com/slash"]["status"] == 308
    assert snap["pages"]["https://example.com/slash"]["redirect_chain"] == [
        {"url": "https://example.com/slash/", "status": 200}
    ]
    assert snap["pages"]["https://example.com/missing"]["status"] == 404
    assert "http://www.example.com/private" in snap["pages"]
    assert (
        snap["pages"]["http://www.example.com/private"]["indexability_reason"] == "robots-blocked"
    )
    assert "http://www.example.com/private" not in calls


def test_seed_urls_expands_sitemap_index_and_urlset(monkeypatch):
    xml = {
        "https://example.com/robots.txt": (
            200,
            {"Content-Type": "text/plain"},
            b"User-agent: *\nSitemap: https://example.com/index.xml\n",
        ),
        "https://example.com/index.xml": (
            200,
            {"Content-Type": "application/xml"},
            b"<sitemapindex><sitemap><loc>https://example.com/pages.xml</loc></sitemap></sitemapindex>",
        ),
        "https://example.com/pages.xml": (
            200,
            {"Content-Type": "application/xml"},
            b"<urlset><url><loc>https://example.com/a/</loc></url><url><loc>https://example.com/b</loc></url></urlset>",
        ),
    }
    monkeypatch.setattr(
        cs,
        "fetch",
        lambda url, timeout=30, user_agent=None: {
            "status": xml.get(url, (404, {}, b""))[0],
            "headers": xml.get(url, (404, {}, b""))[1],
            "body": xml.get(url, (404, {}, b""))[2],
        },
    )
    args = SimpleNamespace(no_robots=False, timeout=1, user_agent="TestBot", max_pages=10)
    _, seeds, _, _ = cs.seed_urls(
        "https://example.com/", args, "example.com", "https://example.com"
    )
    assert seeds == ["https://example.com/", "https://example.com/a/", "https://example.com/b"]


def test_final_url_resolves_two_hop_redirect(monkeypatch):
    pages = {
        "https://example.com/a": {
            "url": "https://example.com/a",
            "redirect_to": "https://example.com/b",
            "redirect_chain": [],
            "final_url": "https://example.com/a",
        },
        "https://example.com/b": {
            "url": "https://example.com/b",
            "redirect_to": "https://example.com/c",
            "redirect_chain": [],
            "final_url": "https://example.com/b",
        },
        "https://example.com/c": {
            "url": "https://example.com/c",
            "redirect_to": None,
            "redirect_chain": [],
            "final_url": "https://example.com/c",
        },
    }
    cs.add_redirect_chains(pages)
    assert pages["https://example.com/a"]["final_url"] == "https://example.com/c"
    assert pages["https://example.com/b"]["final_url"] == "https://example.com/c"


def test_static_assets_do_not_count_toward_page_limit(monkeypatch):
    assets = ["jpeg", "png", "pdf", "css", "js", "xml", "woff2"]
    asset_links = "".join(
        '<a href="/assets/file-%s.%s">asset</a>' % (i, ext) for i, ext in enumerate(assets)
    )
    asset_links += "".join('<a href="/gallery/nails/%d.jpeg">image</a>' % i for i in range(400))
    site = {
        "https://example.com/": (
            200,
            {"Content-Type": "text/html"},
            (asset_links + '<a href="/two">two</a>').encode(),
        ),
        "https://example.com/two": (
            200,
            {"Content-Type": "text/html"},
            b'<a href="/three">three</a>',
        ),
        "https://example.com/three": (200, {"Content-Type": "text/html"}, b"<title>three</title>"),
    }
    calls = []

    def fake(url, timeout=30, user_agent=None):
        calls.append(url)
        status, headers, body = site.get(url, (404, {"Content-Type": "text/html"}, b""))
        return {"status": status, "headers": headers, "body": body}

    monkeypatch.setattr(cs, "fetch", fake)
    monkeypatch.setattr(cs, "validate_url_strict", lambda url: (url, "203.0.113.1"))
    args = SimpleNamespace(
        no_robots=True,
        timeout=1,
        user_agent="TestBot",
        max_pages=10,
        concurrency=2,
        delay=0,
        refdomains=None,
    )
    snap, errors, _ = cs.crawl_site("https://example.com/", args)
    assert errors == 0
    assert len(snap["pages"]) == 3
    assert set(snap["pages"]) == {
        "https://example.com/",
        "https://example.com/two",
        "https://example.com/three",
    }
    assert not any("/assets/" in url or "/gallery/" in url for url in calls)


def test_crawl_traps_are_not_crawlable():
    assert not cs.is_crawlable_url("https://example.com/search?a=1&b=2&c=3&d=4&e=5&f=6&g=7")
    assert not cs.is_crawlable_url("https://example.com/a/b/a/b/a/b/a")
    assert cs.is_crawlable_url("https://example.com/category/shoes")


def test_fetch_record_decodes_declared_charset(monkeypatch):
    url = "https://example.com/"
    body = "<html><title>Café</title><body>café</body></html>".encode("iso-8859-1")
    monkeypatch.setattr(
        cs,
        "fetch",
        lambda *args, **kwargs: {
            "status": 200,
            "headers": {"Content-Type": "text/html; charset=ISO-8859-1"},
            "body": body,
        },
    )
    args = SimpleNamespace(no_robots=True, timeout=1, user_agent="TestBot", delay=0)
    _, record, _ = cs.fetch_record(url, "example.com", None, args)
    assert record["title"] == "Café"
    assert record["word_count"] == 1


def test_robots_404_allows_but_503_fails_with_json(monkeypatch, capsys):
    monkeypatch.setattr(cs, "validate_url_strict", lambda url: (url, "203.0.113.1"))
    args = SimpleNamespace(
        no_robots=False,
        timeout=1,
        user_agent="TestBot",
        max_pages=2,
        concurrency=1,
        delay=0,
        refdomains=None,
    )
    monkeypatch.setattr(
        cs,
        "fetch",
        lambda url, *args, **kwargs: {
            "status": 404,
            "headers": {},
            "body": b"",
        },
    )
    snap, _, _ = cs.crawl_site("https://example.com/", args)
    assert snap["settings"]["robots_status"] == 404
    capsys.readouterr()

    monkeypatch.setattr(
        cs,
        "fetch",
        lambda url, *args, **kwargs: {"status": 503, "headers": {}, "body": b""},
    )
    monkeypatch.setattr(cs.sys, "argv", ["crawl_snapshot.py", "crawl", "https://example.com/"])
    assert cs.main() == 1
    assert json.loads(capsys.readouterr().err) == {
        "error": "robots.txt unavailable (status or error); retry later or pass --no-robots"
    }


def test_fetch_marks_capped_body_as_truncated(monkeypatch):
    class Response:
        status_code = 200
        headers = {}

        def iter_content(self, chunk_size):
            yield b"x" * (cs.MAX_BODY + 1)

        def close(self):
            pass

    monkeypatch.setattr(cs, "safe_requests_get", lambda *args, **kwargs: Response())
    result = cs.fetch("https://example.com/")
    assert len(result["body"]) == cs.MAX_BODY
    assert result["truncated"] is True
