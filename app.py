import os
import re
import time
import urllib.parse
from typing import List, Set, Dict, Optional

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

API_KEY = os.getenv("API_KEY", "elevate-12345-secret")   # same as Render env
USER_AGENT = "AIO-Pro/1.1 (+https://elevatedigital.co.nz)"
REQUEST_TIMEOUT = 12

app = FastAPI(title="AIO Pro Backend", version="1.1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

@app.get("/health")
def health():
    return {"status": "ok", "service": "AIO Pro Backend"}

def _same_domain(base: str, url: str) -> bool:
    try:
        b = urllib.parse.urlparse(base)
        u = urllib.parse.urlparse(url)
        return (u.netloc == b.netloc) or (u.netloc == "" and u.path)
    except Exception:
        return False

def _absolutize(base: str, href: str) -> str:
    return urllib.parse.urljoin(base, href)

def _get(url: str) -> Optional[requests.Response]:
    try:
        return requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
    except Exception:
        return None

def _discover_links(html: str, base_url: str) -> List[str]:
    soup = BeautifulSoup(html, "lxml")
    links = []

    # Normal anchors
    for a in soup.select("a[href]"):
        href = a.get("href")
        if not href:
            continue
        url = _absolutize(base_url, href)
        links.append(url)

    # WordPress pagination (e.g., /blogs/page/2/, .page-numbers, “Older posts”)
    for a in soup.select("a.page-numbers, a.next, a.older-posts"):
        href = a.get("href")
        if not href:
            continue
        url = _absolutize(base_url, href)
        links.append(url)

    # Also catch /page/2 style patterns if present in text attrs
    for a in soup.find_all("a", href=True):
        if re.search(r"/page/\d+/?$", a["href"]):
            links.append(_absolutize(base_url, a["href"]))

    # De-dup
    deduped = list(dict.fromkeys(links))
    return deduped

def _parse_sitemap(url: str) -> List[str]:
    """Supports sitemap index and plain sitemap (WordPress)."""
    out = []
    resp = _get(url)
    if not resp or resp.status_code != 200:
        return out
    soup = BeautifulSoup(resp.text, "xml")

    # If it's a sitemap index, follow children
    children = soup.find_all("sitemap")
    if children:
        for sm in children:
            loc = sm.find("loc")
            if not loc: continue
            out.extend(_parse_sitemap(loc.text.strip()))
        return out

    # Otherwise normal urlset
    for urltag in soup.find_all("url"):
        loc = urltag.find("loc")
        if not loc: continue
        out.append(loc.text.strip())

    # Fallback for some generators
    for loc in soup.find_all("loc"):
        if loc and loc.text:
            out.append(loc.text.strip())

    # Clean/unique
    return list(dict.fromkeys(out))

def crawl_depth_first(start_url: str, max_pages: int = 20, max_depth: int = 2) -> Dict:
    visited: Set[str] = set()
    to_visit: List[tuple] = [(start_url, 0)]
    base = start_url

    while to_visit and len(visited) < max_pages:
        url, depth = to_visit.pop(0)
        if url in visited:
            continue
        if not _same_domain(base, url):
            continue

        resp = _get(url)
        if not resp or resp.status_code != 200:
            continue

        visited.add(url)

        if depth < max_depth:
            for link in _discover_links(resp.text, url):
                if _same_domain(base, link) and link not in visited:
                    to_visit.append((link, depth + 1))

        # small politeness
        time.sleep(0.2)

    links = sorted(list(visited))
    return {
        "url": start_url,
        "link_count": len(links),
        "links": links[:200],  # cap payload
    }

def require_auth(authorization: Optional[str]):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization.split(" ", 1)[1].strip()
    if token != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")

@app.post("/crawl_site")
def crawl_site(body: dict, Authorization: Optional[str] = Header(default=None)):
    """
    Body can be:
    {
      "start_url": "https://example.com",   # homepage or any page
      "max_pages": 50,
      "max_depth": 3,
      "sitemap_url": "https://example.com/post-sitemap.xml"  # optional
    }
    """
    require_auth(Authorization)

    start_url = body.get("start_url") or body.get("url")
    if not start_url:
        raise HTTPException(status_code=400, detail="Provide 'start_url'")
    max_pages = int(body.get("max_pages", 20))
    max_depth = int(body.get("max_depth", 2))
    sitemap_url = body.get("sitemap_url")

    if sitemap_url:
        urls = _parse_sitemap(sitemap_url)
        # Keep only same-domain and de-dup
        urls = [u for u in urls if _same_domain(start_url, u)]
        urls = list(dict.fromkeys(urls))
        return {
            "url": start_url,
            "link_count": len(urls),
            "links": urls[:500]
        }

    # Depth-first crawl with pagination awareness
    return crawl_depth_first(start_url, max_pages=max_pages, max_depth=max_depth)

