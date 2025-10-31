from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import httpx, asyncio, re
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from playwright.async_api import async_playwright

app = FastAPI()

class CrawlRequest(BaseModel):
    start_url: str
    max_pages: int = 100
    max_depth: int = 2

@app.get("/health")
async def get_health():
    return {"status": "ok", "service": "AIO Pro Backend"}

async def render_page(url):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await page.goto(url, timeout=20000)
            html = await page.content()
        except Exception as e:
            html = ""
        await browser.close()
        return html

@app.post("/crawl_site")
async def crawl_site(req: CrawlRequest):
    start_url = req.start_url.rstrip("/")
    visited = set()
    to_visit = [(start_url, 0)]
    found_links = set()

    async with httpx.AsyncClient(timeout=15.0) as client:
        while to_visit and len(visited) < req.max_pages:
            url, depth = to_visit.pop(0)
            if url in visited or depth > req.max_depth:
                continue

            visited.add(url)
            try:
                html = await render_page(url)
                if not html:
                    continue
                soup = BeautifulSoup(html, "html.parser")
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    abs_url = urljoin(url, href)
                    if urlparse(abs_url).netloc == urlparse(start_url).netloc:
                        found_links.add(abs_url)
                        if abs_url not in visited:
                            to_visit.append((abs_url, depth + 1))
            except Exception as e:
                print(f"Error crawling {url}: {e}")

    return {
        "url": start_url,
        "link_count": len(found_links),
        "links": list(found_links)
    }
