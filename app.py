import os, io, re, hashlib, sqlite3, datetime, pytz
from pathlib import Path
from typing import Optional, Tuple

from fastapi import FastAPI, UploadFile, File, Form, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from PIL import Image, ExifTags
try:
    import pytesseract
    OCR_AVAILABLE = False   # OCR desativado para estabilidade no Windows
except Exception:
    OCR_AVAILABLE = False

APP_TZ = pytz.timezone("America/Sao_Paulo")
BASE_DIR = Path(__file__).resolve().parent
DB_PATH  = BASE_DIR / "dc.db"
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

app = FastAPI(title="DC Dashboard (MVP)")

# monta /static apenas se a pasta existir (evita erro no Windows)
static_dir = BASE_DIR / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# --- rotas de diagnóstico ---
@app.get("/__ping")
def __ping():
    return {"ok": True}

@app.get("/__diag")
def __diag():
    try:
        con = sqlite3.connect(str(DB_PATH))
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM photos;")
        total = cur.fetchone()[0]
        con.close()
        return {"db_ok": True, "total_photos": total}
    except Exception as e:
        return JSONResponse({"db_ok": False, "error": str(e)}, status_code=500)

# --- funções utilitárias ---
def db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    con = db()
    cur = con.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS photos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        taken_at TEXT NOT NULL,
        filename TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        width INTEGER,
        height INTEGER,
        exif_json TEXT,
        ocr_temp_c REAL,
        ocr_humidity REAL,
        device_model TEXT
    );
    """)
    con.commit()
    con.close()

init_db()

def _exif_to_dict(img: Image.Image):
    exif = getattr(img, "_getexif", lambda: None)()
    if not exif:
        return {}
    res = {}
    for k, v in exif.items():
        tag = ExifTags.TAGS.get(k, k)
        try:
            if isinstance(v, bytes):
                v = v.decode(errors="ignore")
        except Exception:
            pass
        res[str(tag)] = str(v)
    return res

def _parse_numbers(text: str):
    text = text.replace(",", ".")
    temp = None
    hum  = None
    mtemp = re.findall(r"(?:t|temp|temperatura)?\s*[:=]?\s*(-?\d{1,2}(?:\.\d{1,2})?)", text, flags=re.I)
    if mtemp:
        try: temp = float(mtemp[0])
        except: pass
    mhum = re.findall(r"(\d{1,2}(?:\.\d{1,2})?)\s*%", text)
    if mhum:
        try: hum = float(mhum[0])
        except: pass
    if temp is None or hum is None:
        nums = re.findall(r"-?\d{1,2}(?:\.\d{1,2})?", text)
        if nums:
            if temp is None:
                try: temp = float(nums[0])
                except: pass
            if len(nums) >= 2 and hum is None:
                try: hum = float(nums[1])
                except: pass
    return temp, hum

def run_ocr(img: Image.Image):
    if not OCR_AVAILABLE:
        return None, None
    gray = img.convert("L")
    bw = gray.point(lambda x: 255 if x > 150 else 0, mode="1")
    text = None
    try:
        text = pytesseract.image_to_string(bw)
    except Exception:
        return None, None
    return _parse_numbers(text)

# --- rotas principais ---

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    try:
        con = db()
        cur = con.cursor()

        cur.execute("SELECT * FROM photos ORDER BY taken_at DESC LIMIT 1;")
        last = cur.fetchone()

        cur.execute("""
            SELECT taken_at, ocr_temp_c, ocr_humidity
            FROM photos
            WHERE ocr_temp_c IS NOT NULL OR ocr_humidity IS NOT NULL
            ORDER BY taken_at ASC;
        """)
        series = cur.fetchall()

        cur.execute("SELECT * FROM photos ORDER BY taken_at DESC LIMIT 20;")
        table = cur.fetchall()

        con.close()
    except Exception as e:
        # Mostra erro na tela se algo falhar
        return HTMLResponse(f"<pre>Erro ao montar dashboard:\n{e!r}</pre>", status_code=500)

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "last": last,
        "series": series,
        "table": table
    })

@app.get("/upload", response_class=HTMLResponse)
def upload_form(request: Request):
    return templates.TemplateResponse("upload.html", {"request": request})

@app.post("/upload")
async def upload_photo(request: Request, photo: UploadFile = File(...)):
    if not photo.content_type.startswith("image/"):
        raise HTTPException(400, "Envie uma imagem")

    raw = await photo.read()
    sha = hashlib.sha256(raw).hexdigest()
    now = datetime.datetime.now(APP_TZ)
    ts = now.strftime("%Y%m%d_%H%M%S")
    fname = f"{ts}_{sha[:8]}.jpg"
    path = UPLOAD_DIR / fname

    img = Image.open(io.BytesIO(raw)).convert("RGB")
    exif = _exif_to_dict(img)
    device_model = exif.get("Model") or exif.get("Make") or ""

    ocr_temp, ocr_hum = run_ocr(img)
    img.save(str(path), format="JPEG", quality=90)
    width, height = img.width, img.height

    con = db()
    cur = con.cursor()
    cur.execute("""
        INSERT INTO photos (taken_at, filename, sha256, width, height, exif_json, ocr_temp_c, ocr_humidity, device_model)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        now.isoformat(),
        fname,
        sha,
        width,
        height,
        str(exif) if exif else None,
        ocr_temp,
        ocr_hum,
        device_model
    ))
    con.commit()
    con.close()

    return RedirectResponse(url="/", status_code=303)

@app.get("/img/{filename}")
def get_image(filename: str):
    from fastapi.responses import FileResponse
    filepath = UPLOAD_DIR / filename
    if not filepath.exists():
        raise HTTPException(404, "Imagem não encontrada")
    return FileResponse(str(filepath), media_type="image/jpeg")
