from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import yfinance as yf
import math, re, io, os
from datetime import datetime

try:
    import openpyxl
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False

try:
    import xlrd
    HAS_XLRD = True
except ImportError:
    HAS_XLRD = False

app = FastAPI(title="Marchesini Collection", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Cache ─────────────────────────────────────────────────────────────────────
_cache: dict = {}
_cal_cache: dict = {}
CACHE_TTL_H = 0.5
CAL_CACHE_TTL = 1800

def _cache_get(key):
    if key in _cache:
        data, ts = _cache[key]
        if (datetime.now() - ts).total_seconds() < CACHE_TTL_H * 3600:
            return data
    return None

def _cache_set(key, data):
    _cache[key] = (data, datetime.now())

# ── Calendario iCal ──────────────────────────────────────────────────────────
ICAL_URLS = {
    "CCB-A": "https://ical.booking.com/v1/export?t=b671991d-0a88-4ddc-926a-01e24ae15622",
    "CCB-B": "https://ical.booking.com/v1/export?t=a5c0a3ce-c66a-4031-aaf9-b2424903533d",
    "CA":    "https://ical.booking.com/v1/export?t=331ca185-45d1-493f-baa2-80d073bbe23a",
}

def fetch_booked_dates(apt_key: str) -> list:
    url = ICAL_URLS.get(apt_key)
    if not url:
        return []
    cached = _cal_cache.get(apt_key)
    if cached:
        data, ts = cached
        if (datetime.now() - ts).total_seconds() < CAL_CACHE_TTL:
            return data
    try:
        import requests
        from icalendar import Calendar
        from datetime import date, timedelta
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        cal = Calendar.from_ical(r.content)
        booked = []
        for component in cal.walk():
            if component.name == "VEVENT":
                dtstart = component.get("DTSTART").dt
                dtend   = component.get("DTEND").dt
                if hasattr(dtstart, 'date'): dtstart = dtstart.date()
                if hasattr(dtend, 'date'):   dtend = dtend.date()
                current = dtstart
                while current < dtend:
                    booked.append(current.strftime("%Y-%m-%d"))
                    current += timedelta(days=1)
        _cal_cache[apt_key] = (booked, datetime.now())
        return booked
    except Exception as e:
        print(f"Errore iCal {apt_key}: {e}")
        return []

# ── Admin prezzi ──────────────────────────────────────────────────────────────
import json

PREZZI_FILE = os.path.join(os.path.dirname(__file__), "prezzi.json")

@app.post("/api/admin/prezzi")
async def save_prezzi(request_body: dict):
    try:
        with open(PREZZI_FILE, 'w', encoding='utf-8') as f:
            json.dump(request_body, f, ensure_ascii=False, indent=2)
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ── Routes API ────────────────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    return {"status": "ok", "version": "1.0", "timestamp": datetime.now().isoformat()}

@app.get("/api/calendar/{apt_key}")
def get_calendar(apt_key: str):
    if apt_key not in ICAL_URLS:
        raise HTTPException(status_code=400, detail="Apt non valido")
    booked = fetch_booked_dates(apt_key)
    return {"apt": apt_key, "booked_dates": booked, "count": len(booked)}

@app.get("/api/calendar/{apt_key}/refresh")
def refresh_calendar(apt_key: str):
    if apt_key in _cal_cache:
        del _cal_cache[apt_key]
    return get_calendar(apt_key)

# ── Serve file statici (HTML, foto, loghi) ────────────────────────────────────
# Monta le cartelle delle foto
for folder in ["CCB-A", "CCB-B", "Casa Albertina", "Casa Albertina 2", "FOTO CCB", "territorio"]:
    folder_path = os.path.join(os.path.dirname(__file__), folder)
    if os.path.exists(folder_path):
        app.mount(f"/{folder}", StaticFiles(directory=folder_path), name=folder.replace(" ", "_"))

# Homepage
@app.get("/")
def serve_index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))

@app.get("/admin")
def serve_admin():
    return FileResponse(os.path.join(os.path.dirname(__file__), "admin.html"))

@app.get("/guida")
@app.get("/guide")
@app.get("/guida/{prop}")
@app.get("/guide/{prop}")
@app.get("/welcome/{prop}")
def serve_guide(prop: str = "caboare-a"):
    return FileResponse(os.path.join(os.path.dirname(__file__), "guide.html"))

@app.get("/prezzi.json")
def serve_prezzi():
    return FileResponse(PREZZI_FILE)

# Serve loghi e file statici root
@app.get("/{filename}")
def serve_static(filename: str):
    filepath = os.path.join(os.path.dirname(__file__), filename)
    if os.path.exists(filepath) and os.path.isfile(filepath):
        return FileResponse(filepath)
    raise HTTPException(status_code=404, detail="File non trovato")

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    print(f"\n🏡 Marchesini Collection · Server avviato su porta {port}\n")
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
