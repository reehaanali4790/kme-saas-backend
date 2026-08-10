# LME Monitoring System — Progress Log

## Session: 2026-06-02

### Completed This Session

#### 1. Vessel Tracking Page (`vessel_tracking.html`)
- Created full AIS vessel tracking page with Leaflet.js map + OpenStreetMap
- Route: `/vessel-tracking` added to `backend/main.py`
- Navigation entry added to sidebar under **TRACKING** section in `shared_layout.js`
- Cache version bumped to `?v=4` across all HTML files

#### 2. Backend WebSocket Proxy (`backend/main.py`)
- Added `/ws/vessel-tracking?name=SHIPNAME` WebSocket endpoint
- Browser connects to our server → server connects to aisstream.io
- Reason: browser (Windows/Playwright/Chrome) blocks outbound `wss://` to external servers
- Server-side filtering by vessel name before forwarding to browser
- Added `import websockets as ws_client`, `import json`, `import asyncio`

#### 3. Bug Fixes Applied
- **Blob frames**: aisstream.io sends binary frames; added `raw.decode('utf-8')` before `send_text()`
- **Frontend Blob**: added `event.data instanceof Blob ? await event.data.text() : event.data`  
- **Button reset**: trackBtn/stopBtn not swapping back after 60s timeout — fixed
- **Debug code cleanup**: removed all `console.log`, `_msgCount`, `_typeSeen` debug artifacts

#### 4. Dependencies
- `websockets>=12.0` added to `requirements.txt`
- Install in venv: `venv\Scripts\pip.exe install websockets`

### Current State
- **Local**: Fully working. Server runs via uvicorn PID ~4780
- **Live (Railway)**: Pushed — will auto-deploy. `websockets` will be installed from requirements.txt on Railway build
- **Tested**: Found MSC GABRIELLA, MSC NAIROBI X, MSC DRAGON live on map with full data cards

---

## Pending / To Do

### Vessel Tracker
- [ ] Show vessel name correctly in "Found" status (currently shows trimmed name — minor display bug)
- [ ] Consider adding MMSI-based re-tracking after initial name match (keeps tracking same ship)
- [ ] Add a "history" list of vessels seen in current session

### WhatsApp Integration (Deferred)
- User said "will do it later"
- Plan: Use **UltraMsg** (~$15/mo) — Instance ID + Token needed
- Backend endpoint already partly designed (see `whatsapp_plan.md` in memory)
- Need: UltraMsg account, instance ID, token

### General
- Dashboard overlay bug (non-incognito): possibly browser extension or stale localStorage
  - Workaround: `localStorage.clear()` + hard refresh (Ctrl+Shift+R)
- Railway deployment: `websockets` package will be installed automatically from requirements.txt

---

## How to Run Locally

```powershell
# From project root
& "C:\LME_PROJECT\lme_monitoring_system\venv\Scripts\uvicorn.exe" main:app --host 0.0.0.0 --port 8000
# Working directory must be: C:\LME_PROJECT\lme_monitoring_system\backend
```

Open: http://localhost:8000/login  
Credentials: admin / admin123

---

## Key Files

| File | Purpose |
|------|---------|
| `backend/main.py` | FastAPI app + all routes + AIS WebSocket proxy |
| `vessel_tracking.html` | Vessel tracker frontend |
| `shared_layout.js` | Shared sidebar nav + auth utilities |
| `backend/config/database.py` | DB connection (reads Railway env vars) |
| `requirements.txt` | Python dependencies |
| `Procfile` | Railway start command |
