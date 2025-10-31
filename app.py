from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
import httpx
from lxml import etree

app = FastAPI()

class CrawlRequest(BaseModel):
    start_url: str
    max_pages: int = 500

@app.get("/health")
def health():
    return {"status": "ok", "service": "AIO Pro Backend"}

def same_domain(u, base_netloc):
    try:
        return urlparse(u).netloc == base_netloc
    except:
        return False

async def get(url, client, headers=None):
    try:
        r = await client.get(url, headers=headers or {}, follow_redirects=True, timeout=15.0)
        if r.status_code == 200:
            return r
    except:
        pass
    return None

async def collect_from_sitemaps(base_url, client):
    """Load sitemap_index.xml and any sub-sitemaps to collect URLs."""
    urls = set()
    base = base_url.rstrip("/")
    candidates = [
        f"{base}/sitemap_index.xml",
        f"{base}/sitemap.xml",
        f"{base}/post-sitemap.xml",
        f"{base}/page-sitemap.xml",
    ]
    seen_xml = set()
    async def parse_xml(xml_url):
        if xml_url in seen_xml:
            return
        seen_xml.add(xml_url)
        r = await get(xml_url, client)
        if not r:
            return
        try:
            xml = etree.fromstring(r.content)
            locs = xml.xpath("//loc/text()")
            for loc in locs:
                if loc.endswith(".xml"):
                    await parse_xml(loc)
                else:
                    urls.add(loc)
        except:
            pass

    for c in candidates:
        await parse_xml(c)
    return urls

async def collect_from_wp_rest(base_url, client):
    """If WordPress REST is available, list pages and posts (up to pagination caps)."""
    urls = set()
    base = base_url.rstrip("/")
    api_root = f"{base}/wp-json/wp/v2"

    async def paged(endpoint, per_page=100, max_pages=20):
        results = []
        for page in range(1, max_pages + 1):
            r = await get(f"{api_root}/{endpoint}?per_page={per_page}&page={page}", client)
            if not r:
                break
            try:
                data = r.json()
                if not data:
                    break
                results.extend(data)
                if len(data) < per_page:
                    break
            except:
                break
        return results

    # Quick probe to see if REST is open:
    probe = await get(f"{api_root}/types", client)
    if not probe:
        return urls  # REST closed or not WP

    # Pages
    for item in await paged("pages"):
        link = item.get("link")
        if link:
            urls.add(link)

    # Posts
    for item in await paged("posts"):
        link = item.get("link")
        if link:
            urls.add(link)

    return urls

async def collect_from_hubs(base_url, client, hub_paths=None):
    """Lightweight HTML link scrape for key hubs (/services, /blog, etc.)."""
    hubs = hub_paths or ["/", "/services", "/blog", "/blogs", "/about", "/contact", "/pricing", "/projects", "/case-studies"]
    urls = set()
    base = base_url.rstrip("/")
    netloc = urlparse(base).netloc
    headers = {"User-Agent": "AIO-Pro-Bot/1.0"}

    for path in hubs:
        r = await get(urljoin(base + "/", path.lstrip("/")), client, headers=headers)
        if not r:
            continue
        try:
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.find_all("a", href=True):
                abs_url = urljoin(r.url, a["href"])
                if same_domain(abs_url, netloc):
                    urls.add(abs_url)
        except:
            pass
    return urls

@app.post("/crawl_site")
async def crawl_site(req: CrawlRequest):
    base = req.start_url.strip()
    if not base.startswith("http"):
        raise HTTPException(status_code=400, detail="start_url must include http(s)://")

    netloc = urlparse(base).netloc
    found = set()

    async with httpx.AsyncClient() as client:
        # 1) Try sitemaps (fast & comprehensive if available)
        found |= await collect_from_sitemaps(base, client)

        # 2) Try WordPress REST (pages + posts) if exposed
        found |= await collect_from_wp_rest(base, client)

        # 3) Fallback: scrape common hubs for internal links
        found |= await collect_from_hubs(base, client)

    # Keep only same-domain, cap to max_pages
    same_domain_links = [u for u in found if same_domain(u, netloc)]
    unique = []
    seen = set()
    for u in same_domain_links:
        if u not in seen:
            unique.append(u)
            seen.add(u)
        if len(unique) >= req.max_pages:
            break

    return {"url": base, "link_count": len(unique), "links": unique}
