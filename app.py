from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, HttpUrl
from typing import List, Set, Tuple
from urllib.parse import urljoin, urlparse, urldefrag
import httpx
from bs4 import BeautifulSoup
import asyncio
import re

app = FastAPI(title="AIO Pro Backend", version="1.3.1")

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
    # Remove fragments and whitespace; keep https canonicalization to caller
    url = urldefrag(url.strip())[0]
    # Filter out non-http(s)
    if not url.startswith("http://") and not url.startswith("https://"):
        return ""
    # Exclude mailto/tel/etc (double guard)
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
        # Some WP endpoints return JSON; that’s ok for REST; for HTML we’ll parse conditionally
        return url, r.text
    except Exception:
        return url, ""


async def discover_sitemaps(client: httpx.AsyncClient, root: str) -> Set[str]:
    """Try common sitemap locations + auto-discover from robots.txt."""
    found: Set[str] = set()

    # robots.txt discovery
    robots_url = urljoin(root, "/robots.txt")
    _, robots = await fetch_text(client, robots_url)
    if robots:
        for line in robots.splitlines():
            if line.lower().startswith("sitemap:"):
                sm = line.split(":", 1)[1].strip()
                sm = normalize_url(sm)
                if sm:
                    found.add(sm)

    # common locations
    for path in SITEMAP_CANDIDATES:
        sm_url = urljoin(root, path)
        url_fetched, text = await fetch_text(client, sm_url)
        if text and len(text) > 60 and "<" in text:
            found.add(url_fetched)

    return found


def extract_urls_from_sitemap(xml_text: str) -> Set[str]:
    """Extract <loc> URLs from a (site|index) sitemap."""
    urls: Set[str] = set()
    try:
        soup = BeautifulSoup(xml_text, "xml")
        # urlset -> url -> loc
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
            if not absolute:
                continue
            if same_host(absolute, host):
                links.add(absolute)
    except Exception:
        pass
    return links


async def enumerate_wordpress(client: httpx.AsyncClient, root: str, host: str) -> Set[str]:
    """Use WP REST to list pages + posts if available."""
    urls: Set[str] = set()
    for endpoint in (WORDPRESS_REST_PAGES, WORDPRESS_REST_POSTS):
        url = urljoin(root, endpoint)
        try:
            r = await client.get(url, headers=DEFAULT_HEADERS, timeout=15)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json"):
                data = r.json()
                if isinstance(data, list):
                    for item in data:
                        link = item.get("link") or item.get("guid", {}).get("rendered")
                        if link:
                            link = normalize_url(link)
                            if link and same_host(link, host):
                                urls.add(link)
        except Exception:
            continue
    return urls


# ---------- Routes ----------

@app.get("/health")
async def health():
    return {"status": "ok", "service": "AIO Pro Backend"}


@app.post("/crawl_site", response_model=CrawlResult)
async def crawl_site(req: CrawlRequest):
    start = str(req.start_url)
    parsed = urlparse(start)
    if not parsed.scheme.startswith("http"):
        raise HTTPException(status_code=400, detail="start_url must be http(s)")

    host = parsed.netloc
    max_pages = max(1, min(req.max_pages, 2000))  # sensible upper bound

    discovered: Set[str] = set()
    to_visit: List[str] = []
    visited: Set[str] = set()

    async with httpx.AsyncClient(follow_redirects=True) as client:
        # 1) Always include the homepage
        discovered.add(start)

        # 2) Try sitemaps
        sitemaps = await discover_sitemaps(client, start)
        for sm in sitemaps:
            _, xml_text = await fetch_text(client, sm)
            if xml_text:
                discovered.update(
                    u for u in extract_urls_from_sitemap(xml_text) if same_host(u, host)
                )

        # 3) Try WordPress REST enumeration
        discovered.update(await enumerate_wordpress(client, start, host))

        # 4) Seed crawl queue from whatever we have so far
        #    If we only found the homepage, we’ll still crawl it and collect nav links
        to_visit = [u for u in discovered if same_host(u, host)]

        # 5) Breadth-first crawl (light-touch)
        while to_visit and len(discovered) < max_pages:
            batch = []
            while to_visit and len(batch) < 10:  # fetch up to 10 concurrently
                url = to_visit.pop(0)
                if url in visited:
                    continue
                visited.add(url)
                batch.append(url)

            if not batch:
                break

            tasks = [fetch_text(client, u) for u in batch]
            results = await asyncio.gather(*tasks)

            for page_url, text in results:
                if not text:
                    continue
                # Collect internal links
                new_links = extract_links_from_html(text, page_url, host)
                for nl in new_links:
                    if nl not in discovered and same_host(nl, host):
                        discovered.add(nl)
                        if len(discovered) < max_pages:
                            to_visit.append(nl)

    final_links = sorted(discovered)
    return CrawlResult(url=start, link_count=len(final_links), links=final_links)
