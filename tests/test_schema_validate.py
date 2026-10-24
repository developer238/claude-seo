import json
import sys

from scripts import schema_validate as sv

VOCAB = {
    "types": {
        "Thing": {"p": [], "d": ["name", "url", "quantity", "date", "featured"]},
        "Organization": {"p": ["Thing"], "d": ["url"]},
        "LocalBusiness": {"p": ["Organization"], "d": ["address"]},
        "WebPage": {"p": ["Thing"], "d": ["inLanguage"]},
        "CreativeWork": {"p": ["Thing"], "d": ["inLanguage"]},
        "Event": {"p": ["Thing"], "d": ["inLanguage"]},
        "Article": {"p": ["CreativeWork"], "d": ["headline"]},
        "LegalService": {"p": ["Organization"], "d": []},
        "BreadcrumbList": {"p": ["Thing"], "d": []},
        "BlogPosting": {"p": ["Article"], "d": []},
        "ThingRef": {"p": ["Thing"], "d": []},
        "Product": {"p": ["Thing"], "d": ["offers", "aggregateRating"]},
        "Service": {"p": ["Thing"], "d": ["offers", "aggregateRating"]},
        "Offer": {"p": ["Thing"], "d": ["price"]},
        "AggregateRating": {"p": ["Thing"], "d": []},
    },
    "props": {
        "name": ["Text"],
        "url": ["URL"],
        "address": ["Text"],
        "inLanguage": ["Text"],
        "headline": ["Text"],
        "quantity": ["Number"],
        "date": ["Date"],
        "featured": ["Article"],
        "offers": ["Offer"],
        "aggregateRating": ["AggregateRating"],
        "price": ["Number"],
    },
}
RULES = {
    "Organization": {"required": [], "recommended": ["url"]},
    "LocalBusiness": {"required": ["name", "address"], "recommended": []},
    "Article": {"recommended": ["headline"]},
    "BlogPosting": {"required": ["name"]},
}


def validate(blocks, vocab=VOCAB, rules=RULES, syntax=None):
    return sv.validate_page("https://example.test/", blocks, syntax or [], vocab, rules)


def test_context_graph_array_types_properties_and_domain():
    issues = validate(
        [
            {
                "@context": "https://schema.org/",
                "@graph": [
                    {
                        "@type": ["schema:Organization", "https://schema.org/LocalBusiness"],
                        "name": "A",
                        "inLanguage": "en",
                    },
                    {"@type": "WebPage", "inLanguage": "en"},
                    {"@type": "Organization", "badName": "x"},
                ],
            }
        ]
    )
    codes = [x["code"] for x in issues]
    assert "schema-property-invalid" in codes
    bad = next(x for x in issues if x["code"] == "schema-property-invalid")
    assert bad["message"] == "Organization.inLanguage not defined for Organization"
    assert sum(x["code"] == "schema-required-missing" for x in issues) == 1


def test_inlanguage_domain_errors_passes_and_item_counts():
    blocks = [
        {
            "@context": "https://schema.org",
            "@graph": [
                {"@type": ["LegalService", "Organization"], "inLanguage": "en"},
                {"@type": "BreadcrumbList", "inLanguage": "en"},
                {"@type": "WebPage", "inLanguage": "en"},
                {"@type": "Article", "inLanguage": "en"},
            ],
        }
    ]
    issues = validate(blocks, rules={})
    invalid = [x for x in issues if x["code"] == "schema-property-invalid"]
    assert [x["message"] for x in invalid] == [
        "LegalService.inLanguage not defined for LegalService",
        "BreadcrumbList.inLanguage not defined for BreadcrumbList",
    ]
    assert {"CreativeWork", "Event"}.issubset(invalid[0]["allowed_on"])
    assert invalid[0]["fix"] == (
        "remove the property from LegalService or move it to a WebPage/CreativeWork node."
    )
    _, summary = sv.summarize(
        {"https://example.test/": issues},
        items_by_page={"https://example.test/": sv.schema_items(blocks)},
    )
    assert (summary["items_checked"], summary["items_failed"], summary["items_passed"]) == (
        5,
        3,
        2,
    )


