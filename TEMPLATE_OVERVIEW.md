[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/deepseek-research)

DeepSeek Research — enter a topic, watch the deep search run live (thinking plus up to ~20 server-side searches), and get a cited markdown report with a shareable link and a markdown export. One FastAPI service, one SQLite volume, one required variable: your DeepSeek API key. There is no SearXNG service, no SERP API keys, and no scraping code — the entire retrieval stack runs on DeepSeek's side via the deepseek_web_search server tool, and the report is written by deepseek-flash with thinking enabled and the provider safety review on by default.

**Workflow.** The topic form posts to /research, which redirects to /reports/id. While the report runs, the page shows live server-sent-event progress — searching with real tool-call counts, then thinking, then writing. Finished reports render sanitized cited markdown (raw HTML disabled, javascript: links rejected, citation URLs restricted to http/https), list every source, show measured usage (searches, fetches, tokens) and an estimated cost, and export as raw markdown at /reports/id/export.md. Report links use unguessable 64-bit ids and stay readable by anyone who has them; creation can optionally be gated.

**Cost and time — billed to your own DeepSeek account.** Deep reports take a few minutes and roughly 5–30 searches plus page fetches each (the model adapts depth to the topic). Search and page fetch are $0.75 per 1,000 uses; deepseek-flash tokens are $0.30 per 1M input and $1.20 per 1M output at peak (01:00–04:00 and 06:00–10:00 UTC Mon–Fri are half price). A typical report lands around $0.05–0.25 at list rates — a measured example: 28 searches + 18 fetches + about 130k input tokens came to roughly $0.21 peak / $0.10 off-peak for a technology comparison, and narrower topics run cheaper. Every finished report shows its measured usage and an estimated cost at list rates, and one report runs at a time — while one is generating, other attempts get a friendly "try again shortly" page instead of piling up spend.

**The variables.** The deploy form asks for `DEEPSEEK_API_KEY` (your key from platform.deepseek.com — the one required prompt) and pre-fills `APP_ACCESS_PASSWORD` with a per-deploy generated secret (creation gate; delete it in the Variables tab if you want an open instance). `MAX_REPORTS` (default 100, FIFO retention) and `MAX_OUTPUT_TOKENS` (default 12000 — thinking consumes the same output budget as report text, so the budget is bounded and a budget-limited report is flagged as truncated) are optional. Reports persist in SQLite on the /data volume and survive redeploys; a restart marks an in-flight report honestly as failed.

**Safety and rendering.** Every research call sends `safety_check: {level: high}` — DeepSeek's provider-side safety review is the deployer's ToS duty for public-facing apps, and this template enables it out of the box, surfacing refusals as a clean "report declined" page. Report markdown is rendered with markdown-it-py in its JS-default preset (raw HTML disabled, so model-emitted HTML is escaped rather than executed) with the default javascript:-link validator and http/https-only citation URLs.

**Where this sits in the marketplace.** The marketplace already lists deep-research stacks — DeerFlow (23 deploys, 0 percent health), GPT Researcher and an auth-gateway variant, Local Deep Research (SearXNG bundle), Agentfield Deep Research, BettaFish Research Reports, Odysseus. This template takes a different shape on purpose: one service, one volume, one variable, with retrieval running server-side on DeepSeek — there is no SearXNG index to size, no SERP key to buy, no multi-service agent stack to babysit, and every report carries real citations from the searches actually run.

# Deploy and Host

## About Hosting

You are hosting a single FastAPI service (uvicorn on python:3.12-slim, pinned dependencies, running as a non-root user) that serves a small server-rendered UI, consumes one streaming DeepSeek Chat Completions call per report (deepseek_web_search tool + thinking enabled + safety_check high), and stores reports in SQLite on the /data volume. The only required configuration is the `DEEPSEEK_API_KEY` variable (your key from platform.deepseek.com — billed to your account at $0.75 per 1,000 searches plus token usage; reports cost roughly $0.05–0.25 each at list rates and take a few minutes). `APP_ACCESS_PASSWORD` arrives pre-generated as a per-deploy secret; `MAX_REPORTS` and `MAX_OUTPUT_TOKENS` are optional knobs. Generation is deliberately one-at-a-time (in-process lock, single worker) so a burst of topics cannot pile up silent spend; the container idles at well under 100 MB RAM.

## Why Deploy

- Live progress you can trust: the SSE progress line shows real tool-call counts from the stream (searches and page fetches), then thinking, then writing — not a fake spinner.
- Cited reports out of the box: every report ends with a sources list built from the citations DeepSeek actually returned, and the report page shows measured usage plus an estimated cost at list rates.
- Zero retrieval plumbing: no SearXNG service, no index volume, no SERP keys, no scraping code — DeepSeek runs the search server-side, so nothing breaks when a site changes its markup.
- Shareable by default: finished reports live at unguessable /reports/id links readable by anyone who has them, with raw markdown export; creation can be gated with the pre-generated access password.
- XSS-safe rendering and provider-side safety review on by default — honest defaults for a public-facing instance.

## Common Use Cases

- A self-serve research desk: paste a topic, wait a few minutes, read (or export) a cited brief with sources.
- Content and SEO briefs: generate sourced backgrounders on a niche and hand the markdown export to your writing pipeline.
- Technology comparisons: ask for SQLite vs Postgres for analytics, self-hosted vs SaaS, or framework-vs-framework write-ups with live citations.
- Internal knowledge starting points: point new team members at a topic and let them share the report link instead of re-searching.
- A demo of DeepSeek's deep-search capability: the progress UI exposes exactly what the server-side search stack does per report.

## Dependencies for

### Deployment Dependencies

- DeepSeek API account: the deploy form's required `DEEPSEEK_API_KEY` (create one at platform.deepseek.com). All search, fetch, and token usage bills to your account; reports cost roughly $0.05–0.25 each at list rates (measured example in the cost section above).
- Railway volume: the template provisions one small volume at /data holding the SQLite database (reports persist across redeploys; no external database service).
- No other services, no SERP/SearXNG stack, no build step — pinned Python dependencies install at image build, and the container runs as a non-root user with the volume ownership fixed at boot.
