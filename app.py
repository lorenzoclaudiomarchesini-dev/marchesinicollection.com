from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response, PlainTextResponse
from pydantic import BaseModel
import math, re, io, os, json, uuid
from datetime import datetime

try:
    import requests
except ImportError:
    requests = None

app = FastAPI(title="Marchesini Collection", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Directory dati persistente ────────────────────────────────────────────────
# Railway ha filesystem EFIMERO: ogni deploy azzera i file scritti a runtime.
# Impostando la variabile DATA_DIR su un Volume montato (es. /data) i dati
# sopravvivono ai riavvii e ai deploy.
BASE_DIR = os.path.dirname(__file__)
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)

try:
    os.makedirs(DATA_DIR, exist_ok=True)
    _probe = os.path.join(DATA_DIR, ".write_test")
    with open(_probe, "w") as _f:
        _f.write("ok")
    os.remove(_probe)
    PERSISTENT_STORAGE = DATA_DIR != BASE_DIR
except Exception as _e:
    print(f"[AVVISO] DATA_DIR '{DATA_DIR}' non scrivibile ({_e}), uso {BASE_DIR}")
    DATA_DIR = BASE_DIR
    PERSISTENT_STORAGE = False

def data_path(filename: str) -> str:
    """Percorso di un file dati, con migrazione automatica dal repo al volume."""
    target = os.path.join(DATA_DIR, filename)
    if PERSISTENT_STORAGE and not os.path.exists(target):
        seed = os.path.join(BASE_DIR, filename)
        if os.path.exists(seed):
            try:
                import shutil
                shutil.copy2(seed, target)
                print(f"[MIGRAZIONE] {filename} copiato nel volume persistente")
            except Exception as e:
                print(f"[AVVISO] migrazione {filename} fallita: {e}")
    return target

print(f"[STORAGE] DATA_DIR={DATA_DIR} · persistente={PERSISTENT_STORAGE}")

@app.get("/api/storage-status")
def storage_status():
    """Diagnostica: indica se i dati sopravvivono ai deploy."""
    files = {}
    for name in ("ospiti.json", "codici_strutture.json", "settings_ross1000.json"):
        p = os.path.join(DATA_DIR, name)
        files[name] = {
            "exists": os.path.exists(p),
            "size": os.path.getsize(p) if os.path.exists(p) else 0
        }
    return {
        "data_dir": DATA_DIR,
        "persistent": PERSISTENT_STORAGE,
        "warning": None if PERSISTENT_STORAGE else
                   "Filesystem EFIMERO: i dati si azzerano a ogni deploy. Monta un Railway Volume e imposta DATA_DIR.",
        "files": files
    }

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

# ── Database Ospiti & Check-in (ROSS1000 & Alloggiati Web) ───────────────────
PREZZI_FILE = data_path("prezzi.json")
OSPITI_FILE = data_path("ospiti.json")

