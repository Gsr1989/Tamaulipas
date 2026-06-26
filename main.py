from fastapi import FastAPI, Request, HTTPException, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from supabase import create_client, Client
import os
from aiogram import Bot, Dispatcher, types
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command
from aiogram.types import FSInputFile, ContentType, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from contextlib import asynccontextmanager, suppress
import asyncio
import aiohttp
import random
from io import BytesIO
import qrcode
import fitz
from starlette.middleware.sessions import SessionMiddleware

# ===================== CONFIG =====================
BOT_TOKEN     = os.getenv("BOT_TOKEN", "")
SUPABASE_URL  = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY  = os.getenv("SUPABASE_KEY", "")
BASE_URL      = os.getenv("BASE_URL", "https://sanfernando-transito-gob.onrender.com").rstrip("/")
OUTPUT_DIR    = "documentos"
PLANTILLA_PDF = "sanfernando_permiso.pdf"
ENTIDAD       = "sanfernando"
PRECIO_PERMISO = 180
TZ            = "America/Mexico_City"

ADMIN_USER = "Serg890105tm3"
ADMIN_PASS = "Serg890105tm3"

TEMPLATES_DIR = "templates"
STATIC_DIR    = "static"
BUCKET_NAME   = "permisos-sanfernando"

os.makedirs(TEMPLATES_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR,    exist_ok=True)
os.makedirs(STATIC_DIR,    exist_ok=True)

templates = Jinja2Templates(directory=TEMPLATES_DIR)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

_bot_session = AiohttpSession(timeout=aiohttp.ClientTimeout(total=300))
bot     = Bot(token=BOT_TOKEN, session=_bot_session)
storage = MemoryStorage()
dp      = Dispatcher(storage=storage)

# ===================== TIMERS =====================
timers_activos       = {}
user_folios          = {}
pending_comprobantes = {}
TOTAL_MINUTOS_TIMER  = 36 * 60

async def eliminar_folio_automatico(folio: str):
    try:
        uid = timers_activos[folio]["user_id"] if folio in timers_activos else None
        await asyncio.to_thread(lambda:
            supabase.table("folios_registrados").delete().eq("folio", folio).execute()
        )
        try:
            supabase.storage.from_(BUCKET_NAME).remove([f"{folio}.pdf"])
        except Exception:
            pass
        if uid:
            await bot.send_message(uid,
                f"⏰ TIEMPO AGOTADO - SAN FERNANDO\n\n"
                f"El folio {folio} fue eliminado por no completar el pago en 36 horas.\n\n"
                f"📋 Use /banamex para generar otro permiso.")
        limpiar_timer_folio(folio)
    except Exception as e:
        print(f"[ERROR] eliminando folio {folio}: {e}")

async def enviar_recordatorio(folio: str, minutos_restantes: int):
    try:
        if folio not in timers_activos: return
        uid = timers_activos[folio]["user_id"]
        await bot.send_message(uid,
            f"⚡ RECORDATORIO - SAN FERNANDO\n\n"
            f"Folio: {folio}\n"
            f"Tiempo restante: {minutos_restantes} minutos\n"
            f"Monto: ${PRECIO_PERMISO}\n\n"
            f"📸 Envíe su comprobante de pago.\n\n"
            f"📋 Use /banamex para generar otro permiso.")
    except Exception as e:
        print(f"[ERROR] recordatorio {folio}: {e}")

async def iniciar_timer_36h(user_id: int, folio: str, nombre: str = ""):
    async def timer_task():
        print(f"[TIMER] Iniciado folio {folio} (36h)")
        await asyncio.sleep(34.5 * 3600)
        if folio not in timers_activos: return
        await enviar_recordatorio(folio, 90)
        await asyncio.sleep(30 * 60)
        if folio not in timers_activos: return
        await enviar_recordatorio(folio, 60)
        await asyncio.sleep(30 * 60)
        if folio not in timers_activos: return
        await enviar_recordatorio(folio, 30)
        await asyncio.sleep(20 * 60)
        if folio not in timers_activos: return
        await enviar_recordatorio(folio, 10)
        await asyncio.sleep(10 * 60)
        if folio in timers_activos:
            print(f"[TIMER] Expirado {folio} — eliminando")
            await eliminar_folio_automatico(folio)

    task = asyncio.create_task(timer_task())
    timers_activos[folio] = {
        "task":       task,
        "user_id":    user_id,
        "start_time": datetime.now(),
        "nombre":     nombre,
    }
    user_folios.setdefault(user_id, []).append(folio)
    print(f"[SISTEMA] Timer 36h iniciado folio {folio} ({nombre})")

def cancelar_timer_folio(folio: str) -> bool:
    if folio not in timers_activos: return False
    timers_activos[folio]["task"].cancel()
    uid = timers_activos[folio]["user_id"]
    del timers_activos[folio]
    if uid in user_folios and folio in user_folios[uid]:
        user_folios[uid].remove(folio)
        if not user_folios[uid]: del user_folios[uid]
    print(f"[SISTEMA] Timer cancelado folio {folio}")
    return True

def limpiar_timer_folio(folio: str):
    if folio not in timers_activos: return
    uid = timers_activos[folio]["user_id"]
    del timers_activos[folio]
    if uid in user_folios and folio in user_folios[uid]:
        user_folios[uid].remove(folio)
        if not user_folios[uid]: del user_folios[uid]

def obtener_folios_usuario(user_id: int) -> list:
    return user_folios.get(user_id, [])

# ===================== FOLIOS — WATERMARK =====================
FOLIO_PREFIJO  = "SFT"
FOLIO_NUM_PREF = "780"
_folio_counter = {"siguiente": 1}
_folio_lock    = asyncio.Lock()

def _sb_leer_watermark() -> int | None:
    try:
        r = supabase.table("folio_watermark") \
            .select("ultimo_asignado").eq("prefijo", FOLIO_PREFIJO).execute()
        return r.data[0]["ultimo_asignado"] if r.data else None
    except Exception as e:
        print(f"[ERROR] leer_watermark: {e}")
        return None

def _sb_guardar_watermark(numero: int):
    try:
        supabase.table("folio_watermark").upsert({
            "prefijo": FOLIO_PREFIJO, "ultimo_asignado": numero
        }).execute()
    except Exception as e:
        print(f"[ERROR] guardar_watermark: {e}")

def _sb_inicializar_folio():
    wm = _sb_leer_watermark()
    if wm is not None:
        _folio_counter["siguiente"] = wm + 1
        print(f"[FOLIO] Desde watermark: {FOLIO_NUM_PREF}{wm} -> siguiente: {_folio_counter['siguiente']}")
        return
    try:
        resp = supabase.table("folios_registrados") \
            .select("folio").eq("entidad", ENTIDAD) \
            .like("folio", f"{FOLIO_NUM_PREF}%").execute()
        nums = []
        for row in resp.data or []:
            f = row.get("folio", "")
            if isinstance(f, str) and f.startswith(FOLIO_NUM_PREF):
                suf = f[len(FOLIO_NUM_PREF):]
                if suf.isdigit():
                    nums.append(int(suf))
        if nums:
            maximo = max(nums)
            _folio_counter["siguiente"] = maximo + 1
            _sb_guardar_watermark(maximo)
        else:
            _folio_counter["siguiente"] = 1
            print(f"[FOLIO] Sin folios previos, empezando desde {FOLIO_NUM_PREF}1")
    except Exception as e:
        print(f"[ERROR] inicializar_folio: {e}")

def _folio_existe(folio: str) -> bool:
    try:
        r = supabase.table("folios_registrados").select("folio").eq("folio", folio).execute()
        return len(r.data) > 0
    except Exception as e:
        print(f"[ERROR] verificar folio {folio}: {e}")
        return False

def _generar_folio_sync() -> str:
    candidato = _folio_counter["siguiente"]
    for _ in range(100_000):
        folio = f"{FOLIO_NUM_PREF}{candidato}"
        if not _folio_existe(folio):
            _folio_counter["siguiente"] = candidato + 1
            _sb_guardar_watermark(candidato)
            print(f"[FOLIO] Asignado: {folio}")
            return folio
        candidato += 1
    return f"{FOLIO_NUM_PREF}{random.randint(50000, 99999)}"

async def _generar_folio_async() -> str:
    async with _folio_lock:
        return await asyncio.to_thread(_generar_folio_sync)

def generar_folio() -> str:
    return _generar_folio_sync()

# ===================== STORAGE =====================
def subir_pdf_a_storage(ruta_local: str, folio: str) -> str:
    try:
        with open(ruta_local, "rb") as f:
            contenido = f.read()
        nombre = f"{folio}.pdf"
        supabase.storage.from_(BUCKET_NAME).upload(
            path=nombre, file=contenido,
            file_options={"content-type": "application/pdf", "upsert": "true"}
        )
        url = supabase.storage.from_(BUCKET_NAME).get_public_url(nombre)
        print(f"[STORAGE] Subido: {url}")
        return url
    except Exception as e:
        print(f"[STORAGE] Error {folio}: {e}")
        return ""

# ===================== QR / PDF =====================
def generar_qr(folio: str):
    try:
        url = f"{BASE_URL}/consulta/{folio}"
        qr  = qrcode.QRCode(version=2,
                             error_correction=qrcode.constants.ERROR_CORRECT_M,
                             box_size=4, border=1)
        qr.add_data(url); qr.make(fit=True)
        return qr.make_image(fill_color="black", back_color="white").convert("RGB"), url
    except Exception as e:
        print(f"[QR] Error: {e}")
        return None, None


# ===================== QR / PDF =====================
def generar_qr(folio: str):
    try:
        url = f"{BASE_URL}/consulta/{folio}"
        qr  = qrcode.QRCode(version=2,
                             error_correction=qrcode.constants.ERROR_CORRECT_M,
                             box_size=4, border=1)
        qr.add_data(url); qr.make(fit=True)
        return qr.make_image(fill_color="black", back_color="white").convert("RGB"), url
    except Exception as e:
        print(f"[QR] Error: {e}")
        return None, None

