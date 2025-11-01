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
    max_pages = max(1, min(req.max_pages, 2000))

    discovered: Set[str] = set()
    to_visit: List[str] = []
    visited: Set[str] = set()

    async with httpx.AsyncClient(follow_redirects=True) as client:
        discovered.add(start)
        sitemaps = await discover_sitemaps(client, start)
        for sm in sitemaps:
            _, xml_text = await fetch_text(client, sm)
            if xml_text:
                discovered.update(u for u in extract_urls_from_sitemap(xml_text) if same_host(u, host))

        discovered.update(await enumerate_wordpress(client, start, host))
        to_visit = [u for u in discovered if same_host(u, host)]

        while to_visit and len(discovered) < max_pages:
            batch = []
            while to_visit and len(batch) < 10:
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
                new_links = extract_links_from_html(text, page_url, host)
                for nl in new_links:
                    if nl not in discovered and same_host(nl, host):
                        discovered.add(nl)
                        if len(discovered) < max_pages:
                            to_visit.append(nl)

    final_links = sorted(discovered)
    return CrawlResult(url=start, link_count=len(final_links), links=final_links)


# ---------- NEW FEATURES: Schema Validator + AI Snippet Simulation ----------

def fetch_html(url: str, timeout: int = 30) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (AIO-Pro/1.0)"}
    with httpx.Client(headers=headers, timeout=timeout, follow_redirects=True) as client:
        r = client.get(url)
        r.raise_for_status()
        return r.text


def extract_json_ld(html: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.text)
            out.extend(data if isinstance(data, list) else [data])
        except Exception:
            pass
    return out


@app.post("/validate_schema")
async def validate_schema(payload: Dict[str, Any] = Body(...)):
    url = payload.get("url")
    if not url:
        return {"error": "Missing 'url'."}
    try:
        resp = httpx.post(
            "https://validator.schema.org/validate",
            json={"url": url, "validationMode": "all"},
            timeout=60
        )
        data = resp.json()
    except Exception as e:
        return {"error": str(e)}

    try:
        html = fetch_html(url)
        blocks = extract_json_ld(html)
        types = [b.get("@type") for b in blocks if "@type" in b]
        eligible = any(t in ["Article", "Product", "FAQPage", "VideoObject", "LocalBusiness"] for t in types)
    except Exception:
        types, eligible = [], False

    return {
        "url": url,
        "eligible_for_rich_results": eligible,
        "detected_types": types,
        "raw": data
    }


@app.post("/ai_snippet_simulate")
async def ai_snippet_simulate(payload: Dict[str, Any] = Body(...)):
    url = payload.get("url")
    if not url:
        return {"error": "Missing 'url'."}
    html = fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")

    title = soup.title.string if soup.title else ""
    h1 = soup.find("h1").get_text(strip=True) if soup.find("h1") else ""
    meta = soup.find("meta", attrs={"name": "description"})
    desc = meta["content"] if meta and meta.get("content") else ""

    snippet = f"{title} — {h1}. {desc}".strip()
    if len(snippet.split()) > 55:
        snippet = " ".join(snippet.split()[:55]) + "…"

    return {
        "url": url,
        "title": title,
        "h1": h1,
        "ai_snippet_simulation": snippet,
        "recommended_schema_types": ["WebPage", "BreadcrumbList", "Organization"]
    }
