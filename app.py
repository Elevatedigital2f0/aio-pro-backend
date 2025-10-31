import os
import re
import time
import urllib.parse
from typing import List, Set, Dict, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

# ====== Config ======
API_KEY = os.getenv("API_KEY", "elevate-12345-secret")
REQUEST_TIMEOUT = 20

# Browser-like headers to avoid simple CDN/Cloudflare bot blocks
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-NZ,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# ====== FastAPI ======
app = FastAPI(title="AIO Pro Backend", version="1.3")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# ====== HTTP session with retries ======
def _session() -> requests.Session:
    s = requests.Session()
    retries = Retry(
        total=4,
        backoff_factor=0.6,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
        raise_on_status=False,
    )
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.mount("http://", HTTPAdapter(max_retries=retries))
    s.headers.update(BROWSER_HEADERS)
    return s

SESSION = _session()

# ====== Helpers ======
def require_auth(authorization: Optional[str]):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization.split(" ", 1)[1].strip()
    if token != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")

def _get(url: str) -> Optional[requests.Response]:
    try:
        return SESSION.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    except Exception:
        return None

def _abs(base: str, href: str) -> str:
    return urllib.parse.urljoin(base, href or "")

def _same_domain(base: str, url: str) -> bool:
    try:
        b = urllib.parse.urlparse(base)
        u = urllib.parse.urlparse(url)
        if not u.netloc:  # relative
            return True
        return u.netloc == b.netloc
    except Exception:
        return False

def _domain_root(url: str) -> str:
    u = urllib.parse.urlparse(url.rstrip("/"))
    return f"{u.scheme}://{u.netloc}"

def _dedupe_keep_domain(urls: List[str], base: str) -> List[str]:
    out = []
    seen = set()
    for u in urls:
        if not u:
            continue
        if not _same_domain(base, u):
            continue
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out

# ====== Sitemap parsing ======
def parse_sitemap_xml(xml_text: str) -> List[str]:
    """Return all <loc> URLs found in a sitemap or sitemap index."""
    urls = re.findall(r"<loc>(.*?)</loc>", xml_text, flags=re.IGNORECASE)
    # also handle pretty-printed XML with namespaces
    if not urls:
        soup = BeautifulSoup(xml_text, "xml")
        urls = [loc.text.strip() for loc in soup.find_all("loc")]
    return list(dict.fromkeys(urls))

def get_sitemap_urls(start_url: str, explicit: Optional[str] = None) -> List[str]:
    """Try multiple sitemap candidates, prefer index -> site -> page -> post."""
    base = _domain_root(start_url)
    candidates = []
    if explicit:
        candidates.append(explicit)
    candidates += [
        f"{base}/sitemap_index.xml",  # WP & many CMS
        f"{base}/sitemap.xml",
        f"{base}/page-sitemap.xml",
        f"{base}/post-sitemap.xml",
        f"{base}/sitemap/posts-sitemap.xml",  # some plugins
    ]
    return list(dict.fromkeys(candidates))

def sitemap_discover_all(start_url: str, explicit: Optional[str] = None) -> List[str]:
    """Try candidates; if index, fetch each child. Returns combined URL list."""
    discovered: List[str] = []
    for sm in get_sitemap_urls(start_url, explicit):
        r = _get(sm)
        if not r or r.status_code != 200:
            continue
        locs = parse_sitemap_xml(r.text)
        if not locs:
            continue

        # If this is an index, many <loc> will be sitemaps; fetch each
        if any("sitemap" in l.lower() and not l.lower().endswith(".xml.gz") and l.lower().endswith(".xml") for l in locs):
            for child in locs:
                rc = _get(child)
                if rc and rc.status_code == 200:
                    urls = parse_sitemap_xml(rc.text)
                    discovered.extend(urls)
        else:
            discovered.extend(locs)

        # We found something meaningful; no need to try lower-priority candidates
        if discovered:
            break

    return _dedupe_keep_domain(discovered, start_url)

# ====== Fallback crawling ======
def discover_links_from_html(html: str, base_url: str) -> List[str]:
    soup = BeautifulSoup(html, "lxml")
    out: List[str] = []

    # Main anchors
    for a in soup.select("a[href]"):
        out.append(_abs(base_url, a.get("href")))

    # Pagination hints (WordPress commonly)
    for a in soup.select("a.page-numbers, a.next, a.older-posts"):
        out.append(_abs(base_url, a.get("href")))

    # /page/N pattern
    for a in soup.find_all("a", href=True):
        if re.search(r"/page/\d+/?$", a["href"]):
            out.append(_abs(base_url, a["href"]))

    return list(dict.fromkeys(out))

def crawl_depth_first(start_url: str, max_pages: int = 60, max_depth: int = 2) -> Dict:
    visited: Set[str] = set()
    queue: List[tuple] = [(start_url, 0)]

    while queue and len(visited) < max_pages:
        url, depth = queue.pop(0)
        if url in visited or not _same_domain(start_url, url):
            continue

        resp = _get(url)
        if not resp or resp.status_code != 200:
            continue

        visited.add(url)

        if depth < max_depth:
            for link in discover_links_from_html(resp.text, url):
                if link not in visited and _same_domain(start_url, link):
                    queue.append((link, depth + 1))

        time.sleep(0.2)  # be polite

    links = sorted(list(visited))
    return {"url": start_url, "link_count": len(links), "links": links[:1000]}

# ====== Routes ======
@app.get("/health")
def health():
    return {"status": "ok", "service": "AIO Pro Backend"}

@app.post("/crawl_site")
def crawl_site(body: dict, Authorization: Optional[str] = Header(default=None)):
    """
    Body:
    {
      "start_url": "https://example.com/",
      "max_pages": 120,            # optional
      "max_depth": 3,              # optional
      "sitemap_url": "https://.../sitemap.xml"  # optional
    }
    Plug-and-play behaviour:
    1) If sitemap_url provided -> use it.
    2) Else try common sitemaps (index -> site -> page -> post).
    3) If none found or empty -> fallback polite crawl.
    """
    require_auth(Authorization)

    start_url = body.get("start_url") or body.get("url")
    if not start_url:
        raise HTTPException(status_code=400, detail="Provide 'start_url'")

    max_pages = int(body.get("max_pages", 60))
    max_depth = int(body.get("max_depth", 2))
    sitemap_url = body.get("sitemap_url")

    # 1 & 2) Sitemap-first
    urls = sitemap_discover_all(start_url, sitemap_url)
    if urls:
        urls = _dedupe_keep_domain(urls, start_url)
        return {"url": start_url, "link_count": len(urls), "links": urls[:1000]}

    # 3) Fallback crawl
    return crawl_depth_first(start_url, max_pages=max_pages, max_depth=max_depth)
