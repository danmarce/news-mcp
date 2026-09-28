# news-mcp

Curated RSS/Atom/RDF news → SQLite (rolling ~14-day window) → structured MCP tools.
Items are stored in their **original language**; the LLM translates, summarizes and compares framing at query time.
Design rationale: [CLAUDE.md](CLAUDE.md).

## Tools
| tool | use |
|---|---|
| `latest_news(source?, lang?, country?, category?, hours=24, n=15)` | newest headlines, filterable |
| `news_by_topic(query, days=7, lang?, n=20)` | keyword search; spaces = AND, `\|` = OR (`"Noboa \| ノボア"`) — case/accent-insensitive |
| `compare_coverage(topic, days=7, per_source=3)` | matches grouped by outlet + outlets with no match → framing comparison |
| `sources()` | curated feeds, item counts, freshness / last status |

## Run
```bash
uv sync
uv run news-mcp refresh                      # pull all feeds once (conditional GET), upsert, prune
uv run news-mcp serve                        # stdio (Claude Desktop / dev)
NEWS_MCP_TOKEN=secret uv run news-mcp serve --transport http --host 0.0.0.0 --port 8000   # streamable-http at /mcp
uv run pytest
```

| env | default | |
|---|---|---|
| `NEWS_DB` | `data/news.db` | SQLite path (WAL; refresh and server can share it) |
| `NEWS_FEEDS` | packaged `src/news_mcp/feeds.toml` | curated feed list |
| `NEWS_RETENTION_DAYS` | `14` | rolling window |
| `NEWS_REFRESH_MINUTES` | `0` (off) | >0: `serve` also refreshes in the background (no timer needed) |
| `NEWS_MCP_TOKEN` | unset | bearer token required on HTTP (`/healthz` stays open) |
| `NEWS_USER_AGENT`, `NEWS_FETCH_TIMEOUT` | polite UA, `20` | |

## Claude Desktop
`%APPDATA%\Claude\claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "news": {
      "command": "uv",
      "args": ["--directory", "C:\\Code\\news-mcp", "run", "news-mcp", "serve"],
      "env": { "NEWS_DB": "C:\\Code\\news-mcp\\data\\news.db", "NEWS_REFRESH_MINUTES": "45" }
    }
  }
}
```

## Homelab (mcpo → OWUI)
Image: `docker build -t news-mcp .` — default CMD serves HTTP on :8000 at `/mcp`; run `news-mcp refresh` from a timer
against the same `/data` volume (or set `NEWS_REFRESH_MINUTES` on the server and skip the timer). mcpo entry:
```json
{ "mcpServers": { "news": { "type": "streamable-http", "url": "http://news-mcp:8000/mcp",
  "headers": { "Authorization": "Bearer ${NEWS_MCP_TOKEN}" } } } }
```

## AI assistance
news-mcp is developed openly with the help of Claude (Anthropic). We state this plainly: commits
Claude helped write carry a `Co-Authored-By: Claude` trailer. The code and design are open source so the
work can be inspected, reused, and given back.

## License
Code: [MPL-2.0](LICENSE). The repo ships only feed *URLs*; the news content it fetches belongs to each publisher and
stays in your local database. Respect each feed's terms, keep the refresh interval polite (≥30 min), and set
`NEWS_USER_AGENT` to identify your own deployment.
