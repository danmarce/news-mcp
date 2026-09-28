# CLAUDE.md — news-mcp

> Handoff brief. Designed in the homelab war-room session (2026-09-27); build here.
> **Twin of `football-mcp`** (periodic pull → SQLite → FastMCP structured tools). This is the RSS/news specialization.
> Repo = **code** (Python/FastMCP + refresh script). Deployment lives in the separate **homelab** repo — see "Deployment split".

## What this is
A **stateful, SQL-backed MCP server** giving a local LLM **current news** from a **curated set of RSS/Atom/RDF feeds**, served from a local SQLite DB that a **~30–60 min job** refreshes. Two parts: a **refresh pipeline** (pull → upsert → prune) and an **MCP query server** (reads SQLite, structured tools). Python + FastMCP + **`feedparser`** (handles RSS 0.9 / RDF 1.0 / RSS 2.0 / Atom uniformly — don't hand-parse XML).

## Why it exists / where it fits
Consumer is "Yuki" (a grounded Gemma-4 12B home assistant). Sources tier by freshness + type:
- **openzim** (offline Wikipedia) → deep/historical.
- **football-mcp** → current structured sport (scores/standings).
- **news-mcp (this)** → **curated current events.**
- **web_search** → broad fallback (long tail).

news-mcp is the **curated** news tier: **you pick the outlets** → editorial balance / defense-against-being-shaped, vs. web_search's SEO soup.

**Product vision:** a curated feed that **beats Facebook/Twitter as a news source** — social feeds optimize for *engagement* (outrage/virality), are algorithm-curated (you don't control the mix), and carry tracking/ads/misinfo. This is the inverse: *your* trustworthy outlets, **chronological**, private, no ragebait — same convenience, none of the manipulation — plus the LLM's translate/summarize/cross-framing on top. **Why RSS specifically:** it's the *publisher-sanctioned, machine-readable* endpoint (published to be polled), so pulling it is *with-the-grain* — no ToS/robots/bot-block friction, unlike scraping HTML. (The **full-article drill-down** *does* hit that wall — bot-block/paywall — which is why it's deferred; headlines-via-RSS is the open part.) Be a polite poller: real **User-Agent** + conditional GET.

## The LLM edge (why an MCP, not just a feed reader) — drives the design
1. **Translation** — feeds can be in ANY language; the LLM answers in the user's. ⇒ **Add original-language feeds from diverse nations** (DW Deutsch, Al Jazeera Arabic, Le Monde FR, NHK JP), not just ES/EN — originals are richer than foreigner-facing editions.
2. **Summarization** — condense many items / a long article.
3. **Cross-source framing comparison** — *"compara cómo cubrieron X la prensa alemana, catarí, estadounidense y ecuatoriana"* → the model-bias-audition insight as a daily media-literacy tool. Consider a dedicated `compare_coverage(topic)` tool.

## Architecture
```
curated feeds (RSS/RDF/Atom, multi-lang) ──~30–60 min pull──▶ SQLite (rolling window) ──reads──▶ FastMCP tools ──▶ mcpo ──▶ OWUI
```
- Refresh: pull each feed → parse (feedparser) → upsert → **prune items older than N days**.
- MCP: reads SQLite only; **store items in ORIGINAL language (raw)** — the LLM **translates at query time** (keeps refresh light + any target language on demand; store stays clean).

## SQLite schema
- `news(guid PK, source, lang, title, summary, link, published_utc, fetched_at)` — dedupe by `guid` (fallback `link`); `lang` = the feed's language (hint for translate-at-query); keep `summary` raw.
- `sources(source PK, name, url, lang, country, category, active)` — the curated feed list.

## Retention — ROLLING WINDOW (unlike football)
Keep ~**7–14 days** (purpose: answer *"last week's news"* — hold a bit more than the longest expected "recent" query for margin); the refresh job **deletes older rows** → DB stays small (news is just text). `published_utc` powers the time-range query (`WHERE published >= now-7d`). **Don't over-dedupe** — multiple outlets on one story *is* the cross-framing feature; keep them, tag by source. *(Contrast: football-mcp **accumulates** [historical asset]; openzim is a **static snapshot**. Three retention policies, by nature.)*

## MCP tools — STRUCTURED (not text-to-SQL)
- `latest_news(source?, lang?, category?, n=15)` — recent headlines.
- `news_by_topic(query, n)` — keyword match over title/summary.
- `compare_coverage(topic)` — items on one topic across sources (framing comparison).
- `sources()` — list configured feeds.
Return raw items + `lang`; the LLM translates/summarizes/compares in its answer. Parameterized queries; **good docstrings = the model's decision boundary**.

## Principles / ops
- **No API keys** (RSS is open) — but be a **polite poller**: set a `User-Agent`, use **conditional GET (ETag / If-Modified-Since)** so unchanged feeds aren't re-fetched. A down feed = skip, don't crash the run.
- **Faithful translation** — prompt Yuki to translate/summarize faithfully + attribute; the reader has its own lean (center-left-Western — see homelab audition notes), so don't let it editorialize.
- **Transport**: stdio for dev; streamable-http + bearer for mcpo → OWUI (mirror openzim-mcp).

## Curated feeds (starting set — VERIFY each: `curl -sL -o /dev/null -w '%{http_code} %{content_type}\n' URL`)
**Confirmed live (fetched 2026-09-27):** El Comercio (EC) `https://www.elcomercio.com/feed/` · El Universo (EC) `https://www.eluniverso.com/arc/outboundfeeds/rss/?outputType=xml`
**Candidates (verify):**
- Primicias (EC): `https://www.primicias.ec/rss/home.xml`
- BBC Mundo (ES, canonical): `https://feeds.bbci.co.uk/mundo/rss.xml`  ← prefer over `bbc.com/mundo/temas/*/index.xml` topic feeds (many deprecated)
- BBC News (EN): `https://feeds.bbci.co.uk/news/rss.xml`
- DW Español (RDF/RSS 1.0): `https://rss.dw.com/rdf/rss-sp-all`
- El País — ⚠ the handed URL is the **English** edition; swap to the Spanish `…/site/elpais.com/portada` if wanted
- NYT Politics (EN): `https://www.nytimes.com/svc/collections/v1/publish/https://www.nytimes.com/section/politics/rss.xml`
- The Economist Intl (EN, summaries only): `https://www.economist.com/international/rss.xml`
- New Scientist (EN, subscription/summaries): `https://www.newscientist.com/feed/home/`
- Ars Technica (EN, tech/sci): `https://feeds.arstechnica.com/arstechnica/index`
- Diario Olé (AR, football news): `https://www.ole.com.ar/rss/futbol-internacional/` (+ `/sudamericana`, `/libertadores`)
**Balance / original-language adds to consider:** France 24 (ES/FR), Al Jazeera (EN/AR), NHK (JP), Quanta (sci), Hacker News (`news.ycombinator.com/rss`). **RSSHub** bridges anything lacking RSS.

## Compose with fetch (drill-down) — DEFERRED
RSS gives headlines + summaries + links (self-sufficient for "what's happening"). **Full-article drill-down** ("cuéntame más") needs a **fetch tool** (download + extract) = `research-mcp`, currently **on hold** (openzim covered the knowledge-grounding need). So **ship news-mcp headlines-first**; add drill-down when a fetch tool exists (or a minimal fetch here later).

## Deployment split
Code here (server + refresh script). Deploy in **homelab** `containers/stacks/news-mcp/` (compose + the ~30–60 min refresh timer + SQLite volume + feed config), mirroring `openzim-mcp` + `zim-refresh`. No secrets (RSS is keyless).

## First steps
1. Verify the feed list (curl loop above); prune dead ones; note full-text-vs-summary per feed.
2. Scaffold: deps (FastMCP, feedparser, httpx, stdlib sqlite3), schema, refresh script (pull → upsert → prune, conditional GET), MCP server (tools reading SQLite).
3. Test stdio with a seeded DB → then streamable-http + mcpo.
4. Hand deploy (compose + timer) to homelab.

## Origin
Design rationale (curated-vs-web_search balance, translate-at-query + cross-framing = the model-bias-audition insight as a daily tool, the writer/reader + retention patterns) came from the homelab session. Deeper context: homelab `someday/todo.md` (RSS entry) + `runbooks/yuki-model-audition.md`.

## Build status (2026-09-27)
- **Scaffold done**: `src/news_mcp/` — `config.py` (env settings, feeds.toml loader), `db.py` (schema, `fold()` accent/case folding), `refresh.py` (httpx async + conditional GET → feedparser → upsert → prune), `server.py` (4 tools + bearer ASGI gate), `__init__.py` (CLI `news-mcp refresh|serve`). Offline tests in `tests/`.
- **SDK note**: `mcp` 2.x renamed FastMCP → `MCPServer` (`mcp.server.mcpserver`). HTTP = `streamable_http_app()` run under uvicorn, wrapped by our own `BearerAuth` (open `/healthz`).
- **Feeds verified**: 20/21 live (list + notes in `src/news_mcp/feeds.toml`). **Primicias dead** (RSS times out, `/feed/` is HTML) → `active=false`. NHK and New Scientist URLs updated to their 301 targets. ~8/20 honor 304 Not Modified. All are headlines+summaries (El País is the biggest at ~145 items).
- **Schema deviation**: `news` PK is `(source, guid)` not `guid` alone (outlets may reuse ids; per-source keeps cross-framing clean). `sources` also stores poll state (`etag`, `last_modified`, `last_status`, `last_ok`).
- **Topic search** = folded substring match (works in any script, incl. JA/AR without tokenizer); cross-language search needs the term per language (`"Ecuador | Équateur | エクアドル"`) — docstrings tell the model so. Tool output is one compact JSON text block (no `structuredContent` duplicate, `ensure_ascii=False`) to spare the 12B's context.
- **Next**: Claude Desktop trial → homelab compose (image from `Dockerfile`, refresh timer or `NEWS_REFRESH_MINUTES`) → mcpo → OWUI; watch Yuki's tool-choice on the docstrings; maybe more feeds (Olé sudamericana/libertadores, Quanta already in).
