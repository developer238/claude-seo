import requests

from scripts import link_audit


def snapshot():
    root = "https://example.test/"
    pages = {
        root: {
            "url": root,
            "status": 200,
            "content_type": "text/html",
            "title": "root",
            "indexable": True,
            "links": [
                {"target": root + "missing", "internal": True, "text": "bad"},
                {"target": root + "old", "internal": True, "text": "redirect"},
                {
                    "target": root + "nofollow",
                    "internal": True,
                    "nofollow": True,
                    "text": "nofollow",
                },
                {"target": "https://ext.test/blocked", "internal": False},
                {"target": "https://ext.test/999", "internal": False},
                {"target": "https://ext.test/404", "internal": False},
                {"target": "https://ext.test/go", "internal": False},
                {"target": "https://ext.test/timeout", "internal": False},
                {"target": "https://ext.test/go-blocked", "internal": False},
                {"target": "https://ext.test/go-404", "internal": False},
            ],
        },
        root + "noindex": {
            "url": root + "noindex",
            "status": 200,
            "content_type": "text/html",
            "title": "n",
            "indexable": False,
            "links": [
                {"target": root + "old", "internal": True},
                {"target": root + "missing", "internal": True},
                {"target": "https://ext.test/go", "internal": False},
            ],
        },
        root + "missing": {"status": 404, "indexability_reason": "non-200"},
        root + "old": {
            "status": 308,
            "redirect_to": root + "old/",
            "redirect_chain": [{"url": root + "old", "status": 308}],
        },
        root + "nofollow": {
            "url": root + "nofollow",
            "status": 200,
            "content_type": "text/html",
            "title": "f",
            "indexable": True,
            "links": [],
        },
        root + "one": {
            "url": root + "one",
            "status": 200,
            "content_type": "text/html",
            "title": "one",
            "indexable": True,
            "links": [],
        },
        root + "two": {
            "url": root + "two",
            "status": 200,
            "content_type": "text/html",
            "title": "two",
            "indexable": True,
            "links": [],
        },
        root + "orphan": {
            "url": root + "orphan",
            "status": 200,
            "content_type": "text/html",
            "title": "o",
            "indexable": True,
            "links": [],
        },
    }
    pages[root]["links"].extend(
        [
            {"target": root + "one", "internal": True, "text": "one"},
            {"target": root + "two", "internal": True, "nofollow": True, "text": "two"},
        ]
    )
    return {
        "format": "claude-seo-snapshot/1",
        "site": root,
        "captured_at": "2026-10-24T00:00:00Z",
        "pages": pages,
    }


def test_internal_findings_and_scopes():
    result = link_audit.audit(snapshot(), "fixture", no_external=True)
    finds = {(f["id"], f["scope"]): f for f in result["findings"]}
    assert finds[("links-to-broken", "indexable")]["details"][0]["targets"][0]["status"] == 404
    assert finds[("links-to-redirect", "non-indexable")]["count"] == 1
    assert "one-dofollow-inlink" in [f["id"] for f in result["findings"]]
    assert "no-internal-inlinks" in [f["id"] for f in result["findings"]]
    assert result["summary"]["internal"]["pages_checked"] == 6
    assert "internal links only" in finds[("links-to-redirect", "indexable")]["title"]
    assert finds[("links-to-broken", "non-indexable")]["count"] == 1


def test_external_classification(monkeypatch):
    def probe(url, method, ua, timeout):
        class Response:
            def __init__(self, status, headers=None):
                self.status_code, self.headers = status, headers or {}

            def close(self):
                pass

        if url.endswith("/timeout"):
            raise requests.exceptions.Timeout("timeout")
        if url.endswith("/go"):
            return Response(301, {"Location": "https://ext.test/final"})
        if url.endswith("/go-blocked"):
            return Response(301, {"Location": "https://ext.test/blocked-final"})
        if url.endswith("/go-404"):
            return Response(301, {"Location": "https://ext.test/missing-final"})
        if url.endswith("/blocked-final"):
            return Response(403)
        if url.endswith("/missing-final"):
            return Response(404)
        return Response({"blocked": 403, "999": 999, "404": 404}.get(url.rsplit("/", 1)[-1], 200))

    monkeypatch.setattr(link_audit, "_probe", probe)
    result = link_audit.audit(snapshot(), "fixture", timeout=1, per_host_delay=0)
    by_id = {f["id"]: f for f in result["findings"]}
    assert by_id["external-blocked"]["count"] == 2
    assert by_id["external-4xx"]["details"][0]["final_status"] == 404
    assert "external-timeout" in by_id
    assert by_id["external-3xx"]["details"][0]["final_status"] == 200
    assert result["summary"]["external"]["urls_checked"] == 7
    redirects = {
        (f["scope"], d["source"]): d
        for f in result["findings"]
        if f["id"] == "links-to-redirect"
        for d in f["details"]
    }
    assert redirects[("indexable", "https://example.test/")]["targets"][-1]["kind"] == "external"
    counts = result["summary"]["internal"]["findings"]
    assert counts["links_to_redirect_external_pages"]["indexable"] == 1
    assert counts["links_to_redirect_external_pages"]["non-indexable"] == 1
    assert counts["links_to_redirect_internal_pages"]["indexable"] == 1
    assert any(
        d["final_status"] == 403 and d["class"] == "redirect"
        for d in by_id["external-3xx"]["details"]
    )
    assert any(
        d["final_status"] == 404 and d["via_redirect"] for d in by_id["external-4xx"]["details"]
    )
    assert any(
        d["linked_from_count"] == 2
        for f in by_id.values()
        for d in f["details"]
        if "linked_from_count" in d
    )
    assert all(
        "unique_urls" in finding
        for finding in result["findings"]
        if finding["id"].startswith("external-")
    )


