import json

import pytest

from scripts.serp_title_check import classify, run


@pytest.mark.parametrize(
    "page,serp,want",
    [
        ("A title", "A title", "exact"),
        ("A title for page", "A title...", "truncated"),
        ("A title | Brand", "A title", "brand-only"),
        ("Long title for all", "Short unrelated", "rewritten"),
        ("A title page", "A novel page", "different"),
    ],
)
def test_classes(page, serp, want):
    threshold = 0.6 if want == "different" else 0.7
    assert classify(page, serp, threshold)[0] == want


@pytest.mark.parametrize("shape", ["raw", "results", "nested", "flat", "pull_row"])
def test_shapes_and_matching(tmp_path, shape):
    snapshot = {
        "format": "claude-seo-snapshot/1",
        "site": "https://example.com",
        "host": "example.com",
        "captured_at": "2026-10-24T00:00:00Z",
        "pages": {
            "https://example.com/a/": {"url": "https://example.com/a/", "title": "Page title"}
        },
    }
    sp = tmp_path / "snap.json"
    sp.write_text(json.dumps(snapshot))
    group = {
        "keyword": "q",
        "datetime": "2026-10-24",
        "items": [
            {
                "type": "organic",
                "url": "https://www.example.com/a",
                "title": "Page title",
                "position": 1,
            }
        ],
    }
    if shape == "raw":
        data = {"tasks": [{"result": [group]}]}
    elif shape == "results":
        data = [group]
    elif shape == "nested":
        data = [[group]]
    elif shape == "pull_row":
        data = {"id": 1, "client": "example", "result": [group]}
    else:
        data = [
            {
                "keyword": "q",
                "url": "https://www.example.com/a",
                "title": "Page title",
                "checked_at": "2026-10-24",
            }
        ]
    if shape == "flat":
        data[0]["items"] = [{"type": "organic", "url": data[0]["url"], "title": data[0]["title"]}]
    fp = tmp_path / "serp.json"
    fp.write_text(json.dumps(data))
    result = run(str(sp), [str(fp)])
    assert result["summary"]["observations_used"] == 1
    assert result["findings"] == []


def test_mismatch_deduplicates_titles_and_tracks_keywords(tmp_path):
    snap = {
        "format": "claude-seo-snapshot/1",
        "site": "https://example.com",
        "host": "example.com",
        "pages": {"https://example.com/a": {"url": "https://example.com/a", "title": "Page title"}},
    }
    sp = tmp_path / "snap.json"
    sp.write_text(json.dumps(snap))
    data = [
        {
            "keyword": q,
            "datetime": "2026-10-24",
            "items": [
                {
                    "type": "organic",
                    "url": "https://example.com/a",
                    "title": "Different result title",
                }
            ],
        }
        for q in ("one", "two")
    ]
    fp = tmp_path / "serp.json"
    fp.write_text(json.dumps(data))
    result = run(str(sp), [str(fp)])
    assert result["summary"]["pages_mismatching"] == 1
    detail = result["findings"][0]["details"]
    assert len(detail) == 1
    assert detail[0]["keywords"] == ["one", "two"]
    assert detail[0]["observations"] == 2


def test_ignores_other_domains_and_ads(tmp_path):
    snap = {
        "format": "claude-seo-snapshot/1",
        "site": "https://example.com",
        "host": "example.com",
        "pages": {},
    }
    sp = tmp_path / "s"
    sp.write_text(json.dumps(snap))
    fp = tmp_path / "d"
    fp.write_text(
        json.dumps(
            [
                {
                    "keyword": "x",
                    "items": [
                        {"type": "organic", "url": "https://elsewhere.test", "title": "x"},
                        {"type": "paid", "url": "https://example.com", "title": "x"},
                    ],
                }
            ]
        )
    )
    with pytest.raises(ValueError, match="no organic items"):
        run(str(sp), [str(fp)])