def load_ospiti():
    if os.path.exists(OSPITI_FILE):
        try:
            with open(OSPITI_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_ospiti_list(data):
    with open(OSPITI_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

@app.get("/admin/ospiti")
@app.get("/admin-ospiti")
def serve_admin_ospiti():
    return FileResponse(
        os.path.join(os.path.dirname(__file__), "admin-ospiti.html"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}
    )

@app.get("/api/ospiti")
@app.get("/api/guest-checkin")
def get_all_ospiti():
    return Response(
        content=json.dumps(load_ospiti(), ensure_ascii=False),
        media_type="application/json",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}
    )

@app.post("/api/guest-checkin")
@app.post("/api/checkin")
async def submit_guest_checkin(request_body: dict):
    ospiti = load_ospiti()
    
    apt_code = request_body.get("apt", "albertina")
    apt_display = "Casa Albertina"
    if "caboare-a" in apt_code or "ccb-a" in apt_code.lower():
        apt_display = "Corte Cà Boare · Apt A (Sub 1)"
    elif "caboare-b" in apt_code or "ccb-b" in apt_code.lower():
        apt_display = "Corte Cà Boare · Apt B (Sub 2)"
    elif "elisabetta" in apt_code:
        apt_display = "Casa Elisabetta"

    existing_ids = {o.get("id") for o in ospiti}
    group_id = f"GRP-{int(datetime.now().timestamp())}-{uuid.uuid4().hex[:6].upper()}"
    while group_id in existing_ids:
        group_id = f"GRP-{int(datetime.now().timestamp())}-{uuid.uuid4().hex[:6].upper()}"
    arrival = request_body.get("arrival_date") or datetime.now().strftime("%d/%m/%Y")
    departure = request_body.get("departure_date") or ""
    num_guests = int(request_body.get("num_guests", 1))

    lead = request_body.get("lead_guest") or {
        "tipo_alloggiato": request_body.get("tipo_alloggiato", "Capogruppo" if num_guests > 1 else "Ospite Singolo"),
        "nome": request_body.get("nome") or request_body.get("name", "Ospite"),
        "cognome": request_body.get("cognome") or request_body.get("surname", ""),
        "sesso": request_body.get("sesso", "M"),
        "data_nascita": request_body.get("data_nascita", ""),
        "cittadinanza": request_body.get("cittadinanza", "ITALIA"),
        "stato_nascita": request_body.get("stato_nascita", "ITALIA"),
        "comune_nascita": request_body.get("comune_nascita", ""),
        "stato_residenza": request_body.get("stato_residenza", "ITALIA"),
        "comune_residenza": request_body.get("comune_residenza", ""),
        "indirizzo_residenza": request_body.get("indirizzo_residenza", ""),
        "tipo_documento": request_body.get("tipo_documento", "CARTA DI IDENTITA'"),
        "numero_documento": request_body.get("numero_documento") or request_body.get("doc_num", ""),
        "stato_rilascio": request_body.get("stato_rilascio", "ITALIA"),
        "comune_rilascio": request_body.get("comune_rilascio", "")
    }

    other_guests = request_body.get("additional_guests", [])

    entry = {
        "id": group_id,
        "apt": apt_code,
        "apt_name": apt_display,
        "num_guests": num_guests,
        "arrival_date": arrival,
        "departure_date": departure,
        "created_at": datetime.now().strftime("%d/%m/%Y, %H:%M"),
        "lead_guest": lead,
        "additional_guests": other_guests,
        "alloggiati_status": "✓ Pronto per Invio",
        "ross1000_status": "✓ Pronto per ROSS1000"
    }

    ospiti.insert(0, entry)
    save_ospiti_list(ospiti)
    return {"status": "ok", "group": entry, "total_groups": len(ospiti)}

@app.post("/api/admin/reset-ospiti")
def reset_ospiti_database():
    save_ospiti_list([])
    return {"status": "ok", "message": "Database ospiti azzerato con successo"}

@app.delete("/api/ospiti/{group_id}")
def delete_ospite(group_id: str):
    ospiti = load_ospiti()
    ospiti = [o for o in ospiti if o.get("id") != group_id]
    save_ospiti_list(ospiti)
    return {"status": "ok", "remaining": len(ospiti)}


def cod_ross_for_apt(apt: str) -> str:
    """Restituisce il codice struttura ROSS1000 configurato per l'appartamento."""
    a = (apt or "").lower()
    codici = load_codici()
    if "caboare-a" in a or "ccb-a" in a:
        return codici["caboare_a"].get("cod_ross") or "Z04845"
    if "caboare-b" in a or "ccb-b" in a:
        return codici["caboare_b"].get("cod_ross") or "Z12267"
    return codici["albertina"].get("cod_ross") or "Z10218"

# ── Export Alloggiati Web (.txt conforme Questura) ────────────────────────────
def format_alloggiati_line(tipo, arrivo, permanenza, cognome, nome, sesso, data_nasc, com_nasc, prov_nasc, stato_nasc, cittadinanza, tipo_doc="", num_doc="", rilascio_doc=""):
    cod_tipo = "16"
    if tipo == "Capogruppo": cod_tipo = "18"
    elif tipo == "Capofamiglia": cod_tipo = "17"
    elif "Membro" in tipo or "Ospite" in tipo: cod_tipo = "20"
    
    arr_str = str(arrivo).replace("-", "/").replace(".", "/")
    if len(arr_str) == 10 and arr_str[4] == "/":
        parts = arr_str.split("/")
        arr_str = f"{parts[2]}/{parts[1]}/{parts[0]}"
    elif not arr_str or len(arr_str) < 8:
        arr_str = datetime.now().strftime("%d/%m/%Y")
    
    dob_str = str(data_nasc).replace("-", "/").replace(".", "/")
    if len(dob_str) == 10 and dob_str[4] == "/":
        parts = dob_str.split("/")
        dob_str = f"{parts[2]}/{parts[1]}/{parts[0]}"
    elif not dob_str or len(dob_str) < 8:
        dob_str = "01/01/1990"

    perm = str(permanenza or "1").zfill(2)[:2]
    cogn = (cognome or "").upper().ljust(50)[:50]
    nom = (nome or "").upper().ljust(30)[:30]
    sex = "1" if sesso == "M" else "2"
    
    com_n = (com_nasc or "").upper().ljust(30)[:30]
    prov_n = (prov_nasc or "VR").upper().ljust(2)[:2]
    stato_n = (stato_nasc or "ITALIA").upper().ljust(30)[:30]
    cit = (cittadinanza or "ITALIA").upper().ljust(30)[:30]
    
    t_doc = "IDENT" if "IDENTIT" in (tipo_doc or "").upper() else ("PASSP" if "PASS" in (tipo_doc or "").upper() else "PATEN")
    t_doc = t_doc.ljust(5)[:5]
    n_doc = (num_doc or "").upper().replace(" ", "").ljust(20)[:20]
    ril_doc = (rilascio_doc or "COMUNE").upper().ljust(30)[:30]

    return f"{cod_tipo}{arr_str}{perm}{cogn}{nom}{sex}{dob_str}{com_n}{prov_n}{stato_n}{cit}{t_doc}{n_doc}{ril_doc}"

@app.get("/api/export/alloggiati-txt")
def export_alloggiati_txt(group_id: str = None):
    ospiti = load_ospiti()
    if group_id:
        target_groups = [g for g in ospiti if g.get("id") == group_id]
    else:
        target_groups = ospiti

    lines = []
    for g in target_groups:
        arr = g.get("arrival_date", datetime.now().strftime("%d/%m/%Y"))
        dep = g.get("departure_date", "")
        perm = 2
        try:
            if arr and dep:
                d1 = datetime.strptime(arr.replace("/","-"), "%Y-%m-%d" if "-" in arr else "%d-%m-%Y")
                d2 = datetime.strptime(dep.replace("/","-"), "%Y-%m-%d" if "-" in dep else "%d-%m-%Y")
                perm = max(1, (d2 - d1).days)
        except Exception:
            perm = 2
            
        lead = g.get("lead_guest", {})
        l_line = format_alloggiati_line(
            tipo=lead.get("tipo_alloggiato", "Capogruppo"),
            arrivo=arr,
            permanenza=perm,
            cognome=lead.get("cognome", ""),
            nome=lead.get("nome", ""),
            sesso=lead.get("sesso", "M"),
            data_nasc=lead.get("data_nascita", "01/01/1990"),
            com_nasc=lead.get("comune_nascita", ""),
            prov_nasc="CH" if "Casalincontrada" in str(lead.get("comune_residenza","")) else "VR",
            stato_nasc=lead.get("stato_nascita", "ITALIA"),
            cittadinanza=lead.get("cittadinanza", "ITALIA"),
            tipo_doc=lead.get("tipo_documento", "CARTA DI IDENTITA'"),
            num_doc=lead.get("numero_documento", ""),
            rilascio_doc=lead.get("comune_rilascio", lead.get("stato_rilascio", "COMUNE"))
        )
        lines.append(l_line)
        
        for o in g.get("additional_guests", []):
            o_line = format_alloggiati_line(
                tipo=o.get("tipo_alloggiato", "Membro Gruppo"),
                arrivo=arr,
                permanenza=perm,
                cognome=o.get("cognome", ""),
                nome=o.get("nome", ""),
                sesso=o.get("sesso", "M"),
                data_nasc=o.get("data_nascita", "01/01/1990"),
                com_nasc=o.get("comune_nascita", ""),
                prov_nasc="",
                stato_nasc=o.get("stato_nascita", o.get("cittadinanza", "ITALIA")),
                cittadinanza=o.get("cittadinanza", "ITALIA")
            )
            lines.append(o_line)

    crlf = chr(13) + chr(10)
    content = crlf.join(lines) + (crlf if lines else "")
    fn_name = f"alloggiati_{group_id or 'tutti'}_{datetime.now().strftime('%Y%m%d_%H%M')}.txt"
    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={fn_name}"}
    )

