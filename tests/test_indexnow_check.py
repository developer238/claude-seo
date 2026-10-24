import json

from scripts import indexnow_submit


def test_read_only_check(monkeypatch, tmp_path):
    class R:
        status_code = 404
        text = ""

    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return R()

    monkeypatch.setattr(indexnow_submit, "safe_requests_get", get)
    result = indexnow_submit._check("https://example.com/", None, None, tmp_path / "ledger")
    assert result["check"] == "indexnow"
    assert result["summary"]["endpoint_reachable"] is True
    assert "indexnow-no-key" in [x["id"] for x in result["findings"]]
    no_key = result["findings"][0]
    assert no_key["count"] == 1
    assert no_key["pages"] == ["https://example.com/"]
    assert not any("?" in c for c in calls)


def test_submit_ledger_on_success(monkeypatch, tmp_path):
    class Response:
        ok = True
        status_code = 200
        text = ""

    monkeypatch.setattr(indexnow_submit, "verify_key_published", lambda *a: {"ok": True})
    monkeypatch.setattr(
        indexnow_submit,
        "submit",
        lambda *a: {
            "ok": True,
            "status_code": 200,
            "submitted": 1,
            "endpoint": "https://api.indexnow.org/indexnow",
            "engines": [],
        },
    )
    monkeypatch.setattr(indexnow_submit, "_load_urls", lambda *a: ["https://example.com/a"])
    monkeypatch.setattr(
        "sys.argv",
        [
            "x",
            "--host",
            "example.com",
            "--key",
            "abcdefgh",
            "--key-location",
            "https://example.com/abcdefgh.txt",
            "--urls",
            "https://example.com/a",
            "--log",
            str(tmp_path / "x.jsonl"),
        ],
    )
    assert indexnow_submit.main() == 0
    row = json.loads((tmp_path / "x.jsonl").read_text())
    assert row["status"] == 200
    assert row["key_hint"] == "abcdef"
    assert row["key_file"] == "https://example.com/abcdefgh.txt"
    assert "key" not in row
