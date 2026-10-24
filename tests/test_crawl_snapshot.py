import importlib.util
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
    _, seeds = cs.seed_urls("https://example.com/", args, "example.com", "https://example.com")
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
