from fastapi import FastAPI, HTTPException, Body
from pydantic import BaseModel, HttpUrl
from typing import List, Set, Tuple, Dict, Any
from urllib.parse import urljoin, urlparse, urldefrag
import httpx
from bs4 import BeautifulSoup
import asyncio
import re
import json

app = FastAPI(title="AIO Pro Backend", version="1.3.2")

# ---------- Models ----------

class CrawlRequest(BaseModel):
    start_url: HttpUrl
    max_pages: int = 100


class CrawlResult(BaseModel):
    url: HttpUrl
    link_count: int
    links: List[HttpUrl]


# ---------- Helpers ----------

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0 Safari/537.36 AIO-Visibility-Optimiser"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

SITEMAP_CANDIDATES = [
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/wp-sitemap.xml",
    "/post-sitemap.xml",
    "/page-sitemap.xml",
    "/feed/sitemap.xml",
]

WORDPRESS_REST_PAGES = "/wp-json/wp/v2/pages?per_page=100"
WORDPRESS_REST_POSTS = "/wp-json/wp/v2/posts?per_page=100"


def same_host(url: str, host: str) -> bool:
    return urlparse(url).netloc == host


def normalize_url(url: str) -> str:
    """Remove fragments, ensure only http(s) links."""
    url = urldefrag(url.strip())[0]
    if not url.startswith(("http://", "https://")):
        return ""
    if re.match(r"^(mailto:|tel:|javascript:)", url, re.IGNORECASE):
        return ""
    return url


def absolutize(base: str, maybe_relative: str) -> str:
    return urljoin(base, maybe_relative)


async def fetch_text(client: httpx.AsyncClient, url: str) -> Tuple[str, str]:
    """Return (url, text or '') with soft error handling."""
    try:
        r = await client.get(url, headers=DEFAULT_HEADERS, timeout=15)
        r.raise_for_status()
        return url, r.text
    except Exception:
        return url, ""


async def discover_sitemaps(client: httpx.AsyncClient, root: str) -> Set[str]:
    """Try common sitemap locations + auto-discover from robots.txt."""
    found: Set[str] = set()
    robots_url = urljoin(root, "/robots.txt")
    _, robots = await fetch_text(client, robots_url)
    if robots:
        for line in robots.splitlines():
            if line.lower().startswith("sitemap:"):
                sm = line.split(":", 1)[1].strip()
                sm = normalize_url(sm)
                if sm:
                    found.add(sm)
    for path in SITEMAP_CANDIDATES:
        sm_url = urljoin(root, path)
        url_fetched, text = await fetch_text(client, sm_url)
        if text and len(text) > 60 and "<" in text:
            found.add(url_fetched)
    return found


def extract_urls_from_sitemap(xml_text: str) -> Set[str]:
    """Extract <loc> URLs from sitemap XML."""
    urls: Set[str] = set()
    try:
        soup = BeautifulSoup(xml_text, "xml")
        for loc in soup.find_all("loc"):
            if loc and loc.text:
                u = normalize_url(loc.text)
                if u:
                    urls.add(u)
    except Exception:
        pass
    return urls


def extract_links_from_html(html: str, base_url: str, host: str) -> Set[str]:
    """Collect internal <a href> links."""
    links: Set[str] = set()
    try:
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a.get("href")
            if not href:
                continue
            absolute = absolutize(base_url, href)
            absolute = normalize_url(absolute)
            if absolute and same_host(absolute, host):
                links.add(absolute)
    except Exception:
        pass
    return links


async def enumerate_wordpress(client: httpx.AsyncClient, root: str, host: str) -> Set[str]:
    """Use WP RES
