from fastapi import FastAPI, HTTPException, Body
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

from pydantic import BaseModel, HttpUrl
from typing import List, Set, Tuple, Dict, Any
from urllib.parse import urljoin, urlparse, urldefrag
from collections import defaultdict

import httpx
from bs4 import BeautifulSoup
import asyncio
import re
import json
import datetime
import os


app = FastAPI(title="AIO Pro Backend", version="1.4.1")


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
        r = await client.get(url, headers=DEFAULT_HEADERS, timeout=30)
        r.raise_for_status()
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
            r = await client.get(url, headers=DEFAULT_HEADERS, timeout=30)
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


# ---------- ROUTES ----------

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
        # always include homepage
        discovered.add(start)

        # sitemaps
        sitemaps = await discover_sitemaps(client, start)
        for sm in sitemaps:
            _, xml_text = await fetch_text(client, sm)
            if xml_text:
                discovered.update(u for u in extract_urls_from_sitemap(xml_text) if same_host(u, host))

        # WP REST
        discovered.update(await enumerate_wordpress(client, start, host))

        # seed
        to_visit = [u for u in discovered if same_host(u, host)]

        # light BFS crawl
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


# ---------- SCHEMA VALIDATION + SNIPPET SIM ----------

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
    # call validator (best-effort)
    try:
        resp = httpx.post(
            "https://validator.schema.org/validate",
            json={"url": url, "validationMode": "all"},
            timeout=60
        )
        data = resp.json()
    except Exception as e:
        data = {"error": str(e)}

    # local detection for reliability
    try:
        html = fetch_html(url)
        blocks = extract_json_ld(html)
        types = [b.get("@type") for b in blocks if "@type" in b]
        # if any structured type present, treat as “eligible candidate”
        eligible = any(
            t in ["Article", "BlogPosting", "Product", "FAQPage", "VideoObject", "LocalBusiness", "Organization", "Service"]
            for t in (types if isinstance(types, list) else [])
        )
    except Exception:
        types, eligible = [], False

    return {
        "url": url,
        "eligible_for_rich_results": bool(eligible),
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
    h1_el = soup.find("h1")
    h1 = h1_el.get_text(strip=True) if h1_el else ""
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


# ---------- AUTO AUDIT (LOCAL EXECUTION) ----------

@app.post("/auto_audit")
async def auto_audit(payload: Dict[str, Any] = Body(...)):
    start_url = payload.get("start_url")
    max_pages = int(payload.get("max_pages", 30))
    if not start_url:
        return {"error": "Missing 'start_url'."}

    crawl_out = await crawl_site(CrawlRequest(start_url=start_url, max_pages=max_pages))
    links = list(crawl_out.links)[:max_pages]
    pages = []

    for url in links:
        page = {"url": url}
        try:
            html = fetch_html(url, timeout=45)
            blocks = extract_json_ld(html)
            types = [b.get("@type") for b in blocks if "@type" in b]
            eligible = any(
                t in ["Article", "BlogPosting", "Product", "FAQPage", "VideoObject", "LocalBusiness", "Organization", "Service"]
                for t in (types if isinstance(types, list) else [])
            )
            page.update({
                "schema_types": types,
                "eligible_for_rich_results": bool(eligible),
            })

            soup = BeautifulSoup(html, "html.parser")
            title = soup.title.string if soup.title else ""
            h1_el = soup.find("h1")
            h1 = h1_el.get_text(strip=True) if h1_el else ""
            meta = soup.find("meta", attrs={"name": "description"})
            desc = meta["content"] if meta and meta.get("content") else ""
            snippet = f"{title} — {h1}. {desc}".strip()
            if len(snippet.split()) > 55:
                snippet = " ".join(snippet.split()[:55]) + "…"

            page.update({
                "title": title,
                "h1": h1,
                "ai_snippet_simulation": snippet,
                "recommended_schema_types": ["WebPage", "BreadcrumbList", "Organization"]
            })
        except Exception as e:
            page["error"] = str(e)

        pages.append(page)

    return {"domain": start_url, "page_count": len(pages), "pages": pages}


# ---------- REPAIR SCHEMA ENDPOINT ----------

JSONLD_CONTEXT = "https://schema.org"
PREFERRED_ENTITIES = [
    "LocalBusiness", "Organization", "WebSite", "WebPage",
    "Article", "BlogPosting", "FAQPage", "Service", "Product",
    "VideoObject", "BreadcrumbList"
]


def _strip_nones(obj):
    if isinstance(obj, dict):
        return {k: _strip_nones(v) for k, v in obj.items() if v not in (None, "", [], {}, "null")}
    if isinstance(obj, list):
        return [_strip_nones(v) for v in obj if v not in (None, "", [], {}, "null")]
    return obj


def _ensure_context(block):
    if "@context" not in block:
        block["@context"] = JSONLD_CONTEXT
    return block


def _as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _collect_types(blocks):
    types = []
    for b in blocks:
        t = b.get("@type")
        if isinstance(t, list):
            types.extend(t)
        elif isinstance(t, str):
            types.append(t)
    return types


def _infer_recommendations(merged: Dict[str, Any], detected_types: List[str]) -> List[str]:
    recs: List[str] = []
    ts = set(detected_types)

    if "Article" in ts or "BlogPosting" in ts:
        if not merged.get("author"):
            recs.append("Add 'author' (Person) with name and profile URL.")
        if "datePublished" not in merged or "dateModified" not in merged:
            recs.append("Add 'datePublished' and 'dateModified'.")
    if "Service" in ts:
        if "offers" not in merged:
            recs.append("Add 'offers' with priceRange or Offer details.")
        if "areaServed" not in merged:
            recs.append("Add 'areaServed' for GEO targeting.")
    if "LocalBusiness" in ts or "Organization" in ts:
        if "sameAs" not in merged:
            recs.append("Add 'sameAs' with official social profiles.")
        if "contactPoint" not in merged:
            recs.append("Add 'contactPoint' with phone and contactType.")
    if "FAQPage" not in ts:
        recs.append("Consider adding FAQPage for answer-first snippets.")
    if "VideoObject" not in ts:
        recs.append("Add VideoObject with transcript for SGE trust.")
    if "BreadcrumbList" not in ts:
        recs.append("Add BreadcrumbList for context and sitelinks.")
    return recs


@app.post("/repair_schema")
async def repair_schema(payload: Dict[str, Any] = Body(...)):
    url = payload.get("url")
    if not url:
        return {"error": "Missing 'url'."}

    try:
        html = fetch_html(url)
    except Exception as e:
        return {"error": f"Fetch failed: {e}"}

    blocks = extract_json_ld(html)
    if not blocks:
        # Build minimal page skeleton
        soup = BeautifulSoup(html, "html.parser")
        title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
        h1_el = soup.find("h1")
        h1 = h1_el.get_text(strip=True) if h1_el else ""
        skeleton = _ensure_context({
            "@type": ["WebPage"],
            "headline": h1 or title,
            "name": title or h1,
            "url": url
        })
        return {
            "url": url,
            "invalid_types": [],
            "merged_jsonld": json.dumps(skeleton, ensure_ascii=False),
            "validation_summary": "No JSON-LD found — created minimal WebPage schema.",
            "content_recommendations": [
                "Add Organization/LocalBusiness schema.",
                "Add Service, Article, or FAQPage schema as relevant."
            ]
        }

    # normalise contexts
    blocks = [_ensure_context(b) for b in blocks]

    # group by @type
    by_type: defaultdict[str, list] = defaultdict(list)
    for b in blocks:
        tlist = _as_list(b.get("@type"))
        if not tlist:
            by_type["_unknown"].append(b)
        else:
            for t in tlist:
                by_type[t].append(b)

    detected_types = _collect_types(blocks)
    merged_root: Dict[str, Any] = {"@context": JSONLD_CONTEXT}

    # Identity (prefer LocalBusiness > Organization)
    identity = by_type.get("LocalBusiness", [None])[0] or by_type.get("Organization", [None])[0]
    if identity:
        keep_keys = {"@type", "name", "url", "logo", "sameAs", "address", "telephone", "email", "contactPoint"}
        merged_root.update({k: identity.get(k) for k in keep_keys if identity.get(k)})

    # WebSite
    if by_type.get("WebSite"):
        ws = by_type["WebSite"][0]
        keep_ws = {"@type", "name", "url", "potentialAction", "inLanguage"}
        merged_root["webSite"] = {k: ws.get(k) for k in keep_ws if ws.get(k)}

    # WebPage
    if by_type.get("WebPage"):
        wp = by_type["WebPage"][0]
        keep_wp = {"@type", "name", "headline", "about", "primaryImageOfPage", "breadcrumb", "inLanguage"}
        merged_root["webPage"] = {k: wp.get(k) for k in keep_wp if wp.get(k)}

    # Article / BlogPosting (prefer BlogPosting)
    post = None
    if by_type.get("BlogPosting"):
        post = by_type["BlogPosting"][0]
        post["@type"] = "BlogPosting"
    elif by_type.get("Article"):
        post = by_type["Article"][0]
        post["@type"] = "Article"
    if post:
        keep_post = {
            "@type", "headline", "name", "description", "author",
            "datePublished", "dateModified", "image", "mainEntityOfPage", "articleSection"
        }
        merged_root["primaryContent"] = {k: post.get(k) for k in keep_post if post.get(k)}

    # FAQPage
    if by_type.get("FAQPage"):
        faq = by_type["FAQPage"][0]
        if "mainEntity" in faq:
            merged_root["faq"] = {"@type": "FAQPage", "mainEntity": faq["mainEntity"]}

    # VideoObject
    if by_type.get("VideoObject"):
        vid = by_type["VideoObject"][0]
        keep_vid = {"@type", "name", "description", "thumbnailUrl", "uploadDate", "embedUrl", "contentUrl", "transcript"}
        merged_root["video"] = {k: vid.get(k) for k in keep_vid if vid.get(k)}

    # BreadcrumbList
    if by_type.get("BreadcrumbList"):
        bc = by_type["BreadcrumbList"][0]
        if "itemListElement" in bc:
            merged_root["breadcrumbs"] = {"@type": "BreadcrumbList", "itemListElement": bc["itemListElement"]}

    # Clean + ensure context
    merged_root = _ensure_context(_strip_nones(merged_root))

    # Try remote validation (best-effort)
    validation_summary = "Merged OK."
    try:
        resp = httpx.post(
            "https://validator.schema.org/validate",
            json={"raw": json.dumps(merged_root, ensure_ascii=False), "validationMode": "all"},
            timeout=60
        )
        jd = resp.json() if resp.headers.get("content-type","").startswith("application/json") else {}
        errs: List[str] = []
        for item in _as_list(jd.get("errors")):
            msg = (item.get("message") or item.get("error") or "").strip()
            if msg:
                errs.append(msg)
        if errs:
            validation_summary = f"Validator warnings/errors: {len(errs)} issue(s). First: {errs[0][:200]}"
    except Exception:
        # keep silent if validator unreachable
        pass

    # Recommendations
    content_recs = _infer_recommendations(merged_root, detected_types)

    return {
        "url": url,
        "invalid_types": [t for t in by_type.keys() if t not in PREFERRED_ENTITIES and t != "_unknown"],
        "merged_jsonld": json.dumps(merged_root, ensure_ascii=False),
        "validation_summary": validation_summary,
        "content_recommendations": content_recs
    }


# ---------- Static files & plugin helpers ----------

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# /static (for logo etc.)
if not os.path.isdir("static"):
    os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# /.well-known/ai-plugin.json
@app.get("/.well-known/ai-plugin.json", include_in_schema=False)
async def serve_plugin_manifest():
    return FileResponse(".well-known/ai-plugin.json", media_type="application/json")

# /openapi.yaml (the spec you paste into the GPT “Actions”)
@app.get("/openapi.yaml", include_in_schema=False)
async def serve_openapi_yaml():
    return FileResponse("aio-pro-backend.yaml", media_type="text/yaml")