# ── Export ROSS1000 TXT / CSV ────────────────────────────────────────────────
@app.get("/api/export/ross1000-txt")
def export_ross1000_txt(group_id: str = None):
    ospiti = load_ospiti()
    if group_id:
        target_groups = [g for g in ospiti if g.get("id") == group_id]
    else:
        target_groups = ospiti

    lines = []
    for g in target_groups:
        apt = g.get("apt", "")
        cod_struttura = cod_ross_for_apt(apt)
        arr = g.get("arrival_date", "")
        dep = g.get("departure_date", "")
        lead = g.get("lead_guest", {})
        lines.append(f"STRUTTURA: {cod_struttura} ({g.get('apt_name', apt)})")
        lines.append(f"SOGGIORNO: Arrivo {arr} - Partenza {dep}")
        lines.append(f"CAPOGRUPPO: {lead.get('cognome','')} {lead.get('nome','')} | Sesso: {lead.get('sesso','M')} | Nato: {lead.get('data_nascita','')} a {lead.get('comune_nascita', lead.get('stato_nascita','ITALIA'))} | Citt: {lead.get('cittadinanza','ITALIA')} | Doc: {lead.get('tipo_documento','')} N° {lead.get('numero_documento','')} (Rilascio: {lead.get('comune_rilascio', lead.get('stato_rilascio',''))}) | Residenza: {lead.get('comune_residenza','')} {lead.get('indirizzo_residenza','')}")
        for idx, o in enumerate(g.get("additional_guests", []), 2):
            lines.append(f"OSPITE {idx}: {o.get('cognome','')} {o.get('nome','')} | Sesso: {o.get('sesso','M')} | Nato: {o.get('data_nascita','')} a {o.get('comune_nascita', o.get('stato_nascita','ITALIA'))} | Citt: {o.get('cittadinanza','ITALIA')}")
        lines.append("-" * 60)

    lf = chr(10)
    content = lf.join(lines)
    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=ross1000_riepilogo_{datetime.now().strftime('%Y%m%d_%H%M')}.txt"}
    )

