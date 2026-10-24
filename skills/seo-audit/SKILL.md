---
name: seo-audit
description: "Run a full-site SEO audit and return a scored, prioritized report. Use only for site-wide checks; use seo-page for one URL or seo-technical for a technical-only review."
user-invocable: true
argument-hint: "[url]"
license: MIT
metadata:
  author: AgriciDaniel
  version: "2.4.2"
  category: seo
---

# Full Website SEO Audit

## Process

1. **Render homepage**: use `"${CLAUDE_PLUGIN_ROOT}/scripts/claude-seo" run render_page.py <url> --mode auto --json` to capture raw HTML, rendered HTML, extracted text, SPA status, and accessibility data when needed
2. **Detect business type**: analyze homepage signals per seo orchestrator
3. **Crawl site**: follow internal links up to 500 pages, respect robots.txt. Write the dated snapshot (see Site-Wide Checks)
4. **Delegate to subagents** (if available, otherwise run inline sequentially):
   - `seo-technical` -- robots.txt, sitemaps, canonicals, Core Web Vitals, security headers
   - `seo-content` -- E-E-A-T, readability, thin content, AI citation readiness
   - `seo-schema` -- detection, validation, generation recommendations
   - `seo-sitemap` -- structure analysis, quality gates, missing pages
   - `seo-performance` -- LCP, INP, CLS measurements
   - `seo-visual` -- screenshots, mobile testing, above-fold analysis
   - `seo-geo` -- AI crawler access, llms.txt, citability, brand mention signals
   - `seo-agentic` -- Lighthouse Agentic Browsing fraction (X/N), accessibility tree for agents, AI agent access policy, Markdown and discovery files, WebMCP (always include in full audits; its findings feed AI Search Readiness)
   - `seo-local` -- GBP signals, NAP consistency, reviews, local schema, industry-specific local factors (spawn when Local Service industry detected: brick-and-mortar, SAB, or hybrid business type)
   - `seo-maps` -- Geo-grid rank tracking, GBP audit, review intelligence, competitor radius mapping (spawn when Local Service detected AND DataForSEO MCP available)
   - `seo-google` -- CWV field data (CrUX), URL indexation (GSC), organic traffic (GA4) (spawn when Google API credentials detected via `"${CLAUDE_PLUGIN_ROOT}/scripts/claude-seo" run google_auth.py --check`)
   - `seo-matomo` -- Matomo Reporting API: organic traffic, landing pages, device / country splits, referrer analysis (spawn when Matomo credentials detected via `"${CLAUDE_PLUGIN_ROOT}/scripts/claude-seo" run matomo_auth.py --check`; runs alongside `seo-google` when both are configured, or as a GA4 alternative when GA4 is not)
   - `seo-backlinks` -- Backlink profile data: DA/PA, referring domains, anchor text, toxic links (spawn when Moz or Bing API credentials detected via `"${CLAUDE_PLUGIN_ROOT}/scripts/claude-seo" run backlinks_auth.py --check`, or always include Common Crawl domain-level metrics)
   - `seo-cluster` -- Semantic clustering analysis (spawn when content strategy signals detected: blog, pillar pages, topic clusters)
   - `seo-sxo` -- Search experience analysis: page-type mismatch, user stories, persona scoring (always include in full audits)
   - `seo-drift` -- Drift analysis: compare against stored baseline (spawn when drift baseline exists for the URL via `"${CLAUDE_PLUGIN_ROOT}/scripts/claude-seo" run drift_history.py <url>`)
   - `seo-ecommerce` -- Product schema, marketplace intelligence (spawn when E-commerce industry detected)
5. **Score** -- aggregate into SEO Health Score (0-100)
6. **Persist audit artifacts** -- write all outputs under `{domain}-audit/`
7. **Report** -- generate prioritized action plan and optional PDF/HTML report

## Crawl Configuration

```
Max pages: 500
Respect robots.txt: Yes
Follow redirects: Yes (max 3 hops)
Timeout per page: 30 seconds
Concurrent requests: 5
Delay between requests: 1 second
```

## Site-Wide Checks (crawl snapshot)

`crawl_snapshot.py` writes one dated snapshot (`snapshot-YYYY-MM-DD.json`); every check below reads it and writes JSON with `findings[]` to merge into `findings/*.md` and `audit-data.json`. Format, finding ids and thresholds: `references/crawl-snapshot.md`.

