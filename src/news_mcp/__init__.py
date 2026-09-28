"""news-mcp: curated RSS/Atom/RDF news -> SQLite -> structured MCP tools."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .config import Settings

log = logging.getLogger("news_mcp")


async def _refresh_loop(settings: Settings) -> None:
    from .refresh import refresh

    while True:
        try:
            await refresh(settings)
        except Exception:  # keep serving even if a refresh run blows up
            log.exception("background refresh failed")
        await asyncio.sleep(settings.refresh_minutes * 60)


async def _serve(settings: Settings, transport: str, host: str, port: int) -> None:
    from .server import BearerAuth, build_server

    mcp = build_server(settings)
    tasks = []
    if settings.refresh_minutes > 0:
        tasks.append(asyncio.create_task(_refresh_loop(settings)))

    if transport == "stdio":
        await mcp.run_stdio_async()
    else:
        import uvicorn

        app = mcp.streamable_http_app(host=host)
        if settings.http_token:
            app = BearerAuth(app, settings.http_token)
        elif host not in ("127.0.0.1", "localhost", "::1"):
            log.warning("serving on %s without NEWS_MCP_TOKEN - anyone on the network can connect", host)
        await uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="info")).serve()
    for t in tasks:
        t.cancel()


def main() -> None:
    parser = argparse.ArgumentParser(prog="news-mcp", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("refresh", help="pull all active feeds once, upsert, prune (run from a timer)")
    serve = sub.add_parser("serve", help="run the MCP server")
    serve.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    # stderr only: stdout is the MCP channel in stdio mode
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings()

    if args.cmd == "refresh":
        from .refresh import refresh

        results = asyncio.run(refresh(settings))
        failed = [r for r in results if r.status.startswith("error")]
        # partial failure is normal (a feed is down); only fail the job when nothing worked
        sys.exit(1 if results and len(failed) == len(results) else 0)
    else:
        asyncio.run(_serve(settings, args.transport, args.host, args.port))
