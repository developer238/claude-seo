#!/usr/bin/env python3
"""Validate page JSON-LD against schema.org and Google rich-result rules.

Usage: schema_validate.py <snapshot.json[.gz] | URL ...> [--html FILE --url URL]
Output: the shared check envelope with grouped findings and per-page counts.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import threading
from urllib.parse import urlparse

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
from snapshot_io import (  # noqa: E402
    SnapshotError,
    envelope,
    finding,
    is_html_page,
    load_snapshot,
    now_iso,
    write_json,
)
from url_safety import safe_requests_get  # noqa: E402

_DATA_DIR = os.path.join(_SCRIPTS_DIR, "..", "data")
_VOCAB = None
_RULES = None
_FETCH_LOCK = threading.Lock()
KEYWORDS = {
    "@context",
    "@id",
    "@type",
    "@graph",
    "@value",
    "@language",
    "@list",
    "@reverse",
    "@index",
    "@vocab",
}
DATATYPES = {
    "Text",
    "URL",
    "Number",
    "Integer",
    "Float",
    "Boolean",
    "Date",
    "DateTime",
    "Time",
    "CssSelectorType",
    "XPathType",
    "PronounceableText",
    "DataType",
}


def short(value):
    value = str(value)
    for prefix in ("https://schema.org/", "http://schema.org/", "schema:"):
        if value.startswith(prefix):
            return value[len(prefix) :]
    return value


def refresh_vocab(source=None):
    if source:
        raw = json.load(open(source, encoding="utf-8"))
    else:
        import requests

        raw = requests.get(
            "https://schema.org/version/latest/schemaorg-current-https.jsonld", timeout=30
        ).json()
    nodes = raw.get("@graph", [])
    types, props, enums = {}, {}, {}
    for n in nodes:
        ident = short(n.get("@id", ""))
        kinds = n.get("@type", [])
        kinds = [kinds] if isinstance(kinds, str) else kinds
        if not ident or ":" in ident:
            continue
        if "rdfs:Class" in kinds:
            parents = n.get("rdfs:subClassOf", [])
            parents = [parents] if not isinstance(parents, list) else parents
            types[ident] = {
                "p": [
                    short(x.get("@id", "")) if isinstance(x, dict) else short(x) for x in parents
                ],
                "d": [],
            }
            if any(
                short(x.get("@id", "")) == "Enumeration" for x in parents if isinstance(x, dict)
            ):
                enums[ident] = []
        elif "rdf:Property" in kinds:
            domains = n.get("schema:domainIncludes", [])
            domains = [domains] if not isinstance(domains, list) else domains
            ranges = n.get("schema:rangeIncludes", [])
            ranges = [ranges] if not isinstance(ranges, list) else ranges
            props[ident] = [
                short(x.get("@id", "")) if isinstance(x, dict) else short(x) for x in ranges
            ]
            for domain in domains:
                name = short(domain.get("@id", "")) if isinstance(domain, dict) else short(domain)
                if name in types:
                    types[name]["d"].append(ident)
    # Property domains can precede their classes in the source graph.
    for n in nodes:
        kinds = n.get("@type", [])
        kinds = [kinds] if isinstance(kinds, str) else kinds
        ident = short(n.get("@id", ""))
        if ident and not ident.startswith("_"):
            for kind in kinds:
                enum_type = short(kind)
                if enum_type in enums and ident not in enums[enum_type]:
                    enums[enum_type].append(ident)
        if "rdf:Property" not in kinds:
            continue
        for domain in (
            n.get("schema:domainIncludes", [])
            if isinstance(n.get("schema:domainIncludes", []), list)
            else [n.get("schema:domainIncludes")]
        ):
            name = short(domain.get("@id", "")) if isinstance(domain, dict) else short(domain)
            if name in types and short(n.get("@id", "")) not in types[name]["d"]:
                types[name]["d"].append(short(n.get("@id", "")))
    version = raw.get("schema:version", raw.get("version"))
    result = {
        "version": version,
        "types": types,
        "props": props,
        "datatypes": sorted(DATATYPES),
        "enums": enums,
    }
    path = os.path.join(_DATA_DIR, "schemaorg-vocab.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, separators=(",", ":"), ensure_ascii=False)
    return result


def get_data(path, kind):
    global _VOCAB, _RULES
    if kind == "vocab":
        if path:
            return json.load(open(path, encoding="utf-8"))
        if _VOCAB is None:
            _VOCAB = json.load(
                open(os.path.join(_DATA_DIR, "schemaorg-vocab.json"), encoding="utf-8")
            )
        return _VOCAB
    if path:
        return json.load(open(path, encoding="utf-8"))
    if _RULES is None:
        _RULES = json.load(
            open(os.path.join(_DATA_DIR, "google-rich-results.json"), encoding="utf-8")
        )
    return _RULES


def schema_context(ctx):
    vals = ctx if isinstance(ctx, list) else [ctx]
    for val in vals:
        if isinstance(val, str) and val.rstrip("/") in ("http://schema.org", "https://schema.org"):
            return True
        if isinstance(val, dict) and str(val.get("@vocab", "")).rstrip("/") in (
            "http://schema.org",
            "https://schema.org",
        ):
            return True
    return False


def nodes_in(obj):
    if isinstance(obj, list):
        for x in obj:
            yield from nodes_in(x)
    elif isinstance(obj, dict):
        if "@type" in obj and set(obj).difference({"@id"}):
            yield obj
        for k, v in obj.items():
            if k in ("@graph",):
                yield from nodes_in(v)
            elif isinstance(v, (dict, list)) and not k.startswith("@") and k != "@context":
                yield from nodes_in(v)


def types_of(node):
    vals = node.get("@type", [])
    vals = vals if isinstance(vals, list) else [vals]
    return [short(x) for x in vals if isinstance(x, str)]


def ancestors(typ, vocab):
    out, todo = [], [typ]
    while todo:
        cur = todo.pop()
        if cur in out:
            continue
        out.append(cur)
        todo.extend(vocab.get("types", {}).get(cur, {}).get("p", []))
    return out


def validate_page(url, blocks, syntax, vocab, rules):
    issues = []
    last_breadcrumb_items = set()
    parent_contexts = {}

    def collect_parents(value, parents=()):
        if isinstance(value, list):
            for child in value:
                collect_parents(child, parents)
        elif isinstance(value, dict):
            node_types = types_of(value)
            if node_types:
                parent_contexts[id(value)] = parents
            for key, child in value.items():
                if key == "@graph":
                    collect_parents(child, parents)
                elif not key.startswith("@"):
                    edges = tuple((typ, key) for typ in node_types) if node_types else parents
                    collect_parents(child, edges)

    collect_parents(blocks)

    def mark_breadcrumbs(value):
        if isinstance(value, dict):
            for key, children in value.items():
                if key == "itemListElement":
                    items = children if isinstance(children, list) else [children]
                    for child in items[:-1]:
                        if isinstance(child, dict) and "ListItem" in types_of(child):
                            last_breadcrumb_items.add(id(child))
                mark_breadcrumbs(children)
        elif isinstance(value, list):
            for child in value:
                mark_breadcrumbs(child)

    mark_breadcrumbs(blocks)

    def add(code, severity, node, path, message, suggestion=None, typ=""):
        issues.append(
            {
                "code": code,
                "severity": severity,
                "url": url,
                "node": typ or ",".join(types_of(node)) if isinstance(node, dict) else "",
                "path": path,
                "message": message,
                "kind": "google"
                if code in ("schema-required-missing", "schema-recommended-missing")
                else "schemaorg",
                **({"suggestion": suggestion} if suggestion else {}),
            }
        )

    for err in syntax or []:
        add("schema-syntax", "High", {}, "jsonld", str(err))
    for bi, block in enumerate(blocks or []):
        if not isinstance(block, dict) or "@context" not in block:
            add(
                "schema-syntax",
                "High",
                {},
                "jsonld[%d].@context" % bi,
                "JSON-LD block is missing @context",
            )
            continue
        if not schema_context(block["@context"]):
            continue
        for ni, node in enumerate(nodes_in(block)):
            ts = types_of(node)
            if not ts:
                continue
            base = "jsonld[%d].node[%d]" % (bi, ni)
            for typ in ts:
                if ":" not in typ and typ not in vocab.get("types", {}):
                    close = difflib.get_close_matches(typ, vocab.get("types", {}), n=1, cutoff=0.65)
                    add(
                        "schema-type-unknown",
                        "High",
                        node,
                        base + ".@type",
                        "Unknown schema.org type: " + typ,
                        "Did you mean %s?" % close[0] if close else None,
                        typ,
                    )
            ancestry = set(
                sum((ancestors(t, vocab) for t in ts if t in vocab.get("types", {})), [])
            )
            props = set(p for t in ancestry for p in vocab.get("types", {}).get(t, {}).get("d", []))
            for prop, value in node.items():
                if prop.startswith("@") or ":" in prop:
                    continue
                if prop not in props:
                    close = difflib.get_close_matches(
                        prop, vocab.get("props", {}), n=1, cutoff=0.65
                    )
                    typ = ts[0]
                    add(
                        "schema-property-invalid",
                        "High",
                        node,
                        base + "." + prop,
                        "%s.%s not defined for %s" % (typ, prop, typ),
                        "Did you mean %s?" % close[0] if close else None,
                        typ,
                    )
                    continue
                ranges = vocab.get("props", {}).get(prop, [])
                vals = value if isinstance(value, list) else [value]
                for val in vals:
                    if isinstance(val, dict) and val.get("@type"):
                        vt = [
                            short(x)
                            for x in (
                                val["@type"] if isinstance(val["@type"], list) else [val["@type"]]
                            )
                        ]
                        if not any(
                            set(ancestors(x, vocab)).intersection(ranges)
                            for x in vt
                            if x in vocab.get("types", {})
                        ) and not set(ranges).intersection({"Thing", "DataType"}):
                            add(
                                "schema-value-type",
                                "Medium",
                                node,
                                base + "." + prop,
                                "%s has type %s, outside range %s"
                                % (prop, ", ".join(vt), ", ".join(ranges)),
                                typ=ts[0],
                            )
                    elif isinstance(val, str):
                        bad = (
                            (
                                ranges
                                and set(ranges).issubset({"Number", "Integer", "Float"})
                                and not re.fullmatch(r"[-+]?\d+(\.\d+)?", val)
                            )
                            or (
                                set(ranges) == {"URL"}
                                and not bool(urlparse(val).scheme and urlparse(val).netloc)
                            )
                            or (
                                set(ranges).intersection({"Date", "DateTime"})
                                and not re.match(r"^\d{4}(?:-\d\d(?:-\d\d)?)?(?:T.*)?$", val)
                            )
                        )
                        if bad:
                            add(
                                "schema-value-type",
                                "Medium",
                                node,
                                base + "." + prop,
                                "Value for %s does not match range %s" % (prop, ", ".join(ranges)),
                                typ=ts[0],
                            )
            applicable = []
            for t in ancestry:
                rule = rules.get(t)
                if not rule or t.startswith("_"):
                    continue
                parents = rule.get("parents")
                if parents and not any(
                    parent in parents or "%s.%s" % (parent, prop) in parents
                    for parent, prop in parent_contexts.get(id(node), ())
                ):
                    continue
                applicable.append((t, rule))
            if applicable:
                specific, rule = max(applicable, key=lambda x: len(ancestors(x[0], vocab)))
                present = set(node)
                for level in ("required", "recommended"):
                    reqs = list(rule.get(level, []))
                    if (
                        specific == "ListItem"
                        and id(node) in last_breadcrumb_items
                        and level == "required"
                    ):
                        reqs.append("item")
                    for req in reqs:
                        choices = req if isinstance(req, list) else [req]
                        if not present.intersection(choices):
                            code = (
                                "schema-required-missing"
                                if level == "required"
                                else "schema-recommended-missing"
                            )
                            add(
                                code,
                                "High" if level == "required" else "Low",
                                node,
                                base,
                                "%s missing %s" % (specific, " or ".join(choices)),
                                typ=specific,
                            )
    return issues


def summarize(pages, errors_only=False):
    grouped, per, by_type = {}, {}, {}
    schema_error_pages, google_error_pages = set(), set()
    for url, issues in pages.items():
        if errors_only:
            issues = [i for i in issues if i["severity"] == "High"]
        err = sum(i["severity"] == "High" for i in issues)
        warn = len(issues) - err
        if err or warn:
            per[url] = {"errors": err, "warnings": warn}
        for i in issues:
            item = grouped.setdefault(i["code"], {"pages": set(), "details": []})
            item["pages"].add(url)
            item["details"].append({k: v for k, v in i.items() if k != "code"})
            if i["severity"] == "High":
                if i["kind"] == "schemaorg":
                    schema_error_pages.add(url)
                else:
                    google_error_pages.add(url)
                for t in filter(None, i["node"].split(",")):
                    by_type[t] = by_type.get(t, 0) + 1
    findings = [
        finding(
            code,
            "High"
            if code != "schema-recommended-missing" and code != "schema-value-type"
            else ("Low" if code == "schema-recommended-missing" else "Medium"),
            code.replace("schema-", "Schema ").replace("-", " "),
            sorted(v["pages"]),
            details=v["details"],
        )
        for code, v in sorted(grouped.items())
    ]
    return findings, {
        "pages_checked": len(pages),
        "pages_with_schemaorg_errors": len(schema_error_pages),
        "pages_with_google_errors": len(google_error_pages),
        "pages_with_warnings": sum(x["warnings"] > 0 for x in per.values()),
        "total_errors": sum(x["errors"] for x in per.values()),
        "total_warnings": sum(x["warnings"] for x in per.values()),
        "by_code": {k: len(v["details"]) for k, v in grouped.items()},
        "by_type": by_type,
        "per_page": per,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("inputs", nargs="*")
    p.add_argument("--html")
    p.add_argument("--url", action="append")
    p.add_argument("--vocab")
    p.add_argument("--rules")
    p.add_argument("--out")
    p.add_argument("--errors-only", action="store_true")
    p.add_argument("--refresh-vocab", action="store_true")
    p.add_argument("--from", dest="source")
    a = p.parse_args()
    try:
        if a.refresh_vocab:
            refresh_vocab(a.source)
            return 0
        vocab = get_data(a.vocab, "vocab")
        rules = get_data(a.rules, "rules")
        pages = {}
        snap = None
        source = {}
        if a.html:
            urls = a.url or []
            if not urls:
                raise ValueError("--html requires --url")
            html = open(a.html, encoding="utf-8").read()
            blocks, syntax = extract_jsonld(html)
            for url in urls:
                pages[url] = validate_page(url, blocks, syntax, vocab, rules)
            source = {"urls": urls}
        elif a.inputs and a.inputs[0].startswith(("http://", "https://")):
            urls = a.inputs + (a.url or [])
            for url in urls:
                with _FETCH_LOCK:
                    response = safe_requests_get(url, timeout=30)
                response.raise_for_status()
                blocks, syntax = extract_jsonld(response.text)
                pages[url] = validate_page(url, blocks, syntax, vocab, rules)
            source = {"urls": urls}
        else:
            if len(a.inputs) != 1:
                raise ValueError("provide one snapshot or one or more URLs")
            snap = load_snapshot(a.inputs[0])
            source = a.inputs[0]
            for url, page in snap["pages"].items():
                if is_html_page(page):
                    pages[url] = validate_page(
                        url, page.get("jsonld", []), page.get("jsonld_errors", []), vocab, rules
                    )
        findings, summary = summarize(pages, a.errors_only)
        result = (
            envelope("schema", snap, source, summary, findings)
            if snap
            else {
                "check": "schema",
                "version": 1,
                "site": None,
                "generated_at": now_iso(),
                "source": source,
                "summary": summary,
                "findings": findings,
            }
        )
        write_json(result, a.out)
        return 0
    except (SnapshotError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


def extract_jsonld(html):
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    out = []
    errors = []
    for index, tag in enumerate(soup.find_all("script", type=re.compile(r"ld\+json", re.I)), 1):
        try:
            out.append(json.loads(tag.string or tag.get_text()))
        except ValueError as exc:
            errors.append("block %d: %s" % (index, exc))
    return out, errors


if __name__ == "__main__":
    sys.exit(main())