```bash
CS="${CLAUDE_PLUGIN_ROOT}/scripts/claude-seo"
$CS run crawl_snapshot.py crawl <url> --max-pages 500 --out {domain}-audit/snapshots/
$CS run crawl_snapshot.py onpage {domain}-audit/snapshots/snapshot-<date>.json --out {domain}-audit/findings/onpage.json
$CS run link_audit.py <snapshot> --out {domain}-audit/findings/links.json
$CS run schema_validate.py <snapshot> --out {domain}-audit/findings/schema.json
$CS run serp_title_check.py <snapshot> --serp <stored-serp.json> --out {domain}-audit/findings/serp-titles.json
$CS run crawl_snapshot.py themes <snapshot> --links {domain}-audit/findings/links.json --schema {domain}-audit/findings/schema.json --out {domain}-audit/findings/themes.json
$CS run js_impact.py <snapshot> --max-pages 15 --out {domain}-audit/findings/js-impact.json
$CS run crawl_snapshot.py diff --latest {domain}-audit/snapshots/ --out {domain}-audit/findings/changes.json
$CS run indexnow_submit.py --check <url> --changes {domain}-audit/findings/changes.json --out {domain}-audit/findings/indexnow.json
```

- Re-audit: crawl into the same folder, then `diff --latest` (title, meta description, H1, word-count changes, new and lost pages and referring domains). First crawl has no baseline: say so.
- Report internal-link counts split by indexable vs non-indexable source page; external 403/429/999 are bot-blocks, never "broken".
- `onpage` also covers robots.txt syntax, anchor problems, duplicate H1/title, text-to-HTML ratio, nofollow external links, blocked pages and AI-search content signals; `themes` gives per-theme % (Crawlability, Markup, Internal linking, HTTPS, Performance): show it above the severity list.
- SERP titles: use stored SERP data (DataForSEO organic pulls, `seo.pulls`); no new paid calls.

## Output Files

- `{domain}-audit/FULL-AUDIT-REPORT.md`: Comprehensive findings
- `{domain}-audit/ACTION-PLAN.md`: Prioritized recommendations (Critical > High > Medium > Low)
- `{domain}-audit/audit-data.json`: Structured audit envelope for report generation
- `{domain}-audit/findings/*.md`: Per-category specialist findings (`technical.md`, `content.md`, `schema.md`, `performance.md`, `visual.md`, etc.)
- `{domain}-audit/screenshots/`: Desktop + mobile captures (if Playwright available)
- **PDF Report** (recommended): Generate a professional A4 PDF using `"${CLAUDE_PLUGIN_ROOT}/scripts/claude-seo" run google_report.py --type full --data {domain}-audit/audit-data.json --domain <domain> --output-dir {domain}-audit/`. This produces a white-cover enterprise report with TOC, executive summary, charts (Lighthouse gauges, query bars, index donut), metric cards, threshold tables, prioritized recommendations with effort estimates, and implementation roadmap. Always offer PDF generation after completing an audit.

## Structured Audit Data Envelope

Write `{domain}-audit/audit-data.json` with this shape so `"${CLAUDE_PLUGIN_ROOT}/scripts/claude-seo" run google_report.py --type full --data {domain}-audit/audit-data.json --domain <domain> --output-dir {domain}-audit/` can generate a report even when Google API data is unavailable:

```json
{
  "summary": {
    "health_score": 0,
    "business_type": "detected type",
    "top_findings": [],
    "quick_wins": []
  },
  "categories": [
    {
      "name": "Technical SEO",
      "score": 0,
      "what_works": [],
      "findings": [
        {
          "title": "Finding title",
          "severity": "Critical|High|Medium|Low|Info",
          "description": "Evidence-backed detail",
          "recommendation": "Specific fix"
        }
      ]
    }
  ],
  "action_plan": {
    "phases": [
      {"name": "Phase 1: Critical Fixes", "timeframe": "Week 1", "items": []},
      {"name": "Phase 2: High-Impact Improvements", "timeframe": "Weeks 2-3", "items": []},
      {"name": "Phase 3: Content & Authority", "timeframe": "Month 2", "items": []},
      {"name": "Phase 4: Monitoring & Iteration", "timeframe": "Ongoing", "items": []}
    ]
  },
  "artifacts": {
    "findings_dir": "findings/",
    "screenshots_dir": "screenshots/"
  }
}
```