def test_repeated_429_is_one_blocked_url(monkeypatch):
    class Response:
        status_code = 429
        headers = {"Retry-After": "60"}

        def close(self):
            pass

    monkeypatch.setattr(link_audit, "_probe", lambda *args: Response())
    root = "https://example.test/"
    pages = {
        root + f"page-{i}": {
            "url": root + f"page-{i}",
            "status": 200,
            "content_type": "text/html",
            "title": "page",
            "indexable": True,
            "links": [{"target": "https://wa.me/example", "internal": False}],
        }
        for i in range(50)
    }
    data = {"format": "claude-seo-snapshot/1", "site": root, "pages": pages}
    result = link_audit.audit(data, "fixture", per_host_delay=0)
    blocked = next(f for f in result["findings"] if f["id"] == "external-blocked")
    assert blocked["count"] == blocked["unique_urls"] == 1
    assert "1 URL, linked from 50 pages" in blocked["title"]
    assert blocked["details"][0]["linked_from_count"] == 50
    assert not any(f["id"] == "external-4xx" for f in result["findings"])
    assert result["summary"]["external"]["classes"]["blocked"]["source_pages"] == 50
    assert result["summary"]["external"]["blocked_urls"] == ["https://wa.me/example"]


def test_external_timeout_retry(monkeypatch):
    calls = []

    class Response:
        status_code = 200
        headers = {}

        def close(self):
            pass

    def probe(url, method, ua, timeout):
        calls.append(timeout)
        if len(calls) == 1:
            raise requests.exceptions.Timeout("slow first attempt")
        return Response()

    monkeypatch.setattr(link_audit, "_probe", probe)
    result = link_audit._external("https://ext.test/slow", 12, 0)
    assert result["class"] == "ok"
    assert result["attempts"] == 2
    assert calls == [12, 24]


def test_external_timeout_after_retry(monkeypatch):
    calls = []

    def probe(url, method, ua, timeout):
        calls.append(timeout)
        raise requests.exceptions.Timeout("still slow")

    monkeypatch.setattr(link_audit, "_probe", probe)
    result = link_audit._external("https://ext.test/slow", 12, 0)
    assert result["class"] == "timeout"
    assert result["attempts"] == 2
    assert calls == [12, 24]


def test_external_redirect_loop(monkeypatch):
    class Response:
        status_code = 302

        def __init__(self, url):
            self.headers = {
                "Location": "https://ext.test/again"
                if url.endswith("/start")
                else "https://ext.test/start"
            }

        def close(self):
            pass

    monkeypatch.setattr(link_audit, "_probe", lambda url, method, ua, timeout: Response(url))
    result = link_audit._external("https://ext.test/start", 12, 0)
    assert result["class"] == "redirect-loop"
    assert result["attempts"] == 1
    assert len(result["hops"]) >= 2
    data = {
        "format": "claude-seo-snapshot/1",
        "site": "https://example.test/",
        "pages": {
            "https://example.test/": {
                "status": 200,
                "content_type": "text/html",
                "title": "home",
                "indexable": True,
                "links": [{"target": "https://ext.test/start", "internal": False}],
            }
        },
    }
    audited = link_audit.audit(data, "fixture", per_host_delay=0)
    loop_finding = next(f for f in audited["findings"] if f["id"] == "external-redirect-loop")
    assert loop_finding["severity"] == "Medium"
    assert loop_finding["details"][0]["hops"]


def test_external_5xx_retry(monkeypatch):
    calls = []

    class Response:
        headers = {}

        def __init__(self, status):
            self.status_code = status

        def close(self):
            pass

    def probe(url, method, ua, timeout):
        calls.append(timeout)
        return Response(500 if timeout == 12 else 200)

    monkeypatch.setattr(link_audit, "_probe", probe)
    monkeypatch.setattr(link_audit.time, "sleep", lambda seconds: None)
    result = link_audit._external("https://ext.test/transient", 12, 0)
    assert result["class"] == "ok"
    assert result["attempts"] == 2
    assert calls == [12, 12, 24]
