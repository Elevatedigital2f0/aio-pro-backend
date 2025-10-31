from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from playwright.async_api import async_playwright
import asyncio, re, httpx
from lxml import etree

app = FastAPI()

class CrawlRequest(BaseModel):
    start_url: str
    max_pages: int = 200
    max_depth: int = 3

@app.get("/health")
async def get_health():
    return {"status": "ok", "service": "AIO Pro Backend"}

async def get_sitemap_links(sitemap_url):
    urls = set()
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.get(sitemap_url)
            if resp.status_code != 200:
                return []
            xml = etree.fromstring(resp.content)
            sitemap_links = xml.xpath("//loc/text()")
            for link in sitemap_links:
                if link.endswith(".xml"):
                    urls.update(await get_sitemap_links(link))
                else:
                    urls.add(link)
        except Exception:
            pass
    return list(urls)

async def render_page(url):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent="Mozilla/5.0 (AIO Pro Bot)")
        page = await context.new_page()
        try:
            await page.goto(url, timeout=30000)
            html = await page.content()
        except Exception:
            html = ""
        await browser.close()
        return html

@app.post("/crawl_site")
async def crawl_site(req: CrawlRequest):
    start_url = req.start_url.rstrip("/")
    domain = urlparse(start_url).netloc
    visited, to_visit, found_links = set(), [(start_url, 0)], set()

    # Step 1: Try sitemap if available
    sitemap_urls = await get_sitemap_links(start_url + "/sitemap_index.xml")
    if sitemap_urls:
        found_links.update([u for u in sitemap_urls if domain in u])

    # Step 2: Crawl with Playwright
    while to_visit and len(visited) < req.max_pages:
        url, depth = to_visit.pop(0)
        if url in visited or depth > req.max_depth:
            continue
        visited.add(url)

        html = await render_page(url)
        if not html:
            continue

        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            abs_url = urljoin(url, href)
            if urlparse(abs_url).netloc == domain:
                found_links.add(abs_url)
                if abs_url not in visited:
                    to_visit.append((abs_url, depth + 1))

    return {
        "url": start_url,
        "link_count": len(found_links),
        "links": list(found_links)
    }
