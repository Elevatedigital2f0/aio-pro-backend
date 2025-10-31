import os, re, time, urllib.parse
from typing import List, Set, Dict, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

API_KEY = os.getenv("API_KEY", "elevate-12345-secret")
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
REQUEST_TIMEOUT = 20

app = FastAPI(title="AIO Pro Backend", version="1.2")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

def _session():
    s = requests.Session()
    retries = Retry(
        total=4,
        backoff_factor=0.6,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"]
    )
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.mount("http://", HTTPAdapter(max_retries=retries))
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-NZ,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1"
    })
    return s

SESSION = _session()

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

def _abs(base: str, href: str) -> str:
    return urllib.parse.urljoin(base, href)

def _get(url: str) -> Optional[requests.Response]:
    try:
        return SESSION.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    except Exception:
        return None

def _discover_links(html: str, base_url: str) -> List[str]:
    soup = BeautifulSoup(html, "lxml")
    links = []

    # Normal anchors
    for a in soup.select("a[href]"):
        url = _abs(base_url, a.get("href"))
        links.append(url)

    # WP pagination
    for a in soup.select("a.page-numbers, a.next, a.older-posts"):
        links.append(_abs(base_url, a.get("href") or ""))

    # /page/N pattern
    for a in soup.find_all("a", href=True):
        if re.search(r"/page/\d+/?$", a["href"]):
            links.append(_abs(base_url, a["href"]))

    return list(dict.fromkeys(links))

def _parse_sitemap(url: str, base: str) -> List[str]:
    out: List[str] = []
    r = _get(url)
    if not r or r.status_code != 200:
        return out
    soup = BeautifulSoup(r.text, "xml")

    # index
    sm = soup.find_all("sitemap")
    if sm:
        for node in sm:
            loc = node.find("loc")
            if loc and loc.text:
                out.extend(_parse_sitemap(loc.text.strip(), base))
        return list(dict.fromkeys([u for u in out if _same_domain(base, u)]))

    # urlset
    for node in soup.find_all("url"):
        loc = node.find("loc")
        if loc and loc.text:
            out.append(loc.text.strip())

    # fallback
    for loc in soup.find_all("loc"):
        if loc and loc.text:
            out.append(loc.text.strip())

    return list(dict.fromkeys([u for u in out if _same_domain(base, u)]))

def crawl_depth_first(start_url: str, max_pages: int = 40, max_depth: int = 2) -> Dict:
    visited: Set[str] = set()
    queue: List[tuple] = [(start_url, 0)]
    base = start_url

    while queue and len(visited) < max_pages:
        url, depth = queue.pop(0)
        if url in visited or not _same_domain(base, url):
            continue

        resp = _get(url)
        if not resp or resp.status_code != 200:
            continue

        visited.add(url)

        if depth < max_depth:
            for link in _discover_links(resp.text, url):
                if link not in visited and _same_domain(base, link):
                    queue.append((link, depth + 1))

        time.sleep(0.2)  # politeness

    links = sorted(list(visited))
    return {"url": start_url, "link_count": len(links), "links": links[:500]}

def _default_sitemap_candidates(start_url: str) -> List[str]:
    # Works for WP & most CMS
    base = start_url.rstrip("/")
    domain = urllib.parse.urlparse(base).scheme + "://" + urllib.parse.urlparse(base).netloc
    return [
        f"{domain}/sitemap_index.xml",
        f"{domain}/sitemap.xml",
        f"{domain}/post-sitemap.xml",
        f"{domain}/sitemap/posts-sitemap.xml",
    ]

def require_auth(authorization: Optional[str]):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization.split(" ", 1)[1].strip()
    if token != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")

@app.post("/crawl_site")
def crawl_site(body: dict, Authorization: Optional[str] = Header(default=None)):
    """
    Body:
    {
      "start_url": "https://example.com/",
      "max_pages": 100,
      "max_depth": 3,
      "sitemap_url": "https://example.com/post-sitemap.xml"  # optional
    }
    Plug-and-play behaviour:
    - If sitemap_url is provided → use it.
    - Else auto-try common sitemap URLs; if found → use it.
    - Else fall back to browser-like crawl with retries + pagination.
    """
    require_auth(Authorization)

    start_url = body.get("start_url") or body.get("url")
    if not start_url:
        raise HTTPException(status_code=400, detail="Provide 'start_url'")

    max_pages = int(body.get("max_pages", 40))
    max_depth = int(body.get("max_depth", 2))
    sitemap_url = body.get("sitemap_url")

    # 1) Explicit sitemap
    if sitemap_url:
        urls = _parse_sitemap(sitemap_url, start_url)
        return {"url": start_url, "link_count": len(urls), "links": urls[:1000]}

    # 2) Auto-sitemap discovery
    for candidate in _default_sitemap_candidates(start_url):
        urls = _parse_sitemap(candidate, start_url)
        if urls:
            return {"url": start_url, "link_count": len(urls), "links": urls[:1000]}

    # 3) Fallback: polite crawl (headers + retries + pagination)
    return crawl_depth_first(start_url, max_pages=max_pages, max_depth=max_depth)