def test_missing_context_unknown_type_prefixed_type_and_references():
    issues = validate(
        [
            {"@type": "Organization", "name": "missing context"},
            {"@context": "https://schema.org", "@type": "Organizaton", "name": "x"},
            {"@context": "https://schema.org", "@type": "other:Thing", "name": "x"},
            {"@context": "https://example.org", "@type": "Nope", "name": "x"},
            {"@context": "https://schema.org", "@graph": [{"@id": "#ref"}]},
        ]
    )
    assert sum(x["code"] == "schema-syntax" for x in issues) == 1
    assert sum(x["code"] == "schema-type-unknown" for x in issues) == 1
    assert next(x for x in issues if x["code"] == "schema-type-unknown").get("suggestion")


def test_google_any_of_and_most_specific_rule():
    rules = {
        "Organization": {"required": ["url"]},
        "LocalBusiness": {"required": [["offer", "review"]]},
    }
    issues = validate(
        [{"@context": "https://schema.org", "@type": "LocalBusiness", "name": "x"}], rules=rules
    )
    missing = [x["message"] for x in issues if x["code"] == "schema-required-missing"]
    assert missing == ["LocalBusiness missing offer or review"]


def test_context_restricted_google_rules_and_summary_kinds():
    rules = {
        "Offer": {"parents": ["Product.offers"], "required": ["price"]},
        "AggregateRating": {"parents": ["Product", "LocalBusiness"], "required": ["ratingValue"]},
    }
    blocks = [
        {
            "@context": "https://schema.org",
            "@graph": [
                {"@type": "Offer"},
                {
                    "@type": "Service",
                    "offers": {"@type": "Offer"},
                    "aggregateRating": {"@type": "AggregateRating"},
                },
                {
                    "@type": "Product",
                    "offers": {"@type": "Offer"},
                    "aggregateRating": {"@type": "AggregateRating"},
                },
            ],
        }
    ]
    issues = validate(blocks, rules=rules)
    assert len([x for x in issues if x["code"] == "schema-required-missing"]) == 2
    assert all(x["kind"] == "google" and x["severity"] == "High" for x in issues)
    findings, summary = sv.summarize({"https://example.test/": issues})
    assert summary["pages_with_google_errors"] == 1
    assert summary["pages_with_schemaorg_errors"] == 0
    assert all(
        "kind" in detail and "severity" in detail for f in findings for detail in f["details"]
    )


def test_value_warnings_and_schema_syntax_errors():
    issues = validate(
        [
            {
                "@context": {"@vocab": "http://schema.org/"},
                "@type": "Thing",
                "url": "relative",
                "quantity": "many",
                "date": "next week",
                "featured": {"@type": "Organization", "name": "x"},
            }
        ],
        rules={},
        syntax=["broken block"],
    )
    assert sum(x["code"] == "schema-value-type" for x in issues) == 4
    assert any(x["code"] == "schema-syntax" for x in issues)


def test_url_mode_fetch_is_monkeypatched(monkeypatch, tmp_path, capsys):
    class Response:
        status_code = 200
        text = '<script type="application/ld+json">{"@context":"https://schema.org","@type":"Organization","name":"Acme"}</script>'

        def raise_for_status(self):
            pass

    monkeypatch.setattr(sv, "safe_requests_get", lambda *a, **kw: Response())
    monkeypatch.setattr(sv, "get_data", lambda path, kind: VOCAB if kind == "vocab" else {})
    monkeypatch.setattr(sys, "argv", ["schema_validate.py", "https://example.test/"])
    assert sv.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["source"] == {"urls": ["https://example.test/"]}
    assert result["summary"]["pages_checked"] == 1