def generar_pdf(datos: dict) -> str:
    """
    Llena la plantilla Sanfer.pdf con los datos del permiso.
    Hoja carta vertical (612 x 792 pts).

    Campos de la plantilla:
    ─────────────────────────────────────────────────────────────
    FOLIO             → arriba derecha, en ROJO
    Fecha             → CD. SAN FERNANDO, TAM. A [dia] DE [mes] DEL [año]
    Titular (AL C.)   → nombre completo
    Domicilio línea 1 → Calle Hidalgo Sin Número, entre Calle Juárez y Calle Escandón
    Domicilio línea 2 → Zona Centro, C.P 87600. San Fernando, Tamps.
    Marca             → AL VEHÍCULO MARCA:
    Tipo/Línea        → TIPO:
    Color             → COLOR:
    Modelo/Año        → MODELO:
    Núm. Serie        → NUMERO DE SERIE:
    QR                → esquina inferior derecha
    """
    folio = datos["folio"]
    out   = os.path.join(OUTPUT_DIR, f"{folio}.pdf")

    # Parsear fecha de expedición
    tz  = ZoneInfo(TZ)
    try:
        fecha_dt = datos["fecha_exp_dt"]
        if isinstance(fecha_dt, str):
            fecha_dt = datetime.fromisoformat(fecha_dt.replace("Z", "+00:00"))
        if fecha_dt.tzinfo is None:
            fecha_dt = fecha_dt.replace(tzinfo=tz)
        else:
            fecha_dt = fecha_dt.astimezone(tz)
    except Exception:
        fecha_dt = datetime.now(tz)

    meses = {
        1:"ENERO", 2:"FEBRERO", 3:"MARZO", 4:"ABRIL", 5:"MAYO", 6:"JUNIO",
        7:"JULIO", 8:"AGOSTO", 9:"SEPTIEMBRE", 10:"OCTUBRE", 11:"NOVIEMBRE", 12:"DICIEMBRE"
    }
    dia  = str(fecha_dt.day)
    mes  = meses[fecha_dt.month]
    anio = str(fecha_dt.year)

    DOMICILIO_1 = "Calle Hidalgo Sin Número, entre Calle Juárez y Calle Escandón"
    DOMICILIO_2 = "Zona Centro, C.P 87600. San Fernando, Tamps."

    try:
        plantilla = "Sanfer.pdf"
        if os.path.exists(plantilla):
            doc = fitz.open(plantilla)
            pg  = doc[0]

            # ── FOLIO en rojo, arriba a la derecha ──
            pg.insert_text((500, 95), str(folio),
                           fontsize=13, fontname="hebo", color=(0.8, 0, 0))

            # ── FECHA: CD. SAN FERNANDO, TAM. A ___ DE ___ DEL ___ ──
            pg.insert_text((169, 123), dia,  fontsize=10, fontname="helv", color=(0,0,0))
            pg.insert_text((228, 123), mes,  fontsize=10, fontname="helv", color=(0,0,0))
            pg.insert_text((346, 123), anio, fontsize=10, fontname="helv", color=(0,0,0))

            # ── TITULAR (AL C.) ──
            pg.insert_text((84, 188), str(datos.get("nombre", "")).upper(),
                           fontsize=10, fontname="helv", color=(0,0,0))

            # ── DOMICILIO línea 1 ──
            pg.insert_text((264, 213), DOMICILIO_1,
                           fontsize=10, fontname="helv", color=(0,0,0))

            # ── DOMICILIO línea 2 ──
            pg.insert_text((260, 226), DOMICILIO_2,
                           fontsize=10, fontname="helv", color=(0,0,0))

            # ── VEHÍCULO ──
            pg.insert_text((268, 239), str(datos.get("marca", "")).upper(),
                           fontsize=10, fontname="helv", color=(0,0,0))
            pg.insert_text((240, 252), str(datos.get("linea", "")).upper(),
                           fontsize=10, fontname="helv", color=(0,0,0))
            pg.insert_text((240, 265), str(datos.get("color", "")).upper(),
                           fontsize=10, fontname="helv", color=(0,0,0))
            pg.insert_text((240, 278), str(datos.get("anio", "")),
                           fontsize=10, fontname="helv", color=(0,0,0))
            pg.insert_text((260, 290), str(datos.get("serie", "")).upper(),
                           fontsize=10, fontname="helv", color=(0,0,0))

            # ── QR en esquina inferior derecha ──
            img_qr, _ = generar_qr(folio)
            if img_qr:
                buf = BytesIO()
                img_qr.save(buf, format="PNG")
                buf.seek(0)
                pg.insert_image(
                    fitz.Rect(460, 480, 360, 580),
                    pixmap=fitz.Pixmap(buf.read()),
                    overlay=True
                )

        else:
            # Fallback si no existe la plantilla: PDF en blanco con datos
            print(f"[PDF] ⚠️  Sanfer.pdf no encontrado, generando PDF básico")
            doc = fitz.open()
            pg  = doc.new_page(width=612, height=792)

            # Header básico
            pg.insert_text((50, 60),
                "MUNICIPIO DE SAN FERNANDO TAMAULIPAS",
                fontsize=13, fontname="hebo", color=(0,0,0))
            pg.insert_text((50, 76),
                "SECRETARÍA DE SEGURIDAD PÚBLICA",
                fontsize=11, fontname="helv", color=(0,0,0))
            pg.insert_text((50, 91),
                "DIRECCIÓN DE TRÁNSITO Y VIALIDAD",
                fontsize=11, fontname="helv", color=(0,0,0))

            # Línea roja simulada
            pg.draw_rect(fitz.Rect(40, 100, 572, 102), color=(0.55,0.12,0.23), fill=(0.55,0.12,0.23))

            pg.insert_text((170, 125),
                "PERMISO DE CIRCULACIÓN",
                fontsize=13, fontname="hebo", color=(0,0,0))

            # Folio en rojo
            pg.insert_text((470, 125), str(folio),
                           fontsize=13, fontname="hebo", color=(0.8, 0, 0))

            # Fecha
            pg.insert_text((50, 160),
                f"CD. SAN FERNANDO, TAM. A  {dia}  DE  {mes}  DEL  {anio}",
                fontsize=10, fontname="helv", color=(0,0,0))

            # Texto permiso
            pg.insert_text((50, 185),
                "ESTE R. AYUNTAMIENTO CONCEDE PERMISO PROVISIONAL POR EL TÉRMINO DE  TREINTA",
                fontsize=9, fontname="helv", color=(0,0,0))
            pg.insert_text((50, 200),
                "DÍAS A PARTIR DE LAS FECHAS PARA CIRCULAR SIN  PLACAS",
                fontsize=9, fontname="helv", color=(0,0,0))

            # AL C.
            pg.insert_text((50, 230),
                f"AL C.  {str(datos.get('nombre','')).upper()}",
                fontsize=10, fontname="helv", color=(0,0,0))

            # Domicilio
            pg.insert_text((230, 270),
                f"CON DOMICILIO EN:  {DOMICILIO_1}",
                fontsize=9, fontname="helv", color=(0,0,0))
            pg.insert_text((230, 283),
                f"                          {DOMICILIO_2}",
                fontsize=9, fontname="helv", color=(0,0,0))

            # Vehículo
            pg.insert_text((230, 308),
                f"AL VEHÍCULO MARCA:  {str(datos.get('marca','')).upper()}",
                fontsize=9, fontname="helv", color=(0,0,0))
            pg.insert_text((230, 323),
                f"TIPO:  {str(datos.get('linea','')).upper()}",
                fontsize=9, fontname="helv", color=(0,0,0))
            pg.insert_text((230, 338),
                f"COLOR:  {str(datos.get('color','')).upper()}",
                fontsize=9, fontname="helv", color=(0,0,0))
            pg.insert_text((230, 353),
                f"MODELO:  {str(datos.get('anio',''))}",
                fontsize=9, fontname="helv", color=(0,0,0))
            pg.insert_text((230, 368),
                f"NUMERO DE SERIE:  {str(datos.get('serie','')).upper()}",
                fontsize=9, fontname="helv", color=(0,0,0))

            # QR
            img_qr, _ = generar_qr(folio)
            if img_qr:
                buf = BytesIO()
                img_qr.save(buf, format="PNG")
                buf.seek(0)
                pg.insert_image(
                    fitz.Rect(460, 480, 560, 580),
                    pixmap=fitz.Pixmap(buf.read()),
                    overlay=True
                )

        doc.save(out)
        doc.close()
        print(f"[PDF] ✅ {out}")

    except Exception as e:
        print(f"[PDF] Error: {e}")
        doc_fb = fitz.open()
        doc_fb.new_page().insert_text((50, 50), f"ERROR - Folio: {folio}", fontsize=12)
        doc_fb.save(out)
        doc_fb.close()

    # Subir a Storage
    url = subir_pdf_a_storage(out, folio)
    if url:
        try:
            supabase.table("folios_registrados") \
                .update({"pdf_url": url}).eq("folio", folio).execute()
        except Exception as e:
            print(f"[WARN] pdf_url: {e}")
    return out


# ===================== CONSULTA PÚBLICA =====================
async def callback_detener(callback: CallbackQuery):
    folio = callback.data.replace("detener_", "")
    if folio in timers_activos:
        nombre = timers_activos[folio].get("nombre", "")
        cancelar_timer_folio(folio)
        with suppress(Exception):
            await asyncio.to_thread(lambda: supabase.table("folios_registrados").update({
                "estado": "TIMER_DETENIDO"
            }).eq("folio", folio).execute())
        await callback.answer("⏹️ Timer detenido", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            f"⏹️ TIMER DETENIDO\nFolio: {folio}\nTitular: {nombre}\n\n"
            f"El folio ya NO se eliminará automáticamente.\n\n📋 Use /banamex para otro permiso.")
    else:
        await callback.answer("❌ Timer ya no está activo", show_alert=True)

@dp.message(lambda m: m.text and m.text.strip().upper().startswith("SERO"))
async def codigo_admin(message: types.Message):
    texto = message.text.strip().upper()
    folio = texto.replace("SERO", "", 1).strip()
    if not folio or not folio.startswith(FOLIO_NUM_PREF):
        await message.answer(
            f"⚠️ Formato: SERO{FOLIO_NUM_PREF}X (folio debe iniciar con {FOLIO_NUM_PREF}).\n\n"
            f"📋 Use /banamex para generar otro permiso."); return
    cancelado = cancelar_timer_folio(folio)
    with suppress(Exception):
        await asyncio.to_thread(lambda: supabase.table("folios_registrados").update({
            "estado_pago": "VALIDADO", "fecha_comprobante": datetime.now().isoformat()
        }).eq("folio", folio).execute())
    msg = (f"✅ Validación admin exitosa\nFolio: {folio}\n⏹️ Timer cancelado"
           if cancelado else
           f"✅ Validación admin\nFolio: {folio}\n⚠️ Timer ya estaba inactivo")
    await message.answer(msg + "\n\n📋 Use /banamex para generar otro permiso.")

@dp.message(lambda m: m.content_type == ContentType.PHOTO)
async def recibir_comprobante(message: types.Message):
    uid    = message.from_user.id
    folios = obtener_folios_usuario(uid)
    if not folios:
        await message.answer(
            "ℹ️ No tienes folios pendientes.\n\n📋 Use /banamex para generar un permiso."); return
    if len(folios) > 1:
        lista = "\n".join(f"• {f}" for f in folios)
        pending_comprobantes[uid] = "waiting_folio"
        await message.answer(
            f"📄 Varios folios activos:\n\n{lista}\n\n"
            f"Responde con el NÚMERO DE FOLIO para este comprobante.\n\n"
            f"📋 Use /banamex para generar otro permiso."); return
    folio = folios[0]
    cancelar_timer_folio(folio)
    with suppress(Exception):
        await asyncio.to_thread(lambda: supabase.table("folios_registrados").update({
            "estado": "COMPROBANTE_ENVIADO", "fecha_comprobante": datetime.now().isoformat()
        }).eq("folio", folio).execute())
    await message.answer(
        f"✅ Comprobante recibido\nFolio: {folio}\n⏹️ Timer detenido.\n\n"
        f"📋 Use /banamex para generar otro permiso.")

@dp.message(lambda m: m.from_user.id in pending_comprobantes
            and pending_comprobantes[m.from_user.id] == "waiting_folio")
async def especificar_folio_comprobante(message: types.Message):
    uid = message.from_user.id
    fe  = message.text.strip().upper()
    fl  = obtener_folios_usuario(uid)
    if fe not in fl:
        await message.answer("❌ Folio no en tu lista.\n\n📋 Use /banamex para otro permiso."); return
    cancelar_timer_folio(fe)
    del pending_comprobantes[uid]
    with suppress(Exception):
        await asyncio.to_thread(lambda: supabase.table("folios_registrados").update({
            "estado": "COMPROBANTE_ENVIADO", "fecha_comprobante": datetime.now().isoformat()
        }).eq("folio", fe).execute())
    await message.answer(
        f"✅ Comprobante asociado.\nFolio: {fe}\n\n📋 Use /banamex para otro permiso.")

