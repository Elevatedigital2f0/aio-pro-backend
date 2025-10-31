
import os, json, httpx
from typing import Optional
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from bs4 import BeautifulSoup

API_KEY = os.getenv("API_KEY", "changeme")
app = FastAPI(title="AIO Pro Backend", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def require_key(auth: Optional[str]):
    if not auth or auth.split()[-1] != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

class CrawlRequest(BaseModel):
    start_url: str
    max_pages: int = 20

@app.post("/crawl_site")
async def crawl_site(req: CrawlRequest, authorization: Optional[str] = Header(None)):
    require_key(authorization)
    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(req.start_url, timeout=15)
            r.raise_for_status()
        except Exception as e:
            raise HTTPException(400, f"Fetch failed: {e}")
    soup = BeautifulSoup(r.text, "html.parser")
    links = [a["href"] for a in soup.find_all("a", href=True)]
    return {"url": req.start_url, "link_count": len(links), "links": links[:20]}

@app.get("/health")
async def health():
    return {"status": "ok", "service": "AIO Pro Backend"}