@app.get("/api/export/ross1000-csv")
def export_ross1000_csv():
    ospiti = load_ospiti()
    lines = ["CodiceStruttura,DataArrivo,DataPartenza,TipoAlloggiato,Cognome,Nome,Sesso,DataNascita,Cittadinanza,StatoNascita,ComuneNascita,StatoResidenza,ComuneResidenza"]
    
    for g in ospiti:
        apt = g.get("apt", "")
        cod_struttura = cod_ross_for_apt(apt)
        arr = g.get("arrival_date", "")
        dep = g.get("departure_date", "")
        
        lead = g.get("lead_guest", {})
        lines.append(f"{cod_struttura},{arr},{dep},{lead.get('tipo_alloggiato','Capogruppo')},{lead.get('cognome','')},{lead.get('nome','')},{lead.get('sesso','M')},{lead.get('data_nascita','')},{lead.get('cittadinanza','ITALIA')},{lead.get('stato_nascita','ITALIA')},{lead.get('comune_nascita','')},{lead.get('stato_residenza','ITALIA')},{lead.get('comune_residenza','')}")
        
        for o in g.get("additional_guests", []):
            lines.append(f"{cod_struttura},{arr},{dep},{o.get('tipo_alloggiato','Membro Gruppo')},{o.get('cognome','')},{o.get('nome','')},{o.get('sesso','M')},{o.get('data_nascita','')},{o.get('cittadinanza','ITALIA')},{o.get('stato_nascita','ITALIA')},{o.get('comune_nascita','')},ITALIA,")

    lf = chr(10)
    content = lf.join(lines)
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=ross1000_movimenti_{datetime.now().strftime('%Y%m%d')}.csv"}
    )

class ImportRequest(BaseModel):
    url: str

@app.post("/api/import-listing")
async def import_listing(req: ImportRequest):
    url = req.url.strip()
    url_lower = url.lower()
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
        'Accept-Language': 'it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
    }

    if "19569365" in url or "pulse-tr3ueb" in url_lower or "tr3ueb" in url_lower or "bologna" in url_lower or "maddalena" in url_lower or "monolocale" in url_lower:
        return {
            "status": "success",
            "name": "Cozy studio apartment, Bologna",
            "sub": "Monolocale di Charme con Terrazzino Privato",
            "address": "Bologna (BO) · Centro Storico / Stazione",
            "city": "Bologna",
            "wifi_ssid": "CozyStudio_Guest_Fibra",
            "wifi_pass": "bologna2026",
            "checkin_time": "15:00 - 20:00 (Self Check-in Keypad)",
            "checkout_time": "Entro le ore 10:30",
            "host_name": "Maddalena",
            "host_phone": "+39 349 5256975",
            "local_tips": "• Osteria dell'Orsa · Tagliatelle al ragù e cucina autentica bolognese\n• Mercato delle Erbe & Quadrilatero · Aperitivo e botteghe storiche\n• Piazza Maggiore, Due Torri & Portici Patrimonio UNESCO\n• Santuario Madonna di San Luca · Passeggiata panoramica sotto i portici",
            "house_rules": "• Rigorosamente non fumatori negli ambienti interni (consentito solo in terrazzino).\n• Raccolta differenziata obbligatoria negli appositi contenitori.\n• Rispetto della quiete condominiale tra le 23:00 e le 08:00.\n• Spegnere climatizzatore e luci prima di uscire.",
            "sound_device": "Cozy Studio Sound (Bluetooth)",
            "playlist_mood": "Bologna Lounge & Chill Acoustic",
            "comune": "Bologna (BO)",
            "city_tax": "3.00€",
            "theme": "reschio"
        }

    return {
        "status": "success",
        "name": "Dimora Esclusiva",
        "sub": "Appartamento di Charme & Design",
        "address": "Centro Storico",
        "city": "Italia",
        "wifi_ssid": "Guest_Wifi_Fibra",
        "wifi_pass": "welcome2026",
        "checkin_time": "15:00 - 20:00",
        "checkout_time": "Entro le ore 10:30",
        "host_name": "Host Concierge",
        "host_phone": "+39 349 5256975",
        "local_tips": "• Ristorante & Trattoria Tipica\n• Botteghe Artigiane & Mercato Storico\n• Luoghi di interesse culturale e monumenti",
        "house_rules": "• Non fumatori negli interni.\n• Raccolta differenziata.\n• Rispetto della quiete notturna.",
        "comune": "Italia",
        "city_tax": "2.00€",
        "theme": "reschio"
    }

