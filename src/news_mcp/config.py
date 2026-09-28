"""Runtime settings, all from environment variables (no secrets except the optional HTTP bearer)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path

DEFAULT_USER_AGENT = "news-mcp/0.1 (personal homelab RSS reader; polls politely with conditional GET)"


@dataclass(frozen=True)
class Settings:
    db_path: Path = field(default_factory=lambda: Path(os.environ.get("NEWS_DB", "data/news.db")))
    feeds_path: Path | None = field(
        default_factory=lambda: Path(p) if (p := os.environ.get("NEWS_FEEDS")) else None
    )
    retention_days: int = field(default_factory=lambda: int(os.environ.get("NEWS_RETENTION_DAYS", "14")))
    user_agent: str = field(default_factory=lambda: os.environ.get("NEWS_USER_AGENT", DEFAULT_USER_AGENT))
    fetch_timeout: float = field(default_factory=lambda: float(os.environ.get("NEWS_FETCH_TIMEOUT", "20")))
    # >0 makes `serve` refresh in the background (handy for Claude Desktop, where no timer exists).
    refresh_minutes: int = field(default_factory=lambda: int(os.environ.get("NEWS_REFRESH_MINUTES", "0")))
    # Bearer token required on streamable-http when set (mcpo sends it as a header).
    http_token: str | None = field(default_factory=lambda: os.environ.get("NEWS_MCP_TOKEN") or None)


@dataclass(frozen=True)
class Feed:
    source: str
    name: str
    url: str
    lang: str
    country: str
    category: str
    active: bool = True


def load_feeds(path: Path | None) -> list[Feed]:
    """Read the curated list: NEWS_FEEDS if set, else the packaged feeds.toml."""
    raw = path.read_bytes() if path else files("news_mcp").joinpath("feeds.toml").read_bytes()
    data = tomllib.loads(raw.decode("utf-8"))
    feeds = [Feed(**f) for f in data.get("feed", [])]
    dupes = {f.source for f in feeds if sum(g.source == f.source for g in feeds) > 1}
    if dupes:
        raise ValueError(f"duplicate feed source ids: {sorted(dupes)}")
    return feeds
