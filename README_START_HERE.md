
# AIO Pro Backend (Free Version)

This is the backend service for your **AI Visibility Optimiser GPT**.

## Deploy for Free
1. Create a free account on **Render.com**
2. Connect your GitHub repo containing these files.
3. Build command: `pip install -r requirements.txt`
4. Start command: `uvicorn app:app --host 0.0.0.0 --port $PORT`
5. Add environment variable: `API_KEY = your-secret-key`
6. Deploy! Your backend will be available at https://your-app.onrender.com

## Test It
Visit: `https://your-app.onrender.com/health`
It should return: `{"status":"ok","service":"AIO Pro Backend"}`

## Use in GPT Actions
Paste **openapi.yaml** into your Custom GPT → *Configure → Actions → Add API Schema*
Replace `YOUR-RENDER-URL` with your live Render URL.

---
Created for Geoff Lanagan / Elevate Digital (NZ)