## Scoring Weights

| Category | Weight |
|----------|--------|
| Technical SEO | 22% |
| Content Quality | 23% |
| On-Page SEO | 20% |
| Schema / Structured Data | 10% |
| Performance (CWV) | 10% |
| AI Search Readiness | 10% |
| Images | 5% |

## Report Structure

### Executive Summary
- Overall SEO Health Score (0-100)
- Business type detected
- Top 5 critical issues
- Top 5 quick wins

### Technical SEO
- Crawlability issues
- Indexability problems
- Security concerns
- Core Web Vitals status

### Content Quality
- E-E-A-T assessment
- Thin content pages
- Duplicate content issues
- Readability scores

### On-Page SEO
- Title tag issues
- Meta description problems
- Heading structure
- Internal linking gaps

### Schema & Structured Data
- Current implementation
- Validation errors
- Missing opportunities

### Performance
- LCP, INP, CLS scores
- Resource optimization needs
- Third-party script impact

### Images
- Missing alt text
- Oversized images
- Format recommendations

### AI Search Readiness
- Citability score
- Structural improvements
- Authority signals

## Priority Definitions

- **Critical**: Blocks indexing or causes penalties (fix immediately)
- **High**: Significantly impacts rankings (fix within 1 week)
- **Medium**: Optimization opportunity (fix within 1 month)
- **Low**: Nice to have (backlog)

## DataForSEO Integration (Optional)

If DataForSEO MCP tools are available, spawn the `seo-dataforseo` agent alongside existing subagents to enrich the audit with live data: real SERP positions, backlink profiles with spam scores, on-page analysis (Lighthouse), business listings, and AI visibility checks (ChatGPT scraper, LLM mentions).

## Google API Integration (Optional)

If Google API credentials are configured (`"${CLAUDE_PLUGIN_ROOT}/scripts/claude-seo" run google_auth.py --check`), spawn the `seo-google` agent to enrich the audit with real Google field data: CrUX Core Web Vitals (replaces lab-only estimates), GSC URL indexation status, search performance (clicks, impressions, CTR), and GA4 organic traffic trends. The Performance (CWV) category score benefits most from field data.

## Matomo Integration (Optional)

If Matomo credentials are configured (`"${CLAUDE_PLUGIN_ROOT}/scripts/claude-seo" run matomo_auth.py --check`), spawn the `seo-matomo` agent to enrich the audit with self-hosted analytics: organic visits trend, top landing pages, device and country breakdowns, channel / search-engine split, and organic keywords. Works as a GA4 alternative (when only Matomo is configured) or as a complement (when both GA4 and Matomo are present). Matomo numbers will not match GA4 exactly because of segmentation differences (`referrerType==search` vs `sessionDefaultChannelGroup == "Organic Search"`) and attribution-window rules.

## Google Update Correlation

Before attributing a traffic or ranking change to anything, list the confirmed
Google updates in that window from the primary-source ledger:

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/claude-seo" run seo_updates.py --since <yyyy-mm> --json
```

Every entry cites a Google-owned URL. If `freshness.stale` is true, say the
ledger may miss recent updates and check status.search.google.com before
drawing conclusions. A date overlap is a hypothesis, never proof of cause.

## Error Handling

| Scenario | Action |
|----------|--------|
| URL unreachable (DNS failure, connection refused) | Report the error clearly. Do not guess site content. Suggest the user verify the URL and try again. |
| robots.txt blocks crawling | Report which paths are blocked. Analyze only accessible pages and note the limitation in the report. |
| Rate limiting (429 responses) | Back off and reduce concurrent requests. Report partial results with a note on which sections could not be completed. |
| Timeout on large sites (500+ pages) | Cap the crawl at the timeout limit. Report findings for pages crawled and estimate total site scope. |
| Subagent hits its `maxTurns` budget on a large site | Findings are not lost: every audit subagent writes a partial `output_dir/findings/*.md` after its first analysis pass and overwrites it with the complete findings before finishing. Read whatever findings file exists and merge it into the report, noting it may be partial. |