@dp.message(Command("folios"))
async def ver_folios_activos(message: types.Message):
    uid    = message.from_user.id
    folios = obtener_folios_usuario(uid)
    if not folios:
        await message.answer("ℹ️ No hay folios activos.\n\n📋 Use /banamex para generar uno."); return
    lista   = []
    botones = []
    for f in folios:
        if f in timers_activos:
            seg  = max(0, int(TOTAL_MINUTOS_TIMER * 60 -
                (datetime.now() - timers_activos[f]["start_time"]).total_seconds()))
            h, m = divmod(seg // 60, 60)
            nombre = timers_activos[f].get("nombre", "")
            lista.append(f"• {f} — {nombre}\n  {h}h {m}min restantes")
        else:
            lista.append(f"• {f} (sin timer)")
        botones.append([InlineKeyboardButton(
            text=f"⏹️ Detener {f}", callback_data=f"detener_{f}")])
    await message.answer(
        f"📋 FOLIOS ACTIVOS ({len(folios)})\n\n" + "\n\n".join(lista) +
        "\n\n⏰ Timer 36h por folio.\n📋 Use /banamex para otro permiso.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=botones))

# ===================== FASTAPI LIFESPAN =====================
_keep_task = None

async def keep_alive():
    while True:
        await asyncio.sleep(600)
        print("[HEARTBEAT] Sistema San Fernando activo")

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _keep_task
    await asyncio.to_thread(_sb_inicializar_folio)
    await bot.delete_webhook(drop_pending_updates=True)
    webhook_url = f"{BASE_URL}/webhook"
    await bot.set_webhook(webhook_url, allowed_updates=["message", "callback_query"])
    _keep_task = asyncio.create_task(keep_alive())
    print(f"[WEBHOOK] {webhook_url}")
    print(f"[SISTEMA] San Fernando v1.0 listo — siguiente folio: {FOLIO_NUM_PREF}{_folio_counter['siguiente']}")
    yield
    if _keep_task:
        _keep_task.cancel()
        with suppress(asyncio.CancelledError): await _keep_task
    await bot.session.close()

app = FastAPI(lifespan=lifespan, title="Tránsito San Fernando", version="1.0")
app.add_middleware(SessionMiddleware, secret_key="sanfernando_clave_super_segura_123456")

try:
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
except Exception:
    pass

# ===================== WEBHOOK =====================
@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        await dp.feed_webhook_update(bot, types.Update(**data))
        return {"ok": True}
    except Exception as e:
        print(f"[WEBHOOK] Error: {e}")
        return {"ok": False, "error": str(e)}




# ===================== HELPER — RENDER CON DISEÑO SAN FERNANDO =====================


# ===================== TABLA BD DISPONIBLES =====================
TABLAS_DISPONIBLES = {
    "folios_registrados": {
        "nombre":   "Folios Registrados",
        "pk_col":   "folio",
        "columnas": ["folio","marca","linea","anio","numero_serie","numero_motor",
                     "color","nombre","fecha_expedicion","fecha_vencimiento",
                     "entidad","estado","estado_pago","creado_por"],
    },
    "verificacion_sanfernando": {
        "nombre":   "Usuarios del Sistema",
        "pk_col":   "id",
        "columnas": ["id","username","password","folios_asignac","folios_usados"],
    },
}

PAGE_SIZE = 100

# ===================== HELPERS HTML — DISEÑO SAN FERNANDO =====================

def _sf_head(titulo: str, extra_css: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{titulo} - San Fernando, Tamaulipas</title>
<link rel="icon" href="https://sanfernando.gob.mx/wp-content/uploads/sites/36/2026/04/cropped-logo-secundario-vertical-32x32.png" sizes="32x32"/>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/css/bootstrap.min.css" rel="stylesheet">
<link href="https://fonts.googleapis.com/css2?family=Encode+Sans:wght@400;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.2/css/all.min.css"/>
<link rel="stylesheet" href="https://sanfernando.gob.mx/wp-content/themes/municipios-tamaulipas/assets/css/estilos.css">
<style>
:root{{--primario:#8b1f3a !important;--font:'Encode Sans',sans-serif;}}
*{{font-family:var(--font);}}
.btn-primary{{--bs-btn-bg:#8b1f3a !important;--bs-btn-border-color:#8b1f3a !important;--bs-btn-hover-bg:#700c26 !important;--bs-btn-color:#fff;--bs-btn-hover-color:#fff;}}
.logo-home{{display:flex !important;}}
.logo-home a picture img{{display:block !important;visibility:visible !important;max-height:65px !important;width:auto !important;}}
.admin-bar{{background:#8b1f3a;color:white;padding:10px 20px;display:flex;align-items:center;justify-content:space-between;font-weight:700;font-size:14px;}}
.admin-bar a{{color:rgba(255,255,255,.85);text-decoration:none;font-size:13px;}}
.admin-bar a:hover{{color:white;}}
.admin-content{{padding:20px;max-width:960px;margin:0 auto;}}
.stat-card{{background:white;border-radius:10px;padding:18px;text-align:center;box-shadow:0 2px 8px rgba(0,0,0,.08);}}
.stat-num{{font-size:32px;font-weight:700;color:#8b1f3a;}}
.stat-lbl{{font-size:11px;color:#666;font-weight:700;text-transform:uppercase;margin-top:4px;}}
.menu-btn{{background:white;border:1.5px solid #e0e0e0;border-radius:10px;padding:18px;text-align:center;text-decoration:none;color:#1d1d1b;transition:.2s;display:block;}}
.menu-btn:hover{{border-color:#8b1f3a;color:#8b1f3a;transform:translateY(-2px);box-shadow:0 4px 12px rgba(139,31,58,.15);}}
.menu-btn i{{font-size:26px;display:block;margin-bottom:7px;color:#8b1f3a;}}
.menu-btn.danger{{border-color:#dc3545;}} .menu-btn.danger i{{color:#dc3545;}} .menu-btn.danger:hover{{color:#dc3545;}}
table{{font-size:13px;width:100%;border-collapse:collapse;}}
thead th{{background:#8b1f3a;color:white;white-space:nowrap;padding:9px 10px;border:none;}}
tbody td{{padding:8px 10px;vertical-align:middle;border-bottom:1px solid #eee;}}
tbody tr:last-child td{{border-bottom:none;}} tbody tr:hover td{{background:#fef9f9;}}
.tabla-wrap{{overflow-x:auto;background:white;border-radius:10px;box-shadow:0 2px 8px rgba(0,0,0,.08);}}
.bp{{display:inline-block;padding:2px 9px;border-radius:12px;font-size:11px;font-weight:700;color:white;}}
.bp-p{{background:#dc3545;}} .bp-v{{background:#1a6e2e;}} .bp-vig{{background:#1a6e2e;}} .bp-ven{{background:#8b1f3a;}}
.form-card{{background:white;border-radius:10px;padding:25px;box-shadow:0 2px 8px rgba(0,0,0,.08);}}
.form-label{{font-weight:600;font-size:14px;}}
.form-control:focus{{border-color:#8b1f3a;box-shadow:0 0 0 .2rem rgba(139,31,58,.15);}}
.alert-sf{{padding:10px 14px;border-radius:6px;margin-bottom:14px;font-size:13px;font-weight:600;}}
.alert-ok{{background:#d4edda;color:#155724;border:1px solid #c3e6cb;}}
.alert-err{{background:#f8d7da;color:#721c24;border:1px solid #f5c6cb;}}
.barra-contenedor{{width:100%;height:28px;background:rgba(139,31,58,.15);border-radius:14px;overflow:hidden;margin:10px 0;}}
.barra-progreso{{height:100%;background:#8b1f3a;border-radius:14px;display:flex;align-items:center;justify-content:center;color:white;font-size:12px;font-weight:700;transition:width .5s;}}
.cv{{display:inline-block;min-width:60px;max-width:200px;overflow:hidden;text-overflow:ellipsis;cursor:text;padding:3px 5px;border-radius:4px;border:1px solid transparent;color:#333;}}
.cv:hover{{border-color:#ccc;background:#fff8f8;}}
.cv.null-val{{color:#ccc;font-style:italic;}}
.cell-input{{border:2px solid #8b1f3a;border-radius:4px;padding:3px 6px;font-size:12px;min-width:120px;max-width:260px;outline:none;background:#fff8f8;}}
.del-btn{{background:#fff;border:1px solid #ccc;color:#c00;border-radius:4px;padding:3px 8px;font-size:11px;cursor:pointer;}}
.del-btn:hover{{background:#c00;color:#fff;}}
.toast{{position:fixed;bottom:70px;right:18px;z-index:999;padding:9px 16px;border-radius:7px;font-size:12px;opacity:0;transition:opacity .25s;pointer-events:none;border:1px solid transparent;max-width:260px;}}
.toast.show{{opacity:1;}} .toast.ok{{background:#e6ffee;border-color:#060;color:#060;}} .toast.err{{background:#fff0f0;border-color:#c00;color:#c00;}}
{extra_css}
</style>
</head>"""

def _sf_header_bar(seccion: str, admin_links: str = "") -> str:
    if not admin_links:
        admin_links = '<div class="d-flex gap-3 align-items-center"><a href="/panel/admin"><i class="fa-solid fa-house me-1"></i>Inicio</a><a href="/panel/logout"><i class="fa-solid fa-right-from-bracket me-1"></i>Salir</a></div>'
    return f"""
<header id="header" class="w-100 header">
<div id="contenido-fix">
  <div id="logo-buscador">
    <div class="container-lg">
      <div class="row">
        <div class="col-8 col-sm-6 logo-home d-flex align-items-center">
          <a href="https://sanfernando.gob.mx">
            <picture class="img-fluid logo">
              <source type="image/webp" srcset="https://sanfernando.gob.mx/wp-content/uploads/sites/36/2026/04/logotipo-secundario-horizontal-final_1600x480.png.webp"/>
              <img src="https://sanfernando.gob.mx/wp-content/uploads/sites/36/2026/04/logotipo-secundario-horizontal-final_1600x480.png" alt="San Fernando" class="img-fluid logo"/>
            </picture>
          </a>
        </div>
        <div class="col-4 col-sm-6 logo-home d-flex align-items-center justify-content-end">
          <div class="d-block d-lg-none" data-bs-toggle="offcanvas" data-bs-target="#nav-right">
            <form class="menu-btn-container m-0 h-100 d-flex justify-content-end">
              <label class="btn-movil m-0"><div class="menu-bars"><span></span><span></span><span></span></div></label>
            </form>
          </div>
        </div>
      </div>
    </div>
  </div>
  <div class="menu-responsivo">
    <div class="offcanvas offcanvas-end w-100 d-lg-none" tabindex="-1" id="nav-right">
      <div class="offcanvas-header">
        <img src="https://sanfernando.gob.mx/wp-content/uploads/sites/36/2026/04/escudo-con-fecha_blanco.png" alt="San Fernando" style="max-height:50px"/>
        <button type="button" class="btn-close btn-close-white btn-lg" data-bs-dismiss="offcanvas"></button>
      </div>
      <div class="offcanvas-body">
        <ul class="menu clean-list menu-principal">
          <li><a href="/panel/admin">Panel Admin</a></li>
          <li><a href="/panel/folios">Ver Folios</a></li>
          <li><a href="/panel/registro_admin">Registrar Permiso</a></li>
          <li><a href="/panel/crear_usuario">Crear Usuario</a></li>
          <li><a href="/panel/tablas">Tablas BD</a></li>
          <li><a href="/panel/test_fechas">Test Fechas</a></li>
          <li><a href="/panel/logout">Cerrar Sesión</a></li>
        </ul>
      </div>
    </div>
  </div>
</div>
</header>
<div class="admin-bar">
  <span><i class="fa-solid fa-shield-halved me-2"></i>{seccion}</span>
  {admin_links}
</div>
<div id="breadcrumbs">
  <div class="container-lg"><div class="row"><div class="col-12">
    <div class="breadcrumbs">
      <span><span><a href="https://sanfernando.gob.mx/">Portada</a></span> » <span><a href="/panel/admin">Panel Admin</a></span></span>
    </div>
  </div></div></div>
</div>"""

def _sf_footer(scripts: str = "") -> str:
    return f"""
<footer id="footer" style="margin-top:40px">
  <div class="container-lg" id="content-footer">
    <div class="row">
      <div class="col-lg-6">
        <div class="row-titulo"><h2 class="titulo-row">San Fernando</h2><div class="borde-hr"></div></div>
        <div class="row"><div class="col informacion-logo d-flex">
          <picture class="logo">
            <img class="logo" src="https://sanfernando.gob.mx/wp-content/uploads/sites/36/2026/04/escudo-con-fecha_blanco.png" alt="San Fernando" style="max-height:75px;display:block"/>
          </picture>
          <div class="px-5"><p>Calle Hidalgo Sin Número, entre Calle Juárez y Calle Escandón, Zona Centro, C.P 87600.</p></div>
        </div></div>
      </div>
      <div class="col-lg-3 col-6">
        <div class="row-titulo"><h2 class="titulo-row">Accesos</h2><div class="borde-hr"></div></div>
        <ul class="informacion-links clean-list">
          <li><a href="/panel/admin">Panel Admin</a></li>
          <li><a href="/panel/folios">Ver Folios</a></li>
          <li><a href="/panel/registro_admin">Registrar Permiso</a></li>
          <li><a href="https://sanfernando.gob.mx/">Sitio Municipal</a></li>
        </ul>
      </div>
      <div class="col-lg-3 col-6">
        <div class="row-titulo"><h2 class="titulo-row">Síguenos en</h2><div class="borde-hr"></div></div>
        <ul class="clean-list informacion-links">
          <li><a href="https://www.facebook.com/VeronicaAguirreOficial" target="_blank"><i class="fa-brands fa-square-facebook"></i> Facebook</a></li>
        </ul>
      </div>
    </div>
  </div>
  <div id="terminos-y-condiciones">
    <div class="container-xxl container-lg">
      <div class="row d-flex justify-content-center">
        <div class="col-lg-7 text-center mt-2 mb-2 contenido-text">
          Todos los derechos reservados © 2026 | Gobierno del Estado de Tamaulipas 2022 - 2028
        </div>
      </div>
    </div>
  </div>
</footer>
<script src="https://cdnjs.cloudflare.com/ajax/libs/jquery/3.7.1/jquery.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/js/bootstrap.bundle.min.js"></script>
<script src="https://sanfernando.gob.mx/wp-content/themes/municipios-tamaulipas/assets/js/funciones.js"></script>
{scripts}
</body></html>"""

def _page(titulo: str, seccion: str, contenido: str, scripts: str = "", extra_css: str = "") -> str:
    return (_sf_head(titulo, extra_css) + "<body>" +
            _sf_header_bar(seccion) +
            f'<div class="admin-content">{contenido}</div>' +
            _sf_footer(scripts))

def _login_html(error: bool = False) -> str:
    err = '<div class="alert-sf alert-err mb-3"><i class="fa-solid fa-triangle-exclamation me-2"></i>Usuario o contraseña incorrectos</div>' if error else ""
    return f"""<!DOCTYPE html><html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Acceso - Tránsito San Fernando</title>
<link rel="icon" href="https://sanfernando.gob.mx/wp-content/uploads/sites/36/2026/04/cropped-logo-secundario-vertical-32x32.png" sizes="32x32"/>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/css/bootstrap.min.css" rel="stylesheet">
<link href="https://fonts.googleapis.com/css2?family=Encode+Sans:wght@400;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.2/css/all.min.css"/>
<link rel="stylesheet" href="https://sanfernando.gob.mx/wp-content/themes/municipios-tamaulipas/assets/css/estilos.css">
<style>
:root{{--font:'Encode Sans',sans-serif;}} *{{font-family:var(--font);}}
body{{background:#8b1f3a;min-height:100vh;margin:0;display:flex;flex-direction:column;}}
.login-header{{background:white;padding:10px 20px;text-align:center;border-bottom:3px solid #8b1f3a;}}
.login-header img{{height:55px;object-fit:contain;}}
.login-wrap{{flex:1;display:flex;align-items:center;justify-content:center;padding:30px 15px;}}
.login-card{{background:white;border-radius:14px;padding:35px;max-width:380px;width:100%;box-shadow:0 10px 40px rgba(0,0,0,.25);}}
.login-escudo{{text-align:center;margin-bottom:18px;}}
.login-escudo img{{height:65px;filter:sepia(1) saturate(3) hue-rotate(310deg) brightness(.7);}}
.login-title{{text-align:center;font-size:19px;font-weight:700;color:#8b1f3a;margin-bottom:4px;}}
.login-sub{{text-align:center;font-size:12px;color:#666;margin-bottom:22px;}}
.form-label{{font-weight:600;font-size:14px;}}
.form-control:focus{{border-color:#8b1f3a;box-shadow:0 0 0 .2rem rgba(139,31,58,.15);}}
.btn-ingresar{{background:#8b1f3a;border-color:#8b1f3a;color:white;width:100%;padding:12px;font-weight:700;}}
.btn-ingresar:hover{{background:#700c26;border-color:#700c26;color:white;}}
.alert-sf{{padding:10px 14px;border-radius:6px;margin-bottom:14px;font-size:13px;font-weight:600;}}
.alert-err{{background:#f8d7da;color:#721c24;border:1px solid #f5c6cb;}}
.login-footer{{background:rgba(0,0,0,.2);color:rgba(255,255,255,.7);text-align:center;padding:12px;font-size:12px;}}
</style></head><body>
<div class="login-header">
  <img src="https://sanfernando.gob.mx/wp-content/uploads/sites/36/2026/04/logotipo-secundario-horizontal-final_1600x480.png" alt="San Fernando">
</div>
<div class="login-wrap"><div class="login-card">
  <div class="login-escudo"><img src="https://sanfernando.gob.mx/wp-content/uploads/sites/36/2026/04/escudo-con-fecha_blanco.png" alt="Escudo"></div>
  <div class="login-title">Tránsito y Vialidad</div>
  <div class="login-sub">Municipio de San Fernando, Tamaulipas<br>Sistema Administrativo</div>
  {err}
  <form method="POST" action="/panel/login">
    <div class="mb-3"><label class="form-label">Usuario</label>
      <input type="text" name="username" class="form-control" required autofocus autocomplete="off"></div>
    <div class="mb-4"><label class="form-label">Contraseña</label>
      <input type="password" name="password" class="form-control" required></div>
    <button type="submit" class="btn btn-ingresar">
      <i class="fa-solid fa-right-to-bracket me-2"></i>Ingresar al Sistema</button>
  </form>
</div></div>
<div class="login-footer">Dirección de Tránsito y Vialidad — San Fernando, Tamaulipas © 2026</div>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/js/bootstrap.bundle.min.js"></script>
</body></html>"""

# ===================== PANEL ADMIN =====================
@app.get("/panel/login", response_class=HTMLResponse)
async def login_get(request: Request):
    return HTMLResponse(_login_html(bool(request.query_params.get("error",""))))

@app.post("/panel/login")
async def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    if username == ADMIN_USER and password == ADMIN_PASS:
        request.session["admin"] = True
        request.session["username"] = username
        return RedirectResponse(url="/panel/admin", status_code=303)
    # Verificar usuario 3ro
    try:
        res = supabase.table("verificacion_sanfernando").select("*")\
            .eq("username", username).eq("password", password).execute()
        if res.data:
            request.session["user_id"]  = res.data[0].get("id")
            request.session["username"] = res.data[0]["username"]
            request.session["admin"]    = False
            return RedirectResponse(url="/registro_usuario", status_code=303)
    except Exception as e:
        print(f"[LOGIN] Error: {e}")
    return RedirectResponse(url="/panel/login?error=1", status_code=303)

@app.get("/panel/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/panel/login", status_code=303)

@app.get("/panel/admin", response_class=HTMLResponse)
async def panel_admin(request: Request):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    pendientes = 0
    try:
        r = supabase.table("folios_registrados").select("folio")\
            .eq("estado_pago","PENDIENTE_PAGO").eq("entidad",ENTIDAD).execute()
        pendientes = len(r.data or [])
    except Exception: pass
    color_pend = "#dc3545" if pendientes else "#1a6e2e"
    contenido = f"""
    <div class="row g-3 mb-4">
      <div class="col-6"><div class="stat-card"><div class="stat-num">{len(timers_activos)}</div><div class="stat-lbl">Timers Activos</div></div></div>
      <div class="col-6"><div class="stat-card"><div class="stat-num" style="color:{color_pend}">{pendientes}</div><div class="stat-lbl">Pendientes de Pago</div></div></div>
      <div class="col-12"><div class="stat-card"><div class="stat-num">{FOLIO_NUM_PREF}{_folio_counter['siguiente']}</div><div class="stat-lbl">Siguiente Folio</div></div></div>
    </div>
    <div class="row g-3">
      <div class="col-6"><a href="/panel/folios" class="menu-btn"><i class="fa-solid fa-list-check"></i><span style="font-size:13px;font-weight:600">Ver Folios</span></a></div>
      <div class="col-6"><a href="/panel/registro_admin" class="menu-btn"><i class="fa-solid fa-file-circle-plus"></i><span style="font-size:13px;font-weight:600">Registrar Permiso</span></a></div>
      <div class="col-6"><a href="/panel/crear_usuario" class="menu-btn"><i class="fa-solid fa-user-plus"></i><span style="font-size:13px;font-weight:600">Crear Usuario</span></a></div>
      <div class="col-6"><a href="/panel/tablas" class="menu-btn"><i class="fa-solid fa-database"></i><span style="font-size:13px;font-weight:600">Tablas BD</span></a></div>
      <div class="col-6"><a href="/consulta_folio" class="menu-btn"><i class="fa-solid fa-magnifying-glass"></i><span style="font-size:13px;font-weight:600">Consultar Folio</span></a></div>
      <div class="col-6"><a href="/panel/test_fechas" class="menu-btn"><i class="fa-solid fa-flask"></i><span style="font-size:13px;font-weight:600">🧪 Test Fechas</span></a></div>
      <div class="col-12"><a href="/panel/logout" class="menu-btn danger"><i class="fa-solid fa-right-from-bracket"></i><span style="font-size:13px;font-weight:600">Cerrar Sesión</span></a></div>
    </div>"""
    return HTMLResponse(_page("Panel de Administración", "Panel de Administración — Tránsito San Fernando", contenido))

# ===================== FOLIOS =====================
@app.get("/panel/folios", response_class=HTMLResponse)
async def admin_folios(request: Request):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    filtro     = request.query_params.get("filtro","").strip()
    criterio   = request.query_params.get("criterio","folio")
    ep_filtro  = request.query_params.get("estado_pago","todos")
    ev_filtro  = request.query_params.get("estado_vigencia","todos")
    msg        = request.query_params.get("msg","")
    try:
        q = supabase.table("folios_registrados").select("*").eq("entidad",ENTIDAD)
        if filtro:
            q = q.ilike(criterio, f"%{filtro}%")
        if ep_filtro != "todos":
            q = q.eq("estado_pago", ep_filtro)
        folios = q.order("fecha_expedicion", desc=True).execute().data or []
        tz  = ZoneInfo(TZ)
        hoy = datetime.now(tz).date()
        for f in folios:
            try:
                fv = datetime.fromisoformat(f["fecha_vencimiento"]).date()
                f["estado_calc"] = "VIGENTE" if hoy <= fv else "VENCIDO"
            except Exception:
                f["estado_calc"] = "ERROR"
        if ev_filtro != "todos":
            folios = [f for f in folios if f.get("estado_calc","") == ev_filtro]
    except Exception as e:
        folios = []
        print(f"[FOLIOS] Error: {e}")
    msg_html = f'<div class="alert-sf alert-ok mb-3"><i class="fa-solid fa-circle-check me-2"></i>{msg}</div>' if msg else ""
    filas = ""
    for f in folios:
        pago  = f.get("estado_pago","VALIDADO") or "VALIDADO"
        ec    = f.get("estado_calc","")
        bp    = f'<span class="bp bp-p">PENDIENTE</span>' if pago=="PENDIENTE_PAGO" else f'<span class="bp bp-v">VALIDADO</span>'
        be    = f'<span class="bp bp-vig">VIGENTE</span>' if ec=="VIGENTE" else f'<span class="bp bp-ven">VENCIDO</span>'
        bval  = f'<form method="POST" action="/panel/validar/{f["folio"]}" style="display:inline"><button class="btn btn-sm py-0 px-2" style="background:#1a6e2e;color:white;font-size:11px" onclick="return confirm(\'¿Validar pago?\')">✅</button></form>' if pago=="PENDIENTE_PAGO" else ""
        filas += f"""<tr>
          <td><strong>{f.get("folio","")}</strong></td>
          <td>{f.get("nombre","")}</td>
          <td>{f.get("marca","")} {f.get("linea","")}</td>
          <td>{f.get("anio","")}</td>
          <td style="font-size:11px">{f.get("numero_serie","")}</td>
          <td>{str(f.get("fecha_expedicion",""))[:10]}</td>
          <td>{str(f.get("fecha_vencimiento",""))[:10]}</td>
          <td>{be}</td><td>{bp}</td>
          <td>
            {bval}
            <a href="/panel/pdf/{f.get('folio','')}" class="btn btn-sm py-0 px-2" style="background:#8b1f3a;color:white;font-size:11px">📄</a>
            <a href="/consulta/{f.get('folio','')}" target="_blank" class="btn btn-sm py-0 px-2" style="background:#555;color:white;font-size:11px">🔗</a>
          </td></tr>"""
    filtros = f"""
    <form method="GET" class="d-flex gap-2 mb-3 flex-wrap align-items-end">
      <div><label class="form-label mb-1" style="font-size:12px;font-weight:700">Buscar</label>
        <div class="input-group input-group-sm">
          <input type="text" name="filtro" class="form-control" value="{filtro}" placeholder="folio, serie...">
          <select name="criterio" class="form-select" style="max-width:120px">
            <option value="folio" {"selected" if criterio=="folio" else ""}>Folio</option>
            <option value="numero_serie" {"selected" if criterio=="numero_serie" else ""}>Serie</option>
            <option value="nombre" {"selected" if criterio=="nombre" else ""}>Nombre</option>
          </select>
        </div>
      </div>
      <div><label class="form-label mb-1" style="font-size:12px;font-weight:700">Pago</label>
        <select name="estado_pago" class="form-select form-select-sm">
          <option value="todos" {"selected" if ep_filtro=="todos" else ""}>Todos</option>
          <option value="PENDIENTE_PAGO" {"selected" if ep_filtro=="PENDIENTE_PAGO" else ""}>Pendiente</option>
          <option value="VALIDADO" {"selected" if ep_filtro=="VALIDADO" else ""}>Validado</option>
        </select>
      </div>
      <div><label class="form-label mb-1" style="font-size:12px;font-weight:700">Vigencia</label>
        <select name="estado_vigencia" class="form-select form-select-sm">
          <option value="todos" {"selected" if ev_filtro=="todos" else ""}>Todos</option>
          <option value="VIGENTE" {"selected" if ev_filtro=="VIGENTE" else ""}>Vigente</option>
          <option value="VENCIDO" {"selected" if ev_filtro=="VENCIDO" else ""}>Vencido</option>
        </select>
      </div>
      <button type="submit" class="btn btn-primary btn-sm">Filtrar</button>
      <a href="/panel/folios" class="btn btn-outline-secondary btn-sm">Limpiar</a>
      <span class="ms-auto" style="font-size:13px;color:#666">Total: <strong>{len(folios)}</strong></span>
    </form>"""
    contenido = f"""
    <div class="row-titulo mb-3"><h1 class="titulo-row" style="font-size:22px">Folios Registrados</h1><div class="borde-hr"><hr></div></div>
    {msg_html}{filtros}
    <div class="tabla-wrap"><table>
      <thead><tr><th>Folio</th><th>Titular</th><th>Vehículo</th><th>Año</th><th>Serie</th><th>Expedición</th><th>Vencimiento</th><th>Estado</th><th>Pago</th><th>Acciones</th></tr></thead>
      <tbody>{filas or '<tr><td colspan="10" style="text-align:center;color:#999;padding:20px">Sin folios</td></tr>'}</tbody>
    </table></div>"""
    return HTMLResponse(_page("Folios", "Folios Registrados", contenido))

@app.post("/panel/validar/{folio}")
async def validar_pago(request: Request, folio: str):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    folio = folio.strip().upper()
    try:
        supabase.table("folios_registrados").update({"estado_pago":"VALIDADO"}).eq("folio",folio).execute()
        if folio in timers_activos:
            uid    = timers_activos[folio]["user_id"]
            nombre = timers_activos[folio].get("nombre","")
            cancelar_timer_folio(folio)
            try:
                await bot.send_message(uid,
                    f"✅ PAGO VALIDADO — SAN FERNANDO\nFolio: {folio}\nTitular: {nombre}\n"
                    f"Tu permiso está activo.\n\n📋 Use /banamex para otro permiso.")
            except Exception: pass
    except Exception as e:
        print(f"[VALIDAR] Error: {e}")
    from urllib.parse import quote
    return RedirectResponse(url=f"/panel/folios?msg={quote(f'Folio {folio} validado ✅')}", status_code=303)

@app.get("/panel/pdf/{folio}")
async def descargar_pdf_panel(folio: str, request: Request):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    folio = folio.strip().upper()
    try:
        res = supabase.table("folios_registrados").select("pdf_url").eq("folio",folio).execute()
        if res.data and res.data[0].get("pdf_url"):
            return RedirectResponse(url=res.data[0]["pdf_url"])
    except Exception: pass
    ruta = os.path.join(OUTPUT_DIR, f"{folio}.pdf")
    if os.path.exists(ruta):
        from fastapi.responses import FileResponse
        return FileResponse(ruta, media_type="application/pdf", filename=f"{folio}_sanfernando.pdf")
    return HTMLResponse(f"<h3>PDF {folio} no encontrado.</h3><a href='/panel/folios'>← Volver</a>", status_code=404)

# ===================== REGISTRO ADMIN =====================
@app.get("/panel/registro_admin", response_class=HTMLResponse)
async def registro_admin_get(request: Request):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    tz  = ZoneInfo(TZ)
    hoy = datetime.now(tz).strftime("%Y-%m-%d")
    err = request.query_params.get("error","")
    err_html = f'<div class="alert-sf alert-err mb-3"><i class="fa-solid fa-triangle-exclamation me-2"></i>{err or "Error al registrar. Intenta de nuevo."}</div>' if err else ""
    contenido = f"""
    <div class="row-titulo mb-3"><h1 class="titulo-row" style="font-size:22px">Registrar Permiso (Admin)</h1><div class="borde-hr"><hr></div></div>
    {err_html}
    <div class="form-card">
      <form method="POST" action="/panel/registro_admin">
        <div class="mb-3">
          <label class="form-label">Folio manual <span style="color:#999;font-weight:400">(vacío = auto-generar)</span></label>
          <input type="text" name="folio" class="form-control" placeholder="Ej: 7801234">
        </div>
        <div class="row g-3">
          <div class="col-sm-6"><label class="form-label">Marca *</label><input type="text" name="marca" class="form-control" required></div>
          <div class="col-sm-6"><label class="form-label">Línea / Modelo *</label><input type="text" name="linea" class="form-control" required></div>
          <div class="col-4"><label class="form-label">Año *</label><input type="text" name="anio" class="form-control" maxlength="4" required></div>
          <div class="col-8"><label class="form-label">Color</label><input type="text" name="color" class="form-control"></div>
          <div class="col-sm-6"><label class="form-label">Núm. de Serie *</label><input type="text" name="numero_serie" class="form-control" required></div>
          <div class="col-sm-6"><label class="form-label">Núm. de Motor *</label><input type="text" name="numero_motor" class="form-control" required></div>
          <div class="col-12"><label class="form-label">Nombre del titular *</label><input type="text" name="nombre" class="form-control" required></div>
          <div class="col-sm-6"><label class="form-label">Fecha de expedición</label><input type="date" name="fecha_expedicion" class="form-control" value="{hoy}"></div>
          <div class="col-sm-6"><label class="form-label">Fecha de vencimiento <span style="color:#999;font-weight:400">(vacío = +30 días)</span></label><input type="date" name="fecha_vencimiento" class="form-control"></div>
        </div>
        <button type="submit" class="btn btn-primary w-100 mt-4 py-2 fw-bold"><i class="fa-solid fa-file-circle-plus me-2"></i>Generar Permiso</button>
      </form>
    </div>"""
    return HTMLResponse(_page("Registrar Permiso", "Registrar Permiso", contenido))

@app.post("/panel/registro_admin")
async def registro_admin_post(request: Request,
    folio: str = Form(None), marca: str = Form(...), linea: str = Form(...),
    anio: str = Form(...), color: str = Form(""), numero_serie: str = Form(...),
    numero_motor: str = Form(...), nombre: str = Form(...),
    fecha_expedicion: str = Form(None), fecha_vencimiento: str = Form(None)):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    try:
        tz = ZoneInfo(TZ)
        fg = folio.strip().upper() if folio and folio.strip() else generar_folio()
        fe = datetime.fromisoformat(fecha_expedicion).date() if fecha_expedicion and fecha_expedicion.strip() else datetime.now(tz).date()
        fv = datetime.fromisoformat(fecha_vencimiento).date() if fecha_vencimiento and fecha_vencimiento.strip() else fe + timedelta(days=30)
        datos_pdf = {
            "folio": fg, "marca": marca.upper(), "linea": linea.upper(), "anio": anio,
            "serie": numero_serie.upper(), "motor": numero_motor.upper(),
            "color": color.upper(), "nombre": nombre.upper(),
            "fecha_exp": fe.strftime("%d/%m/%Y"), "fecha_ven": fv.strftime("%d/%m/%Y"),
            "fecha_exp_dt": datetime.combine(fe, datetime.min.time()).replace(tzinfo=tz),
            "fecha_ven_dt": datetime.combine(fv, datetime.min.time()).replace(tzinfo=tz),
        }
        generar_pdf(datos_pdf)
        supabase.table("folios_registrados").insert({
            "folio": fg, "marca": marca.upper(), "linea": linea.upper(), "anio": anio,
            "numero_serie": numero_serie.upper(), "numero_motor": numero_motor.upper(),
            "color": color.upper(), "nombre": nombre.upper(),
            "fecha_expedicion": fe.isoformat(), "fecha_vencimiento": fv.isoformat(),
            "entidad": ENTIDAD, "estado": "ACTIVO", "estado_pago": "VALIDADO",
            "creado_por": request.session.get("username","admin")
        }).execute()
        from urllib.parse import quote
        return RedirectResponse(url=f"/panel/folios?msg={quote(f'Permiso {fg} generado ✅')}", status_code=303)
    except Exception as e:
        print(f"[REGISTRO ADMIN] Error: {e}")
        from urllib.parse import quote
        return RedirectResponse(url=f"/panel/registro_admin?error={quote(str(e))}", status_code=303)

# ===================== CREAR USUARIO =====================
@app.get("/panel/crear_usuario", response_class=HTMLResponse)
async def crear_usuario_get(request: Request):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    msg = request.query_params.get("msg","")
    err = request.query_params.get("error","")
    msg_html = f'<div class="alert-sf alert-ok mb-3"><i class="fa-solid fa-circle-check me-2"></i>{msg}</div>' if msg else ""
    err_html = f'<div class="alert-sf alert-err mb-3"><i class="fa-solid fa-triangle-exclamation me-2"></i>{err}</div>' if err else ""
    contenido = f"""
    <div class="row-titulo mb-3"><h1 class="titulo-row" style="font-size:22px">Crear Usuario</h1><div class="borde-hr"><hr></div></div>
    {msg_html}{err_html}
    <div class="form-card" style="max-width:500px">
      <form method="POST" action="/panel/crear_usuario">
        <div class="mb-3"><label class="form-label">Nombre de usuario *</label><input type="text" name="username" class="form-control" required autocomplete="off"></div>
        <div class="mb-3"><label class="form-label">Contraseña *</label><input type="password" name="password" class="form-control" required></div>
        <div class="mb-4"><label class="form-label">Folios asignados *</label><input type="number" name="folios" class="form-control" min="1" required></div>
        <button type="submit" class="btn btn-primary w-100 py-2 fw-bold"><i class="fa-solid fa-user-plus me-2"></i>Crear Usuario</button>
      </form>
    </div>"""
    return HTMLResponse(_page("Crear Usuario","Crear Usuario", contenido))

@app.post("/panel/crear_usuario")
async def crear_usuario_post(request: Request,
    username: str = Form(...), password: str = Form(...), folios: int = Form(...)):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    from urllib.parse import quote
    try:
        existe = supabase.table("verificacion_sanfernando").select("id")\
            .eq("username", username).execute()
        if existe.data:
            return RedirectResponse(url=f"/panel/crear_usuario?error={quote('El usuario ya existe')}", status_code=303)
        supabase.table("verificacion_sanfernando").insert({
            "username": username, "password": password,
            "folios_asignac": folios, "folios_usados": 0
        }).execute()
        return RedirectResponse(url=f"/panel/crear_usuario?msg={quote(f'Usuario {username} creado con {folios} folios ✅')}", status_code=303)
    except Exception as e:
        return RedirectResponse(url=f"/panel/crear_usuario?error={quote(str(e))}", status_code=303)

# ===================== REGISTRO USUARIO 3RO =====================
@app.get("/registro_usuario", response_class=HTMLResponse)
async def registro_usuario_get(request: Request):
    if not request.session.get("username") or request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    ud = supabase.table("verificacion_sanfernando").select("*")\
        .eq("username", request.session["username"]).limit(1).execute()
    if not ud.data:
        return RedirectResponse(url="/panel/login", status_code=303)
    u = ud.data[0]
    asig = int(u.get("folios_asignac",0))
    usad = int(u.get("folios_usados",0))
    disp = asig - usad
    porc = round((usad / asig * 100) if asig else 0, 1)
    tz  = ZoneInfo(TZ)
    hoy = datetime.now(tz).strftime("%Y-%m-%d")
    msg = request.query_params.get("msg","")
    err = request.query_params.get("error","")
    msg_html = f'<div class="alert-sf alert-ok mb-3"><i class="fa-solid fa-circle-check me-2"></i>{msg}</div>' if msg else ""
    err_html = f'<div class="alert-sf alert-err mb-3"><i class="fa-solid fa-triangle-exclamation me-2"></i>{err}</div>' if err else ""
    contenido = f"""
    <div class="row-titulo mb-3"><h1 class="titulo-row" style="font-size:22px">Tránsito San Fernando — Registrar Permiso</h1><div class="borde-hr"><hr></div></div>
    <div class="form-card mb-4">
      <div class="d-flex justify-content-between align-items-center mb-2">
        <span style="font-weight:700;font-size:14px">Mis Folios Disponibles</span>
        <span style="font-size:13px;color:#666">{usad} usados / {asig} total</span>
      </div>
      <div class="barra-contenedor">
        <div class="barra-progreso" style="width:{porc}%">{porc}%</div>
      </div>
      <div class="d-flex justify-content-between" style="font-size:12px;color:#666;margin-top:4px">
        <span>✅ Usados: <strong>{usad}</strong></span>
        <span>📦 Total: <strong>{asig}</strong></span>
        <span>🎯 Disponibles: <strong>{disp}</strong></span>
      </div>
    </div>
    {msg_html}{err_html}
    {"" if disp <= 0 else f'''
    <div class="form-card">
      <div style="background:#e8f5e9;border:1px solid #a5d6a7;border-radius:6px;padding:10px;text-align:center;font-weight:700;font-size:14px;color:#1b5e20;margin-bottom:18px">
        🎯 El folio se generará automáticamente
      </div>
      <form method="POST" action="/registro_usuario">
        <div class="row g-3">
          <div class="col-sm-6"><label class="form-label">Marca *</label><input type="text" name="marca" class="form-control" required style="text-transform:uppercase"></div>
          <div class="col-sm-6"><label class="form-label">Línea *</label><input type="text" name="linea" class="form-control" required style="text-transform:uppercase"></div>
          <div class="col-4"><label class="form-label">Año *</label><input type="number" name="anio" class="form-control" required></div>
          <div class="col-8"><label class="form-label">Color</label><input type="text" name="color" class="form-control" style="text-transform:uppercase"></div>
          <div class="col-sm-6"><label class="form-label">Núm. Serie *</label><input type="text" name="serie" class="form-control" required style="text-transform:uppercase"></div>
          <div class="col-sm-6"><label class="form-label">Núm. Motor *</label><input type="text" name="motor" class="form-control" required style="text-transform:uppercase"></div>
          <div class="col-12"><label class="form-label">Nombre del titular *</label><input type="text" name="nombre" class="form-control" required style="text-transform:uppercase"></div>
          <div class="col-sm-6"><label class="form-label">Fecha de inicio de vigencia</label><input type="date" name="fecha_inicio" class="form-control" value="{hoy}" min="{hoy}"></div>
        </div>
        <button type="submit" id="btnReg" class="btn btn-primary w-100 mt-4 py-2 fw-bold">Registrar Folio</button>
      </form>
    </div>
    ''' if disp > 0 else '<div class="alert-sf alert-err"><i class="fa-solid fa-triangle-exclamation me-2"></i>Sin folios disponibles. Contacta al administrador.</div>'}
    <div class="mt-3 d-flex gap-2 flex-wrap">
      <a href="/mis_permisos" class="btn btn-outline-secondary btn-sm">📋 Mis Permisos</a>
      <a href="/consulta_folio" class="btn btn-outline-secondary btn-sm">🔍 Consultar Folio</a>
      <a href="/panel/logout" class="btn btn-outline-danger btn-sm">🚪 Salir</a>
    </div>"""
    scripts = """<script>
document.querySelector('form') && document.querySelector('form').addEventListener('submit', function(e){
  const btn = document.getElementById('btnReg');
  if(btn.disabled){e.preventDefault();return;}
  btn.disabled=true; btn.textContent='⏳ Generando...';
  setTimeout(()=>{btn.disabled=false;btn.textContent='Registrar Folio';},10000);
});
</script>"""
    return HTMLResponse(_page("Registrar Permiso","Registro de Permisos — San Fernando", contenido, scripts))

@app.post("/registro_usuario")
async def registro_usuario_post(request: Request,
    marca: str = Form(...), linea: str = Form(...), anio: str = Form(...),
    color: str = Form(""), serie: str = Form(...), motor: str = Form(...),
    nombre: str = Form(...), fecha_inicio: str = Form(None)):
    if not request.session.get("username") or request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    from urllib.parse import quote
    try:
        ud = supabase.table("verificacion_sanfernando").select("*")\
            .eq("username", request.session["username"]).limit(1).execute()
        if not ud.data:
            return RedirectResponse(url="/panel/login", status_code=303)
        u    = ud.data[0]
        asig = int(u.get("folios_asignac",0))
        usad = int(u.get("folios_usados",0))
        if asig - usad <= 0:
            return RedirectResponse(url=f"/registro_usuario?error={quote('Sin folios disponibles')}", status_code=303)
        tz  = ZoneInfo(TZ)
        fe  = datetime.strptime(fecha_inicio, "%Y-%m-%d").replace(tzinfo=tz) if fecha_inicio else datetime.now(tz)
        fv  = fe + timedelta(days=30)
        fg  = generar_folio()
        datos_pdf = {
            "folio": fg, "marca": marca.upper(), "linea": linea.upper(), "anio": anio,
            "serie": serie.upper(), "motor": motor.upper(), "color": color.upper(),
            "nombre": nombre.upper(), "fecha_exp": fe.strftime("%d/%m/%Y"),
            "fecha_ven": fv.strftime("%d/%m/%Y"), "fecha_exp_dt": fe, "fecha_ven_dt": fv,
        }
        generar_pdf(datos_pdf)
        user_id = request.session.get("user_id")
        supabase.table("folios_registrados").insert({
            "folio": fg, "marca": marca.upper(), "linea": linea.upper(), "anio": anio,
            "numero_serie": serie.upper(), "numero_motor": motor.upper(),
            "color": color.upper(), "nombre": nombre.upper(),
            "fecha_expedicion": fe.date().isoformat(), "fecha_vencimiento": fv.date().isoformat(),
            "entidad": ENTIDAD, "estado": "ACTIVO", "estado_pago": "VALIDADO",
            "user_id": user_id, "creado_por": request.session["username"]
        }).execute()
        supabase.table("verificacion_sanfernando")\
            .update({"folios_usados": usad+1}).eq("username", request.session["username"]).execute()
        # Obtener URL del PDF
        pdf_url = ""
        try:
            res = supabase.table("folios_registrados").select("pdf_url").eq("folio",fg).execute()
            pdf_url = res.data[0].get("pdf_url","") if res.data else ""
        except Exception: pass
        contenido = f"""
        <div class="row-titulo mb-3"><h1 class="titulo-row" style="font-size:22px">✅ Permiso Generado</h1><div class="borde-hr"><hr></div></div>
        <div class="form-card text-center">
          <div style="font-size:48px;margin-bottom:12px">📄</div>
          <h2 style="color:#8b1f3a;font-size:22px;font-weight:700">{fg}</h2>
          <p style="color:#666;font-size:14px">Folio de circulación generado correctamente</p>
          <div class="info-box text-start mt-3">
            <strong>Vehículo:</strong> {marca.upper()} {linea.upper()} {anio}<br>
            <strong>Serie:</strong> {serie.upper()}<br>
            <strong>Titular:</strong> {nombre.upper()}<br>
            <strong>Expedición:</strong> {fe.strftime("%d/%m/%Y")}<br>
            <strong>Vencimiento:</strong> {fv.strftime("%d/%m/%Y")}
          </div>
          {"<a href='" + pdf_url + "' target='_blank' class='btn btn-primary w-100 mt-3 py-2 fw-bold'><i class='fa-solid fa-download me-2'></i>Descargar PDF</a>" if pdf_url else "<p class='text-muted mt-3'>PDF generándose, disponible en segundos...</p>"}
          <div class="d-flex gap-2 mt-3 justify-content-center">
            <a href="/mis_permisos" class="btn btn-outline-secondary btn-sm">📋 Mis Permisos</a>
            <a href="/registro_usuario" class="btn btn-outline-primary btn-sm">+ Nuevo Permiso</a>
          </div>
        </div>"""
        return HTMLResponse(_page("Permiso Generado","Registro Exitoso — San Fernando", contenido))
    except Exception as e:
        print(f"[REG USUARIO] Error: {e}")
        return RedirectResponse(url=f"/registro_usuario?error={quote(str(e))}", status_code=303)

# ===================== MIS PERMISOS =====================
@app.get("/mis_permisos", response_class=HTMLResponse)
async def mis_permisos(request: Request):
    if not request.session.get("username") or request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    permisos = supabase.table("folios_registrados").select("*")\
        .eq("creado_por", request.session["username"])\
        .order("fecha_expedicion", desc=True).execute().data or []
    tz  = ZoneInfo(TZ)
    hoy = datetime.now(tz).date()
    for p in permisos:
        try:
            fv = datetime.fromisoformat(p["fecha_vencimiento"]).date()
            fe = datetime.fromisoformat(p["fecha_expedicion"]).date()
            p["fe_fmt"] = fe.strftime("%d/%m/%Y")
            p["estado_calc"] = "VIGENTE" if hoy <= fv else "VENCIDO"
        except Exception:
            p["fe_fmt"] = p["estado_calc"] = "ERROR"
    ud = supabase.table("verificacion_sanfernando").select("folios_asignac,folios_usados")\
        .eq("username", request.session["username"]).limit(1).execute().data
    ud = ud[0] if ud else {"folios_asignac":0,"folios_usados":0}
    asig = int(ud.get("folios_asignac",0))
    usad = int(ud.get("folios_usados",0))
    filas = ""
    for p in permisos:
        ec   = p.get("estado_calc","")
        be   = f'<span class="bp bp-vig">VIGENTE</span>' if ec=="VIGENTE" else f'<span class="bp bp-ven">VENCIDO</span>'
        pdf  = p.get("pdf_url","")
        btn  = f'<a href="{pdf}" target="_blank" class="btn btn-sm py-0 px-2" style="background:#8b1f3a;color:white;font-size:11px">📥 PDF</a>' if pdf else '<span style="color:#999;font-size:11px">Generando...</span>'
        filas += f"""<tr>
          <td><strong>{p.get("folio","")}</strong></td>
          <td>{p.get("marca","")} {p.get("linea","")}</td>
          <td style="font-size:11px">{p.get("numero_serie","")}</td>
          <td>{p.get("fe_fmt","")}</td>
          <td>{be}</td>
          <td>{btn} <a href="/consulta/{p.get('folio','')}" target="_blank" class="btn btn-sm py-0 px-2" style="background:#555;color:white;font-size:11px">🔗</a></td>
        </tr>"""
    contenido = f"""
    <div class="row-titulo mb-3"><h1 class="titulo-row" style="font-size:22px">📋 Mis Permisos</h1><div class="borde-hr"><hr></div></div>
    <div class="row g-3 mb-4">
      <div class="col-6"><div class="stat-card"><div class="stat-num">{asig}</div><div class="stat-lbl">Folios Asignados</div></div></div>
      <div class="col-6"><div class="stat-card"><div class="stat-num">{usad}</div><div class="stat-lbl">Folios Usados</div></div></div>
      <div class="col-6"><div class="stat-card"><div class="stat-num" style="color:#1a6e2e">{len(permisos)}</div><div class="stat-lbl">Total Generados</div></div></div>
      <div class="col-6"><div class="stat-card"><div class="stat-num" style="color:#8b1f3a">{asig-usad}</div><div class="stat-lbl">Disponibles</div></div></div>
    </div>
    <div class="tabla-wrap"><table>
      <thead><tr><th>Folio</th><th>Vehículo</th><th>Serie</th><th>Fecha</th><th>Estado</th><th>Acciones</th></tr></thead>
      <tbody>{filas or '<tr><td colspan="6" style="text-align:center;color:#999;padding:20px">Sin permisos generados</td></tr>'}</tbody>
    </table></div>
    <div class="mt-3 d-flex gap-2">
      <a href="/registro_usuario" class="btn btn-primary btn-sm">+ Nuevo Permiso</a>
      <a href="/panel/logout" class="btn btn-outline-danger btn-sm">🚪 Salir</a>
    </div>"""
    return HTMLResponse(_page("Mis Permisos","Mis Permisos — San Fernando", contenido))

# ===================== CONSULTA FOLIO (pública y panel) =====================
@app.get("/consulta_folio", response_class=HTMLResponse)
async def consulta_folio_form(request: Request):
    contenido = """
    <div class="row-titulo mb-3"><h1 class="titulo-row" style="font-size:22px">🔍 Consultar Folio</h1><div class="borde-hr"><hr></div></div>
    <div class="form-card" style="max-width:500px">
      <form method="POST" action="/consulta_folio">
        <label class="form-label">Número de Folio</label>
        <div class="d-flex gap-2">
          <input type="text" name="folio" class="form-control" placeholder="Ej: 7801234" required autofocus style="text-transform:uppercase">
          <button type="submit" class="btn btn-primary px-4">Buscar</button>
        </div>
      </form>
    </div>"""
    return HTMLResponse(_page("Consultar Folio","Consultar Folio", contenido))

@app.post("/consulta_folio", response_class=HTMLResponse)
async def consulta_folio_resultado(request: Request, folio: str = Form(...)):
    folio = folio.strip().upper()
    return RedirectResponse(url=f"/consulta/{folio}", status_code=303)

# ===================== TEST FECHAS =====================
@app.get("/panel/test_fechas", response_class=HTMLResponse)
async def test_fechas_get(request: Request):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    fb = request.query_params.get("folio","").strip().upper()
    msg = request.query_params.get("msg","")
    resultado = None
    if fb:
        try:
            r = supabase.table("folios_registrados").select("*").eq("folio",fb).execute()
            resultado = (r.data or [None])[0]
        except Exception as e:
            msg = f"Error: {e}"
    msg_html = f'<div class="alert-sf alert-ok mb-3"><i class="fa-solid fa-circle-check me-2"></i>{msg}</div>' if msg else ""
    info_html = acciones_html = ""
    if resultado:
        info_html = f"""
        <div class="info-box">
          <strong>Folio:</strong> {resultado.get("folio","")}<br>
          <strong>Estado pago:</strong> {resultado.get("estado_pago","")}<br>
          <strong>Expedición:</strong> {str(resultado.get("fecha_expedicion",""))[:10]}<br>
          <strong>Vencimiento:</strong> {str(resultado.get("fecha_vencimiento",""))[:10]}<br>
          <strong>user_id:</strong> {resultado.get("user_id","") or "ninguno (folio oficial)"}<br>
          <a href="/consulta/{resultado.get('folio','')}" target="_blank" style="color:#8b1f3a">
            🔗 Ver en público →</a>
        </div>"""
        acciones_html = f"""
        <form method="POST" action="/panel/test_fechas">
          <input type="hidden" name="folio" value="{resultado.get('folio','')}">
          <button type="submit" name="accion" value="vencer_permiso" class="btn w-100 mb-2 py-2 fw-bold" style="background:#b38b00;color:white;border:none">⏰ Marcar VENCIDO</button>
          <button type="submit" name="accion" value="vencer_pago_48h" class="btn w-100 mb-2 py-2 fw-bold" style="background:#8b1f3a;color:white;border:none">💀 Simular 48h sin pago</button>
          <button type="submit" name="accion" value="restaurar" class="btn w-100 py-2 fw-bold" style="background:#1a6e2e;color:white;border:none">✅ Restaurar vigencia normal</button>
        </form>"""
    contenido = f"""
    <div class="row-titulo mb-3"><h1 class="titulo-row" style="font-size:22px">🧪 Test Fechas</h1><div class="borde-hr"><hr></div></div>
    {msg_html}
    <div class="form-card" style="max-width:500px">
      <form method="GET"><label class="form-label">Folio a probar</label>
        <div class="d-flex gap-2 mb-3">
          <input type="text" name="folio" class="form-control" placeholder="Ej: 7801234" value="{fb}">
          <button type="submit" class="btn btn-primary px-4">Buscar</button>
        </div>
      </form>
      {info_html}{acciones_html}
    </div>"""
    return HTMLResponse(_page("Test Fechas","Test Fechas", contenido))

@app.post("/panel/test_fechas")
async def test_fechas_post(request: Request, folio: str = Form(...), accion: str = Form(...)):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    folio = folio.strip().upper()
    tz = ZoneInfo(TZ)
    msg = ""
    try:
        if accion == "vencer_permiso":
            nueva_ven = (datetime.now(tz) - timedelta(days=1)).date().isoformat()
            supabase.table("folios_registrados").update({"fecha_vencimiento":nueva_ven}).eq("folio",folio).execute()
            msg = f"Folio {folio} marcado VENCIDO."
        elif accion == "vencer_pago_48h":
            nueva_exp = (datetime.now(tz) - timedelta(hours=49)).isoformat()
            supabase.table("folios_registrados").update({"fecha_expedicion":nueva_exp}).eq("folio",folio).execute()
            msg = f"Folio {folio}: expedición movida 49h atrás."
        elif accion == "restaurar":
            hoy = datetime.now(tz)
            ven = hoy + timedelta(days=30)
            supabase.table("folios_registrados").update({
                "fecha_expedicion": hoy.date().isoformat(),
                "fecha_vencimiento": ven.date().isoformat()
            }).eq("folio",folio).execute()
            msg = f"Folio {folio} restaurado."
    except Exception as e:
        msg = f"Error: {e}"
    from urllib.parse import quote
    return RedirectResponse(url=f"/panel/test_fechas?folio={folio}&msg={quote(msg)}", status_code=303)

# ===================== TABLAS BD =====================
@app.get("/panel/tablas", response_class=HTMLResponse)
async def admin_tablas(request: Request):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    cards = "".join([f"""
    <div class="col-md-6"><div class="form-card">
      <h3 style="color:#8b1f3a;font-size:16px;font-weight:700"><i class="fa-solid fa-table me-2"></i>{info['nombre']}</h3>
      <p style="font-size:13px;color:#666">Tabla: <code>{nombre}</code> · {len(info['columnas'])} columnas</p>
      <a href="/panel/tabla/{nombre}" class="btn btn-primary btn-sm">Ver y editar datos →</a>
    </div></div>""" for nombre, info in TABLAS_DISPONIBLES.items()])
    contenido = f"""
    <div class="row-titulo mb-3"><h1 class="titulo-row" style="font-size:22px">🗄️ Tablas Base de Datos</h1><div class="borde-hr"><hr></div></div>
    <div class="row g-3">{cards}</div>"""
    return HTMLResponse(_page("Tablas BD","Administración de Tablas", contenido))

@app.get("/panel/tabla/{nombre_tabla}", response_class=HTMLResponse)
async def admin_tabla_detalle(nombre_tabla: str, request: Request):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    if nombre_tabla not in TABLAS_DISPONIBLES:
        return RedirectResponse(url="/panel/tablas", status_code=303)
    info   = TABLAS_DISPONIBLES[nombre_tabla]
    pk_col = info["pk_col"]
    q      = request.query_params.get("q","").strip()
    page   = max(1, int(request.query_params.get("page","1") or 1))
    try:
        todos = supabase.table(nombre_tabla).select("*").limit(20000).execute().data or []
        filtrados = [r for r in todos if any(q.lower() in str(v).lower() for v in r.values() if v is not None)] if q else todos
        total     = len(filtrados)
        offset    = (page-1)*PAGE_SIZE
        registros = filtrados[offset:offset+PAGE_SIZE]
    except Exception as e:
        todos=filtrados=registros=[]; total=offset=0
    columnas    = list(registros[0].keys()) if registros else (list(todos[0].keys()) if todos else info["columnas"])
    total_pages = max(1,(total+PAGE_SIZE-1)//PAGE_SIZE)
    th = "".join(f"<th>{c}</th>" for c in columnas) + "<th>Acción</th>"
    filas = ""
    for i, reg in enumerate(registros):
        celdas = ""
        for col in columnas:
            val = reg.get(col)
            disp = str(val) if val is not None else "null"
            cls  = "cv null-val" if val is None else "cv"
            celdas += f'<td><span class="{cls}" data-col="{col}" data-pk="{reg.get(pk_col,"")}" data-val="{val or ""}" onclick="editCell(this)" title="{col}">{disp}</span></td>'
        filas += f'<tr id="row{i}">{celdas}<td><button class="del-btn" onclick="delRow(this,\'{reg.get(pk_col,"")}\',\'row{i}\')">Borrar</button></td></tr>'
    pag_html = ""
    if total_pages > 1:
        links = f'<a href="?q={q}&page={page-1}">← Ant</a>' if page > 1 else ""
        links += f'<span class="cur">{page}</span>'
        links += f'<a href="?q={q}&page={page+1}">Sig →</a>' if page < total_pages else ""
        pag_html = f'<div style="display:flex;gap:8px;justify-content:center;padding:14px;border-top:1px solid #eee">{links}</div>'
    def _fila(i, reg):
        celdas = f'<td style="color:#bbb;font-size:11px">{offset+i+1}</td>'
        for col in columnas:
            val  = reg.get(col)
            disp = str(val) if val is not None else "null"
            cls  = "cv null-val" if val is None else "cv"
            pk_v = str(reg.get(pk_col,""))
            v_   = str(val or "")
            celdas += f'<td><span class="{cls}" data-col="{col}" data-pk="{pk_v}" data-val="{v_}" onclick="editCell(this)">{disp}</span></td>'
        pk_v2 = str(reg.get(pk_col,""))
        celdas += f'<td><button class="del-btn" onclick="delRow(this,\'{pk_v2}\',\'row{i}\')">Borrar</button></td>'
        return f'<tr id="row{i}">{celdas}</tr>'

    tbody = "".join(_fila(i, registros[i]) for i in range(len(registros))) or \
            "<tr><td colspan='20' style='text-align:center;padding:20px;color:#999'>Sin registros</td></tr>"

    contenido = f"""
    <div class="row-titulo mb-3"><h1 class="titulo-row" style="font-size:22px">📊 {info['nombre']}</h1><div class="borde-hr"><hr></div></div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px">
      <form method="GET" style="display:contents">
        <input type="text" name="q" value="{q}" placeholder="Buscar..." class="form-control form-control-sm" style="max-width:300px">
        <button type="submit" class="btn btn-primary btn-sm">🔍</button>
        {"<a href='/panel/tabla/"+nombre_tabla+"' class='btn btn-outline-secondary btn-sm'>✕</a>" if q else ""}
      </form>
      <span style="margin-left:auto;font-size:13px;color:#666">{total} registros · Pág {page}/{total_pages}</span>
    </div>
    <div class="tabla-wrap"><table id="tbl">
      <thead><tr><th>#</th>{th}</tr></thead>
      <tbody>{tbody}</tbody>
    </table></div>
    {pag_html}
    <div class="mt-3"><a href="/panel/tablas" class="btn btn-outline-secondary btn-sm">← Tablas</a></div>
    <div class="toast" id="toast"></div>"""
    scripts = f"""<script>
const TABLA="{nombre_tabla}",PK_COL="{pk_col}";
function editCell(span){{
  const col=span.dataset.col,pk=span.dataset.pk,origV=span.dataset.val;
  const inp=document.createElement('input');
  inp.type='text';inp.className='cell-input';inp.value=origV;
  inp._span=span;inp._origVal=origV;inp._col=col;inp._pk=pk;
  span.parentNode.insertBefore(inp,span);span.style.display='none';
  inp.focus();inp.select();
  inp.addEventListener('blur',()=>finishEdit(inp));
  inp.addEventListener('keydown',e=>{{if(e.key==='Enter'){{e.preventDefault();inp.blur();}}if(e.key==='Escape'){{inp._cancel=true;inp.blur();}}}});
}}
function finishEdit(inp){{
  const span=inp._span,newVal=inp.value.trim(),orig=inp._origVal;
  inp.remove();span.style.display='';
  if(inp._cancel||newVal===orig)return;
  span.textContent=newVal||'null';span.dataset.val=newVal;
  span.classList.toggle('null-val',!newVal);
  fetch('/panel/api/update_cell',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{tabla:TABLA,pk_col:PK_COL,pk_val:inp._pk,col:inp._col,val:newVal}})}})
  .then(r=>r.json()).then(d=>{{if(d.ok){{showToast('✓ '+inp._col+' guardado',true);}}else{{span.textContent=orig||'null';span.dataset.val=orig;showToast('Error: '+(d.error||'?'),false);}}}})
  .catch(()=>{{span.textContent=orig||'null';span.dataset.val=orig;showToast('Error de red',false);}});
}}
function delRow(btn,pk,rowId){{
  if(!confirm('¿Eliminar este registro?'))return;
  btn.disabled=true;btn.textContent='...';
  fetch('/panel/api/delete_row',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{tabla:TABLA,pk_col:PK_COL,pk_val:pk}})}})
  .then(r=>r.json()).then(d=>{{if(d.ok){{const tr=document.getElementById(rowId);if(tr){{tr.style.opacity='0';setTimeout(()=>tr.remove(),250);}}showToast('Registro eliminado',true);}}else{{btn.disabled=false;btn.textContent='Borrar';showToast('Error: '+(d.error||'?'),false);}}}})
  .catch(()=>{{btn.disabled=false;btn.textContent='Borrar';showToast('Error de red',false);}});
}}
let tt;
function showToast(msg,ok){{const t=document.getElementById('toast');t.textContent=msg;t.className='toast show '+(ok?'ok':'err');clearTimeout(tt);tt=setTimeout(()=>t.classList.remove('show'),2500);}}
</script>"""
    return HTMLResponse(_page(info["nombre"], info["nombre"], contenido, scripts))

@app.post("/panel/api/update_cell")
async def api_update_cell(request: Request):
    if not request.session.get("admin"):
        return {"ok": False, "error": "no autorizado"}
    d = await request.json()
    tabla=d.get("tabla"); pk_col=d.get("pk_col"); pk_val=d.get("pk_val"); col=d.get("col"); val=d.get("val","")
    if tabla not in TABLAS_DISPONIBLES or not col or not pk_val:
        return {"ok": False, "error": "datos inválidos"}
    try:
        supabase.table(tabla).update({col: val or None}).eq(pk_col, pk_val).execute()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/panel/api/delete_row")
async def api_delete_row(request: Request):
    if not request.session.get("admin"):
        return {"ok": False, "error": "no autorizado"}
    d = await request.json()
    tabla=d.get("tabla"); pk_col=d.get("pk_col"); pk_val=d.get("pk_val")
    if tabla not in TABLAS_DISPONIBLES or not pk_val:
        return {"ok": False, "error": "datos inválidos"}
    try:
        supabase.table(tabla).delete().eq(pk_col, pk_val).execute()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# Fallback — debe ir AL FINAL de todos los handlers del bot
@dp.message()
async def fallback(message: types.Message):
    await message.answer("🏛️ Dirección de Tránsito y Vialidad — San Fernando, Tamaulipas.")

@app.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse(f"""<!DOCTYPE html><html><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sistema Tránsito San Fernando</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
body{{background:#8b1f3a;min-height:100vh;display:flex;align-items:center;justify-content:center;margin:0}}
.card{{max-width:520px;width:100%;background:white;padding:35px;border-radius:14px;box-shadow:0 20px 60px rgba(0,0,0,.3);text-align:center}}
h1{{color:#8b1f3a;font-size:24px;font-weight:700}}
h2{{color:#555;font-size:17px;font-weight:400;margin-bottom:25px}}
.badge-ok{{display:inline-block;background:#e8f5e9;color:#1b5e20;border:2px solid #4caf50;padding:5px 16px;border-radius:20px;font-size:12px;font-weight:700;margin-bottom:20px}}
.info{{background:#f5f5f5;padding:18px;border-radius:10px;text-align:left;font-size:13px;margin-bottom:20px}}
.btn-primary{{background:#8b1f3a;border-color:#8b1f3a;padding:12px 30px;font-weight:700}}
.btn-primary:hover{{background:#700c26;border-color:#700c26}}
</style></head><body>
<div class="card">
  <h1>🚦 Sistema Digital de Permisos</h1>
  <h2>Dirección de Tránsito y Vialidad<br>San Fernando, Tamaulipas</h2>
  <div class="badge-ok">✅ Sistema Operativo</div>
  <div class="info">
    <strong>Versión:</strong> 1.0 — /banamex<br>
    <strong>Costo:</strong> ${PRECIO_PERMISO} MXN<br>
    <strong>Tiempo límite:</strong> 36 horas<br>
    <strong>Timers activos:</strong> {len(timers_activos)}<br>
    <strong>Siguiente folio:</strong> {FOLIO_NUM_PREF}{_folio_counter['siguiente']}
  </div>
  <a href="/panel/login" class="btn btn-primary">→ Panel de Administración</a>
</div>
</body></html>""")

@app.get("/health")
async def health():
    return {
        "status":    "healthy",
        "version":   "1.0",
        "entidad":   "San Fernando, Tamaulipas",
        "timestamp": datetime.now(ZoneInfo(TZ)).isoformat(),
        "timers_activos":  len(timers_activos),
        "siguiente_folio": f"{FOLIO_NUM_PREF}{_folio_counter['siguiente']}",
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    print(f"[SISTEMA] San Fernando v1.0 iniciando en puerto {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
