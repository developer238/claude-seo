# Crawl snapshot and audit checks

One crawl feeds every site-wide check below. Each check reads the snapshot
file and writes JSON that `seo-audit` merges into `findings/<category>.md` and
`audit-data.json`. Free data only: no paid API calls.

## Pipeline

```
CS="${CLAUDE_PLUGIN_ROOT}/scripts/claude-seo"
$CS run crawl_snapshot.py crawl <url> --max-pages 500 --out <domain>-audit/snapshots/
$CS run crawl_snapshot.py onpage  <snapshot> --out findings/onpage.json
$CS run crawl_snapshot.py diff --latest <domain>-audit/snapshots/ --out findings/changes.json
$CS run link_audit.py <snapshot> --out findings/links.json
$CS run schema_validate.py <snapshot> --out findings/schema.json
$CS run serp_title_check.py <snapshot> --serp <serp.json> --out findings/serp-titles.json
$CS run indexnow_submit.py --check <url> --changes findings/changes.json --out findings/indexnow.json
```

Re-audit: run `crawl` again into the same folder, then `diff --latest`. The
first crawl has no baseline, so say so in the report instead of inventing a diff.

## Snapshot format `claude-seo-snapshot/1`

File `snapshot-YYYY-MM-DD.json` (`.json.gz` also accepted). Never edit by hand.

```
{
  "format": "claude-seo-snapshot/1",
  "site": "https://example.com",          # start origin
  "host": "example.com",
  "captured_at": "2026-10-24T12:00:00Z",  # UTC ISO
  "tool": {"name": "crawl_snapshot.py", "version": "1"},
  "settings": {"max_pages": 500, "robots": true, "delay": 1.0, "user_agent": "..."},
  "pages": {                               # key = normalized URL
    "https://example.com/about": {
      "url": "...", "status": 200, "final_url": "...",
      "redirect_to": null,                 # Location (absolute) when 3xx
      "redirect_chain": [{"url": "...", "status": 301}],
      "content_type": "text/html",
      "indexable": true,
      "indexability_reason": "ok",         # ok | noindex-meta | noindex-header | canonical-other | non-200 | robots-blocked | non-html
      "canonical": "...", "meta_robots": "...", "x_robots_tag": "...",
      "title": "...", "title_len": 52,
      "meta_description": "...", "meta_description_len": 143,
      "h1": ["..."], "word_count": 812, "content_hash": "sha1",
      "lang": "en",
      "links": [{"target": "https://example.com/x", "internal": true,
                 "nofollow": false, "text": "anchor (<=100 chars)"}],
      "jsonld": [ {...parsed block...} ],  # parsed JSON-LD blocks, raw
      "jsonld_errors": ["block 2: Expecting ','..."]
    }
  },
  "referring_domains": {                   # optional
    "source": "moz|bing|commoncrawl|file", "captured_at": "...", "domains": ["a.com"]
  }
}
```

Rules:
- Static asset URLs are not crawled; seo-images covers images
- Capped response bodies are marked with `truncated: true`.
- Every internal URL discovered (links, sitemap, redirect targets) gets an entry,
  fetched with GET and no redirect-following, so link targets have a real status.
  Non-200 and non-HTML entries carry only the status fields.
- `links` is de-duplicated by target within a page. `nofollow` is true when `rel`
  has `nofollow`, `ugc` or `sponsored`.
- Word count = visible main text (nav, header, footer, script, style removed).
- External links appear in `links` with `internal: false` and are not fetched by the crawl.

## Check output contract (all check scripts)

```
{"check": "<id>", "version": 1, "site": "...", "generated_at": "...",
 "source": {"snapshot": "path", "captured_at": "..."},
 "summary": {...counts...},
 "findings": [{"id": "links-to-redirect", "severity": "Medium",
               "scope": "indexable|non-indexable|all", "count": 6,
               "title": "...", "recommendation": "...",
               "pages": ["url", ...], "details": [ ... ]}]}
```

`severity` is Critical, High, Medium, Low or Info. `count` is pages affected unless
the title says otherwise. `pages` is capped at 200 (`summary` has the full count).
Exit code 0 on success even with findings; 2 on bad input.

## Checks

| Check | Script | Finding ids |
|---|---|---|
| Meta description too long (default over 160 chars) | `crawl_snapshot.py onpage` | `meta-description-too-long` |
| Internal link target status, split by indexable vs non-indexable source page | `link_audit.py` | `links-to-broken`, `links-to-redirect` |
| Only one dofollow incoming internal link, split by indexable vs not | `link_audit.py` | `one-dofollow-inlink` |
| External links by class: 4xx, 3xx, 5xx, timeout, connection error, bot-block | `link_audit.py` | `external-4xx`, `external-3xx`, `external-5xx`, `external-timeout`, `external-error`, `external-blocked` |
| schema.org vocabulary and Google required/recommended properties per page | `schema_validate.py` | `schema-type-unknown`, `schema-property-invalid`, `schema-required-missing`, `schema-recommended-missing`, `schema-syntax` |
| SERP title vs page title using stored SERP data | `serp_title_check.py` | `serp-title-mismatch` |
| Title, meta description, H1, word-count changes; new and lost pages; referring domains | `crawl_snapshot.py diff` | `title-changed`, `meta-description-changed`, `h1-changed`, `word-count-changed`, `pages-added`, `pages-removed`, `status-changed`, `referring-domains-lost`, `referring-domains-new` |
| IndexNow key file, endpoint, changed pages never submitted | `indexnow_submit.py --check` | `indexnow-no-key`, `indexnow-unsubmitted-changes` |

Notes:
- External 403, 429 and 999 are bot-blocks, not broken links. Report them under
  `external-blocked` and never count them as 4xx.
- Word-count change counts only moves of at least 10% and 50 words.
- A SERP title that Google truncated with an ellipsis and still starts with the page
  title is not a mismatch.
- Schema severity: schema.org violations (unknown type, property not valid for the
  type) are errors; missing Google required properties are errors; missing
  recommended properties are warnings. Google's rich result rules change, so check
  the cited doc URL in `${CLAUDE_PLUGIN_ROOT}/data/google-rich-results.json` before quoting a requirement.
- Stored SERP data: any DataForSEO `serp/google/organic` response (raw `tasks[].result[]`,
  a list of result objects, or the `result` column of the `seo.pulls` table) or a flat
  list of `{keyword, url, title, checked_at}`.
- Referring domains: pass `--refdomains FILE` (JSON list or one domain per line) to `crawl`.
  Sources: Moz, Bing Webmaster (see `seo-backlinks`), Common Crawl graph.