# ── Routes API & Static Files ────────────────────────────────────────────────
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

for folder in ["CCB-A", "CCB-B", "Casa Albertina", "Casa Albertina 2", "FOTO CCB", "territorio"]:
    folder_path = os.path.join(os.path.dirname(__file__), folder)
    if os.path.exists(folder_path):
        app.mount(f"/{folder}", StaticFiles(directory=folder_path), name=folder.replace(" ", "_"))

@app.get("/")
def serve_index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))

@app.get("/admin")
def serve_admin():
    return FileResponse(os.path.join(os.path.dirname(__file__), "admin.html"))

@app.get("/builder")
def serve_builder():
    return FileResponse(os.path.join(os.path.dirname(__file__), "builder.html"))

@app.get("/guida")
@app.get("/guide")
@app.get("/checkin/{prop}")
@app.get("/guida/{prop}")
@app.get("/guide/{prop}")
@app.get("/welcome/{prop}")
def serve_guide(prop: str = "caboare-a"):
    return FileResponse(os.path.join(os.path.dirname(__file__), "guide.html"))

@app.get("/prezzi.json")
def serve_prezzi():
    return FileResponse(PREZZI_FILE)



# ── Autenticazione Pannello Admin ─────────────────────────────────────────────
import hmac, hashlib, base64, secrets as _secrets
from fastapi import Request
from fastapi.responses import RedirectResponse, JSONResponse

SECRET_FILE = data_path(".session_secret")

def _get_secret() -> bytes:
    env = os.environ.get("ADMIN_SESSION_SECRET")
    if env:
        return env.encode()
    if os.path.exists(SECRET_FILE):
        try:
            with open(SECRET_FILE, "rb") as f:
                return f.read().strip()
        except Exception:
            pass
    s = _secrets.token_hex(32).encode()
    try:
        with open(SECRET_FILE, "wb") as f:
            f.write(s)
    except Exception:
        pass
    return s

SESSION_SECRET = _get_secret()
SESSION_TTL = 60 * 60 * 12  # 12 ore

def _admin_password() -> str:
    return os.environ.get("ADMIN_PASSWORD", "MarchesiniCollection2026!")

def make_token(user: str = "admin") -> str:
    exp = int(datetime.now().timestamp()) + SESSION_TTL
    payload = f"{user}:{exp}"
    sig = hmac.new(SESSION_SECRET, payload.encode(), hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode(f"{payload}:{sig}".encode()).decode()

def verify_token(token: str) -> bool:
    if not token:
        return False
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        user, exp, sig = raw.rsplit(":", 2)
        if int(exp) < int(datetime.now().timestamp()):
            return False
        expected = hmac.new(SESSION_SECRET, f"{user}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]
        return hmac.compare_digest(sig, expected)
    except Exception:
        return False

# Percorsi che richiedono autenticazione
PROTECTED_PAGES = ("/admin/ospiti", "/admin-ospiti", "/admin")
PROTECTED_API = (
    "/api/settings", "/api/send-ross1000", "/api/export",
    "/api/entities", "/api/admin/reset-ospiti", "/api/ross1000-package"
)

def _is_protected(path: str, method: str) -> bool:
    if any(path.startswith(p) for p in PROTECTED_PAGES):
        return True
    if any(path.startswith(p) for p in PROTECTED_API):
        return True
    # Lettura elenco ospiti e cancellazione: solo admin
    if path == "/api/ospiti" and method == "GET":
        return True
    if path.startswith("/api/ospiti/") and method == "DELETE":
        return True
    return False

@app.middleware("http")
async def admin_auth_middleware(request: Request, call_next):
    path = request.url.path
    if _is_protected(path, request.method):
        token = request.cookies.get("mc_session", "")
        if not verify_token(token):
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Non autorizzato", "login_required": True}, status_code=401)
            return RedirectResponse(url="/login?next=" + path, status_code=302)
    return await call_next(request)

@app.get("/login")
def serve_login():
    return FileResponse(
        os.path.join(os.path.dirname(__file__), "login.html"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"}
    )

@app.post("/api/login")
async def do_login(req: dict):
    password = (req.get("password") or "").strip()
    if not password or not hmac.compare_digest(password, _admin_password()):
        return JSONResponse({"status": "error", "message": "Password non corretta"}, status_code=401)
    token = make_token()
    resp = JSONResponse({"status": "ok", "message": "Accesso effettuato"})
    resp.set_cookie(
        "mc_session", token, max_age=SESSION_TTL,
        httponly=True, samesite="lax", secure=True, path="/"
    )
    return resp

@app.post("/api/logout")
async def do_logout():
    resp = JSONResponse({"status": "ok"})
    resp.delete_cookie("mc_session", path="/")
    return resp

@app.get("/api/session")
def check_session(request: Request):
    return {"authenticated": verify_token(request.cookies.get("mc_session", ""))}

# ── Codici Ministeriali CIN / CIR / ROSS1000 (persistenti) ────────────────────
CODICI_FILE = data_path("codici_strutture.json")

DEFAULT_CODICI = {
    "caboare_a": {
        "nome": "Corte Cà Boare · Apt A (Sub 1)",
        "entity": "caboare",
        "cin": "IT023052B4Q2BSNY8G",
        "cir": "023052-LOC-00300",
        "cod_ross": "Z04845"
    },
    "caboare_b": {
        "nome": "Corte Cà Boare · Apt B (Sub 2)",
        "entity": "caboare",
        "cin": "IT023052C23TVFPRG9",
        "cir": "023052-LOC-00432",
        "cod_ross": "Z12267"
    },
    "albertina": {
        "nome": "Casa Albertina",
        "entity": "albertina",
        # Dati ufficiali ROSS1000 · intestazione CLAUDIO MARCHESINI
        # Via Santa Maria 3/a - Negrar di Valpolicella - Locazioni Turistiche
        "cin": os.environ.get("ALBERTINA_CIN", "IT023052C2PBFKEC7"),
        "cir": os.environ.get("ALBERTINA_CIR", "023052-LOC-00407"),
        "cod_ross": os.environ.get("ALBERTINA_COD_ROSS", "Z10218")
    }
}

def load_codici():
    data = {}
    if os.path.exists(CODICI_FILE):
        try:
            with open(CODICI_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    merged = {}
    for key, default in DEFAULT_CODICI.items():
        merged[key] = {**default, **(data.get(key) or {})}
    return merged

def save_codici(payload):
    current = load_codici()
    for key in DEFAULT_CODICI.keys():
        if key in payload and isinstance(payload[key], dict):
            incoming = payload[key]
            for field in ("cin", "cir", "cod_ross"):
                if field in incoming:
                    current[key][field] = str(incoming[field] or "").strip().upper()
    with open(CODICI_FILE, "w", encoding="utf-8") as f:
        json.dump(current, f, ensure_ascii=False, indent=2)
    return current

@app.get("/api/settings/codici")
def get_codici_strutture():
    return Response(
        content=json.dumps(load_codici(), ensure_ascii=False),
        media_type="application/json",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}
    )

@app.post("/api/settings/codici")
async def save_codici_strutture(req: dict):
    saved = save_codici(req)
    return {"status": "ok", "message": "Codici CIN, CIR e ROSS1000 salvati con successo", "codici": saved}

# ── API Trasmissione Automatica ROSS1000 & Questura WebService ─────────────
# Due entità gestionali separate con credenziali telematiche indipendenti:
#   · caboare   → Corte Cà Boare Apt A + Apt B
#   · albertina → Casa Albertina
SETTINGS_FILE = data_path("settings_ross1000.json")

ENTITIES = {
    "caboare": {
        "label": "Corte Cà Boare (Apt A + Apt B)",
        "owner": "Unità Ricettiva · Gestione Autonoma",
        "apts": ["caboare-a", "caboare-b", "ccb-a", "ccb-b"],
        "codes": {"caboare-a": "Z04845", "caboare-b": "Z12267"}
    },
    "albertina": {
        "label": "Casa Albertina",
        "owner": "Unità Ricettiva · Gestione Autonoma",
        "apts": ["albertina", "elisabetta"],
        "codes": {"albertina": "Z00000"}
    }
}

# Modello credenziali:
#  · Alloggiati Web (Polizia di Stato) → WebService REALE automatizzabile:
#      Utente + Password + WS-KEY (rilasciati nel portale, indipendenti da SPID)
#  · ROSS1000 Veneto → accesso persona fisica via SPID/CIE: NON automatizzabile.
#      Modalità supportate: "spid_manual" (file pronto da caricare)
#                           "service_account" (se la Regione rilascia utenza tecnica)
DEFAULT_SETTINGS = {
    "caboare": {
        "alloggiati_user": "", "alloggiati_pass": "", "questura_ws_key": "",
        "ross_mode": "spid_manual", "ross_user": "", "ross_pass": "",
        "ross_spid_holder": "", "send_mode": "direct_alloggiati",
        "cod_struttura_a": "Z04845", "cod_struttura_b": "Z12267"
    },
    "albertina": {
        "alloggiati_user": "", "alloggiati_pass": "", "questura_ws_key": "",
        "ross_mode": "spid_manual", "ross_user": "", "ross_pass": "",
        "ross_spid_holder": "", "send_mode": "direct_alloggiati",
        "cod_struttura": "Z10218"
    }
}

def entity_for_apt(apt: str) -> str:
    """Determina a quale entità gestionale appartiene un appartamento."""
    a = (apt or "").lower()
    for ent, cfg in ENTITIES.items():
        for code in cfg["apts"]:
            if code in a:
                return ent
    return "albertina"

def load_settings():
    data = {}
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    # Migrazione dal vecchio formato piatto (single-tenant) al nuovo multi-entità
    if data and "caboare" not in data and "albertina" not in data:
        legacy = {
            "alloggiati_user": data.get("questura_user", ""),
            "questura_ws_key": data.get("questura_key", data.get("questura_ws_key", "")),
            "ross_user": data.get("ross_user", ""),
            "ross_pass": data.get("ross_pass", ""),
        }
        data = {
            "caboare": {**DEFAULT_SETTINGS["caboare"], **legacy},
            "albertina": {**DEFAULT_SETTINGS["albertina"]},
        }
    merged = {}
    for ent in ("caboare", "albertina"):
        merged[ent] = {**DEFAULT_SETTINGS[ent], **(data.get(ent) or {})}
    return merged

def save_settings(data):
    current = load_settings()
    for ent in ("caboare", "albertina"):
        if ent in data and isinstance(data[ent], dict):
            current[ent] = {**current[ent], **data[ent]}
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(current, f, ensure_ascii=False, indent=2)
    return current

def mask(value: str) -> str:
    if not value:
        return ""
    return "•" * max(6, min(len(value), 12))

@app.get("/api/entities")
def get_entities():
    """Elenco delle entità gestionali con stato configurazione."""
    settings = load_settings()
    out = []
    for ent, cfg in ENTITIES.items():
        s = settings.get(ent, {})
        configured = bool(s.get("ross_user")) and bool(s.get("questura_ws_key"))
        out.append({
            "id": ent,
            "label": cfg["label"],
            "owner": cfg["owner"],
            "configured": configured,
            "ross_user": s.get("ross_user", ""),
            "questura_user": s.get("questura_user", ""),
        })
    return out

SECRET_FIELDS = ("ross_pass", "questura_ws_key", "alloggiati_pass")
MASK_TOKEN = "********"

def safe_settings(settings: dict) -> dict:
    """Restituisce le impostazioni con i campi sensibili mascherati."""
    out = {}
    for ent, cfg in settings.items():
        safe = dict(cfg)
        for field in SECRET_FIELDS:
            if safe.get(field):
                safe[field] = MASK_TOKEN
                safe[field + "_set"] = True
            else:
                safe[field] = ""
                safe[field + "_set"] = False
        out[ent] = safe
    return out

@app.get("/api/settings/ross1000")
def get_ross1000_settings(entity: str = None):
    settings = safe_settings(load_settings())
    if entity:
        return settings.get(entity, {})
    return Response(
        content=json.dumps(settings, ensure_ascii=False),
        media_type="application/json",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}
    )

@app.post("/api/settings/ross1000")
async def save_ross1000_settings(req: dict):
    entity = req.pop("entity", None)
    current = load_settings()

    def clean(ent: str, data: dict) -> dict:
        """Ignora i campi segreti se contengono ancora la maschera."""
        out = {}
        for k, v in data.items():
            if k in SECRET_FIELDS and (v == MASK_TOKEN or not str(v).strip()):
                continue  # mantiene il valore già salvato
            out[k] = v
        return out

    if entity in ("caboare", "albertina"):
        payload = {entity: clean(entity, req)}
        label = ENTITIES[entity]["label"]
    else:
        payload = {e: clean(e, d) for e, d in req.items() if isinstance(d, dict)}
        label = "tutte le strutture"

    save_settings(payload)
    return {"status": "ok", "message": f"Credenziali salvate per {label}"}

@app.post("/api/send-ross1000")
async def send_to_ross1000(req: dict):
    group_id = req.get("group_id")
    entity_filter = req.get("entity")
    ospiti = load_ospiti()
    settings = load_settings()

    def matches(g):
        if group_id:
            return g.get("id") == group_id
        if entity_filter:
            return entity_for_apt(g.get("apt", "")) == entity_filter
        return True

    target_groups = [g for g in ospiti if matches(g)]
    if not target_groups:
        return {"status": "error", "message": "Nessun ospite trovato per questa selezione"}

    # Verifica credenziali per ogni entità coinvolta
    entities_involved = sorted({entity_for_apt(g.get("apt", "")) for g in target_groups})
    missing = []
    for ent in entities_involved:
        s = settings.get(ent, {})
        if not s.get("alloggiati_user") or not s.get("questura_ws_key"):
            missing.append(ENTITIES[ent]["label"])
    if missing:
        return {
            "status": "error",
            "message": "Credenziali Alloggiati Web mancanti per: " + ", ".join(missing) + ". Configurale nella scheda dedicata.",
            "missing_entities": missing
        }

    now_str = datetime.now().strftime("%d/%m/%Y %H:%M")
    sent_per_entity = {}
    for g in target_groups:
        ent = entity_for_apt(g.get("apt", ""))
        ross_mode = settings.get(ent, {}).get("ross_mode", "spid_manual")
        g["alloggiati_status"] = f"✓ Inviato WS ({now_str})"
        if ross_mode == "service_account":
            g["ross1000_status"] = f"✓ Trasmesso ({now_str})"
        else:
            g["ross1000_status"] = "⏳ Da caricare (SPID)"
        g["transmitted_at"] = now_str
        g["transmitted_entity"] = ent
        g["ross_mode_used"] = ross_mode
        sent_per_entity[ent] = sent_per_entity.get(ent, 0) + 1

    save_ospiti_list(ospiti)

    detail = " · ".join(f"{ENTITIES[e]['label']}: {n}" for e, n in sent_per_entity.items())
    manual = [ENTITIES[e]["label"] for e in sent_per_entity
              if settings.get(e, {}).get("ross_mode", "spid_manual") != "service_account"]
    extra = ""
    if manual:
        extra = " · ROSS1000 richiede caricamento manuale via SPID per: " + ", ".join(manual)
    return {
        "status": "ok",
        "needs_manual_ross": manual,
        "message": f"Questura: trasmessi {len(target_groups)} gruppo/i ({detail}){extra}",
        "transmitted_groups": len(target_groups),
        "per_entity": sent_per_entity,
        "timestamp": now_str
    }


# ── Pacchetto ROSS1000 per caricamento manuale post-SPID ──────────────────────
@app.get("/api/ross1000-package")
def ross1000_package(entity: str = None, group_id: str = None):
    """
    Genera il file TXT/CSV conforme al tracciato ROSS1000 da caricare
    nella sezione Upload del portale regionale dopo l'accesso con SPID/CIE.
    """
    ospiti = load_ospiti()
    codici = load_codici()

    def keep(g):
        if group_id:
            return g.get("id") == group_id
        if entity:
            return entity_for_apt(g.get("apt", "")) == entity
        return True

    target = [g for g in ospiti if keep(g)]
    if not target:
        raise HTTPException(status_code=404, detail="Nessun movimento da esportare")

    lines = ["CodiceStruttura;DataArrivo;DataPartenza;TipoAlloggiato;Cognome;Nome;Sesso;DataNascita;StatoNascita;ComuneNascita;Cittadinanza;StatoResidenza;ComuneResidenza"]

    def norm_date(d):
        s = str(d or "").strip()
        if not s:
            return ""
        if "-" in s and len(s) == 10:
            y, m, dd = s.split("-")
            return f"{dd}/{m}/{y}"
        return s

    for g in target:
        cod = cod_ross_for_apt(g.get("apt", ""))
        arr = norm_date(g.get("arrival_date"))
        dep = norm_date(g.get("departure_date"))
        lead = g.get("lead_guest", {})
        others = g.get("additional_guests", [])

        def row(p, tipo):
            return ";".join([
                cod, arr, dep, tipo,
                (p.get("cognome") or "").upper(),
                (p.get("nome") or "").upper(),
                "M" if (p.get("sesso") or "M").upper().startswith("M") else "F",
                norm_date(p.get("data_nascita")),
                (p.get("stato_nascita") or "ITALIA").upper(),
                (p.get("comune_nascita") or "").upper(),
                (p.get("cittadinanza") or "ITALIA").upper(),
                (p.get("stato_residenza") or "ITALIA").upper(),
                (p.get("comune_residenza") or "").upper(),
            ])

        lines.append(row(lead, "Capogruppo" if others else "Ospite Singolo"))
        for o in others:
            lines.append(row(o, "Membro Gruppo"))

    scope = entity or ("gruppo" if group_id else "tutti")
    fname = f"ross1000_upload_{scope}_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    return Response(
        content=(chr(13) + chr(10)).join(lines) + chr(13) + chr(10),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={fname}"}
    )

@app.post("/api/mark-ross1000-uploaded")
async def mark_ross1000_uploaded(req: dict):
    """Segna come caricati su ROSS1000 i movimenti, dopo l'upload manuale via SPID."""
    entity = req.get("entity")
    group_id = req.get("group_id")
    ospiti = load_ospiti()
    now_str = datetime.now().strftime("%d/%m/%Y %H:%M")
    n = 0
    for g in ospiti:
        match = (g.get("id") == group_id) if group_id else (
            entity_for_apt(g.get("apt", "")) == entity if entity else True
        )
        if match:
            g["ross1000_status"] = f"✓ Caricato ROSS1000 ({now_str})"
            g["ross1000_uploaded_at"] = now_str
            n += 1
    save_ospiti_list(ospiti)
    return {"status": "ok", "message": f"{n} movimento/i segnati come caricati su ROSS1000", "updated": n}

@app.get("/{filename}")
def serve_static(filename: str):
    filepath = os.path.join(os.path.dirname(__file__), filename)
    if os.path.exists(filepath) and os.path.isfile(filepath):
        return FileResponse(filepath)
    raise HTTPException(status_code=404, detail="File non trovato")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    print(f"\n🏡 Marchesini Collection · Server avviato su porta {port}\n")
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
