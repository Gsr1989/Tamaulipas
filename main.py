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

def generar_pdf(datos: dict) -> str:
    folio = datos["folio"]
    out   = os.path.join(OUTPUT_DIR, f"{folio}.pdf")
    try:
        if os.path.exists(PLANTILLA_PDF):
            doc = fitz.open(PLANTILLA_PDF)
            pg  = doc[0]
            # Folio en rojo
            pg.insert_text((50, 80),  str(folio),              fontsize=14, color=(1,0,0))
            pg.insert_text((50, 120), str(datos["marca"]),     fontsize=11, color=(0,0,0))
            pg.insert_text((50, 135), str(datos["linea"]),     fontsize=11, color=(0,0,0))
            pg.insert_text((50, 150), str(datos["anio"]),      fontsize=11, color=(0,0,0))
            pg.insert_text((50, 165), str(datos["serie"]),     fontsize=11, color=(0,0,0))
            pg.insert_text((50, 180), str(datos["motor"]),     fontsize=11, color=(0,0,0))
            pg.insert_text((50, 195), str(datos["color"]),     fontsize=11, color=(0,0,0))
            pg.insert_text((50, 210), str(datos["nombre"]),    fontsize=11, color=(0,0,0))
            pg.insert_text((50, 225), str(datos["fecha_exp"]), fontsize=10, color=(0,0,0))
            pg.insert_text((50, 240), str(datos["fecha_ven"]), fontsize=10, color=(0,0,0))
        else:
            doc = fitz.open()
            pg  = doc.new_page()
            pg.insert_text((50, 50),
                f"PERMISO SAN FERNANDO TAMAULIPAS\n"
                f"Folio: {folio}\n"
                f"Titular: {datos['nombre']}\n"
                f"Vehículo: {datos['marca']} {datos['linea']} {datos['anio']}\n"
                f"Serie: {datos['serie']}\n"
                f"Motor: {datos['motor']}\n"
                f"Expedición: {datos['fecha_exp']}\n"
                f"Vencimiento: {datos['fecha_ven']}",
                fontsize=12)

        img_qr, _ = generar_qr(folio)
        if img_qr:
            buf = BytesIO(); img_qr.save(buf, format="PNG"); buf.seek(0)
            pg.insert_image(fitz.Rect(450, 50, 550, 150), pixmap=fitz.Pixmap(buf.read()), overlay=True)

        doc.save(out); doc.close()
        print(f"[PDF] ✅ {out}")

        url = subir_pdf_a_storage(out, folio)
        if url:
            try:
                supabase.table("folios_registrados") \
                    .update({"pdf_url": url}).eq("folio", folio).execute()
            except Exception as e:
                print(f"[WARN] pdf_url: {e}")
        return out
    except Exception as e:
        print(f"[PDF] Error: {e}")
        doc_fb = fitz.open()
        doc_fb.new_page().insert_text((50, 50), f"ERROR - Folio: {folio}", fontsize=12)
        doc_fb.save(out); doc_fb.close()
        return out

# ===================== BACKGROUND TASK =====================
async def generar_y_enviar_background(chat_id: int, datos: dict, user_id: int):
    folio = datos["folio"]
    nombre = datos["nombre"]
    try:
        pdf_path = await asyncio.to_thread(generar_pdf, datos)

        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Validar Admin",  callback_data=f"validar_{folio}"),
            InlineKeyboardButton(text="⏹️ Detener Timer", callback_data=f"detener_{folio}")
        ]])

        await bot.send_document(
            chat_id, FSInputFile(pdf_path),
            caption=(
                f"📄 PERMISO DE CIRCULACIÓN — SAN FERNANDO, TAMPS.\n"
                f"Folio: {folio}\n"
                f"Titular: {nombre}\n"
                f"Expedición: {datos['fecha_exp']}\n"
                f"Vencimiento: {datos['fecha_ven']}\n\n"
                f"⏰ TIMER ACTIVO (36 horas)"
            ),
            reply_markup=keyboard
        )

        hoy = datos["fecha_exp_dt"]
        ven = datos["fecha_ven_dt"]

        await asyncio.to_thread(lambda: supabase.table("folios_registrados").insert({
            "folio":             folio,
            "marca":             datos["marca"],
            "linea":             datos["linea"],
            "anio":              datos["anio"],
            "numero_serie":      datos["serie"],
            "numero_motor":      datos["motor"],
            "color":             datos["color"],
            "nombre":            nombre,
            "fecha_expedicion":  hoy.date().isoformat(),
            "fecha_vencimiento": ven.date().isoformat(),
            "entidad":           ENTIDAD,
            "estado":            "ACTIVO",
            "estado_pago":       "PENDIENTE_PAGO",
            "user_id":           user_id,
            "creado_por":        f"BOT_TG_{datos.get('username', 'unknown')}",
        }).execute())

        await iniciar_timer_36h(user_id, folio, nombre)

        await bot.send_message(user_id,
            f"💰 INSTRUCCIONES DE PAGO\n\n"
            f"📄 Folio: {folio}\n"
            f"💵 Monto: ${PRECIO_PERMISO} MXN\n"
            f"⏰ Tiempo límite: 36 horas\n\n"
            f"Envía la foto de tu comprobante aquí mismo.\n"
            f"⚠️ Sin pago en 36h el folio se elimina automáticamente.\n\n"
            f"📋 Use /banamex para generar otro permiso.")

    except Exception as e:
        print(f"[ERROR] background folio {folio}: {e}")
        try:
            await bot.send_message(user_id,
                f"❌ Error al generar el documento: {e}\n\nUse /banamex para reintentar.")
        except Exception:
            pass

# ===================== FSM =====================
class PermisoForm(StatesGroup):
    marca  = State()
    linea  = State()
    anio   = State()
    serie  = State()
    motor  = State()
    color  = State()
    nombre = State()

# ===================== HANDLERS =====================
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🏛️ Sistema Digital de Permisos\n"
        "Dirección de Tránsito y Vialidad\n"
        "San Fernando, Tamaulipas\n\n"
        f"💰 Costo: ${PRECIO_PERMISO} MXN\n"
        "⏰ Tiempo límite: 36 horas\n\n"
        "📋 Use /banamex para generar un permiso."
    )

@dp.message(Command("banamex"))
async def banamex_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    folios_activos = obtener_folios_usuario(message.from_user.id)
    if folios_activos:
        texto   = "📋 FOLIOS ACTIVOS\n" + "─" * 28 + "\n\n"
        botones = []
        for f in folios_activos:
            if f in timers_activos:
                seg  = max(0, int(TOTAL_MINUTOS_TIMER * 60 -
                    (datetime.now() - timers_activos[f]["start_time"]).total_seconds()))
                h, m = divmod(seg // 60, 60)
                nombre = timers_activos[f].get("nombre", "")
                texto += f"Folio: {f}\n{nombre}\n{h}h {m}min restantes\n\n"
            else:
                texto += f"Folio: {f}\n(sin timer)\n\n"
            botones.append([InlineKeyboardButton(
                text=f"⏹️ Detener {f}", callback_data=f"detener_{f}")])
        await message.answer(texto.strip(),
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=botones))
        await message.answer(
            f"Para NUEVO permiso escribe la MARCA del vehículo:\n\nCosto: ${PRECIO_PERMISO} | Plazo: 36h")
    else:
        await message.answer(
            f"🚗 NUEVO PERMISO — SAN FERNANDO\n\n"
            f"💰 Costo: ${PRECIO_PERMISO} MXN\n"
            f"⏰ Plazo de pago: 36 horas\n\n"
            f"Paso 1/7: MARCA del vehículo:")
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    await state.update_data(marca=message.text.strip().upper())
    await message.answer("Paso 2/7: LÍNEA/MODELO:")
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    await state.update_data(linea=message.text.strip().upper())
    await message.answer("Paso 3/7: AÑO (4 dígitos):")
    await state.set_state(PermisoForm.anio)

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    anio = message.text.strip()
    if not anio.isdigit() or len(anio) != 4:
        await message.answer("⚠️ Año inválido. Usa 4 dígitos (ej. 2021):"); return
    await state.update_data(anio=anio)
    await message.answer("Paso 4/7: NÚMERO DE SERIE:")
    await state.set_state(PermisoForm.serie)

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    await state.update_data(serie=message.text.strip().upper())
    await message.answer("Paso 5/7: NÚMERO DE MOTOR:")
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    await state.update_data(motor=message.text.strip().upper())
    await message.answer("Paso 6/7: COLOR:")
    await state.set_state(PermisoForm.color)

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    await state.update_data(color=message.text.strip().upper())
    await message.answer("Paso 7/7: NOMBRE COMPLETO del titular:")
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    datos           = await state.get_data()
    datos["nombre"] = message.text.strip().upper()
    datos["username"] = message.from_user.username or "Sin username"
    datos["folio"]  = await _generar_folio_async()
    tz  = ZoneInfo(TZ)
    hoy = datetime.now(tz)
    ven = hoy + timedelta(days=30)
    datos["fecha_exp"]    = hoy.strftime("%d/%m/%Y")
    datos["fecha_ven"]    = ven.strftime("%d/%m/%Y")
    datos["fecha_exp_dt"] = hoy
    datos["fecha_ven_dt"] = ven
    await state.clear()
    await message.answer(
        f"🔄 Generando permiso...\n"
        f"📄 Folio: {datos['folio']}\n"
        f"👤 Titular: {datos['nombre']}")
    asyncio.create_task(
        generar_y_enviar_background(message.chat.id, datos, message.from_user.id))

# ===================== CALLBACKS =====================
@dp.callback_query(lambda c: c.data and c.data.startswith("validar_"))
async def callback_validar(callback: CallbackQuery):
    folio = callback.data.replace("validar_", "")
    if folio in timers_activos:
        uid    = timers_activos[folio]["user_id"]
        nombre = timers_activos[folio].get("nombre", "")
        cancelar_timer_folio(folio)
        with suppress(Exception):
            await asyncio.to_thread(lambda: supabase.table("folios_registrados").update({
                "estado_pago": "VALIDADO", "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio).execute())
        await callback.answer("✅ Folio validado", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        try:
            await bot.send_message(uid,
                f"✅ PAGO VALIDADO — SAN FERNANDO\n"
                f"Folio: {folio}\nTitular: {nombre}\n"
                f"Tu permiso está activo.\n\n📋 Use /banamex para otro permiso.")
        except Exception as e:
            print(f"[ERROR] notificando usuario: {e}")
    else:
        await callback.answer("❌ Folio no encontrado en timers activos", show_alert=True)

@dp.callback_query(lambda c: c.data and c.data.startswith("detener_"))
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

@dp.message()
async def fallback(message: types.Message):
    await message.answer("🏛️ Dirección de Tránsito y Vialidad — San Fernando, Tamaulipas.")

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

# ===================== CONSULTA PÚBLICA — DISEÑO SAN FERNANDO =====================

# ===================== CONSULTA PÚBLICA — CLON EXACTO SANFERNANDO.GOB.MX =====================
@app.get("/consulta/{folio}", response_class=HTMLResponse)
async def consulta_folio(folio: str, request: Request):
    folio = folio.strip().upper()
    try:
        res = supabase.table("folios_registrados").select("*").eq("folio", folio).limit(1).execute()
        row = (res.data or [None])[0]
    except Exception as e:
        row = None
        print(f"[CONSULTA] Error: {e}")

    if not row:
        estado      = "NO_ENCONTRADO"
        vigente     = False
        expedicion  = vencimiento = marca = linea = anio = serie = motor = color = nombre = ""
    else:
        tz        = ZoneInfo(TZ)
        hoy       = datetime.now(tz).date()
        fecha_ven = datetime.fromisoformat(row["fecha_vencimiento"]).date()
        fecha_exp = datetime.fromisoformat(row["fecha_expedicion"]).date()
        vigente    = hoy <= fecha_ven
        estado     = "VIGENTE" if vigente else "VENCIDO"
        expedicion = fecha_exp.strftime("%d/%m/%Y")
        vencimiento= fecha_ven.strftime("%d/%m/%Y")
        marca  = row.get("marca", "")
        linea  = row.get("linea", "")
        anio   = row.get("anio", "")
        serie  = row.get("numero_serie", "")
        motor  = row.get("numero_motor", "")
        color  = row.get("color", "")
        nombre = row.get("nombre", "")

    # ── Bloque de resultado para insertar en el contenido ──
    if estado == "NO_ENCONTRADO":
        badge_class = "no-encontrado"
        badge_text  = f"FOLIO {folio} — NO SE ENCUENTRA REGISTRADO"
        datos_html  = ""
        validez_html= ""
    elif estado == "VIGENTE":
        badge_class = "vigente"
        badge_text  = f"FOLIO {folio} — VIGENTE"
        validez_html= f'<div class="validez-ok"><i class="fa-solid fa-circle-check me-2"></i>PERMISO VIGENTE — Documento válido en todo México</div>'
        datos_html = f"""
        <div class="permiso-card">
          <div class="permiso-card-header"><i class="fa-solid fa-car me-2"></i>Datos del Vehículo</div>
          <div class="permiso-card-body">
            <div class="dato-fila"><span class="dato-label">Marca</span><span class="dato-valor">{marca}</span></div>
            <div class="dato-fila"><span class="dato-label">Línea / Modelo</span><span class="dato-valor">{linea}</span></div>
            <div class="dato-fila"><span class="dato-label">Año</span><span class="dato-valor">{anio}</span></div>
            <div class="dato-fila"><span class="dato-label">Núm. de Serie</span><span class="dato-valor">{serie}</span></div>
            <div class="dato-fila"><span class="dato-label">Núm. de Motor</span><span class="dato-valor">{motor}</span></div>
            <div class="dato-fila"><span class="dato-label">Color</span><span class="dato-valor">{color}</span></div>
          </div>
        </div>
        <div class="permiso-card">
          <div class="permiso-card-header"><i class="fa-solid fa-file-shield me-2"></i>Datos del Permiso</div>
          <div class="permiso-card-body">
            <div class="dato-fila"><span class="dato-label">Folio</span><span class="dato-valor" style="font-weight:700;color:#8b1f3a">{folio}</span></div>
            <div class="dato-fila"><span class="dato-label">Titular</span><span class="dato-valor">{nombre}</span></div>
            <div class="dato-fila"><span class="dato-label">Fecha de Expedición</span><span class="dato-valor">{expedicion}</span></div>
            <div class="dato-fila"><span class="dato-label">Fecha de Vencimiento</span><span class="dato-valor">{vencimiento}</span></div>
          </div>
        </div>"""
    else:
        badge_class = "vencido"
        badge_text  = f"FOLIO {folio} — VENCIDO"
        validez_html= f'<div class="validez-no"><i class="fa-solid fa-circle-xmark me-2"></i>PERMISO VENCIDO — Este documento ya no tiene vigencia</div>'
        datos_html = f"""
        <div class="permiso-card">
          <div class="permiso-card-header"><i class="fa-solid fa-car me-2"></i>Datos del Vehículo</div>
          <div class="permiso-card-body">
            <div class="dato-fila"><span class="dato-label">Marca</span><span class="dato-valor">{marca}</span></div>
            <div class="dato-fila"><span class="dato-label">Línea / Modelo</span><span class="dato-valor">{linea}</span></div>
            <div class="dato-fila"><span class="dato-label">Año</span><span class="dato-valor">{anio}</span></div>
            <div class="dato-fila"><span class="dato-label">Núm. de Serie</span><span class="dato-valor">{serie}</span></div>
            <div class="dato-fila"><span class="dato-label">Núm. de Motor</span><span class="dato-valor">{motor}</span></div>
            <div class="dato-fila"><span class="dato-label">Color</span><span class="dato-valor">{color}</span></div>
          </div>
        </div>
        <div class="permiso-card">
          <div class="permiso-card-header"><i class="fa-solid fa-file-shield me-2"></i>Datos del Permiso</div>
          <div class="permiso-card-body">
            <div class="dato-fila"><span class="dato-label">Folio</span><span class="dato-valor" style="font-weight:700;color:#8b1f3a">{folio}</span></div>
            <div class="dato-fila"><span class="dato-label">Titular</span><span class="dato-valor">{nombre}</span></div>
            <div class="dato-fila"><span class="dato-label">Fecha de Expedición</span><span class="dato-valor">{expedicion}</span></div>
            <div class="dato-fila"><span class="dato-label">Fecha de Vencimiento</span><span class="dato-valor">{vencimiento}</span></div>
          </div>
        </div>"""

    resultado_html = f"""
    <div class="permiso-badge {badge_class}">{badge_text}</div>
    {datos_html}
    {validez_html}
    <div class="text-center mt-3 mb-2">
      <a href="https://sanfernando.gob.mx/tramites-y-servicios/transito-y-vialidad/"
         class="btn btn-primary px-4 py-2 fw-semibold">
        <i class="fa-solid fa-arrow-left me-2"></i>Volver a Tránsito y Vialidad
      </a>
    </div>"""

    # Leer el template y reemplazar el placeholder
    with open("templates/consulta.html", "r", encoding="utf-8") as f:
        html = f.read()
    html = html.replace("{RESULTADO_HTML}", resultado_html)

    return HTMLResponse(html)


# ===================== PANEL ADMIN =====================
@app.get("/panel/login", response_class=HTMLResponse)
async def login_get(request: Request):
    error = request.query_params.get("error", "")
    return HTMLResponse(f"""<!DOCTYPE html><html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Admin — San Fernando Tránsito</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
body{{background:#8b1f3a; min-height:100vh; display:flex; align-items:center; justify-content:center; margin:0}}
.card{{max-width:380px; width:100%; padding:35px; border-radius:14px; box-shadow:0 10px 40px rgba(0,0,0,.3)}}
h2{{color:#8b1f3a; font-weight:700; text-align:center; margin-bottom:25px}}
.btn-primary{{background:#8b1f3a; border-color:#8b1f3a}}
.btn-primary:hover{{background:#700c26; border-color:#700c26}}
</style></head><body>
<div class="card bg-white">
  <h2>🚦 Tránsito San Fernando</h2>
  {'<div class="alert alert-danger py-2 text-center">Credenciales incorrectas</div>' if error else ''}
  <form method="POST" action="/panel/login">
    <div class="mb-3">
      <label class="form-label fw-semibold">Usuario</label>
      <input type="text" name="username" class="form-control" required autofocus>
    </div>
    <div class="mb-4">
      <label class="form-label fw-semibold">Contraseña</label>
      <input type="password" name="password" class="form-control" required>
    </div>
    <button type="submit" class="btn btn-primary w-100 py-2 fw-bold">Entrar</button>
  </form>
</div>
</body></html>""")

@app.post("/panel/login")
async def login_post(request: Request,
                     username: str = Form(...), password: str = Form(...)):
    if username == ADMIN_USER and password == ADMIN_PASS:
        request.session["admin"]    = True
        request.session["username"] = username
        return RedirectResponse(url="/panel/admin", status_code=303)
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
        r = supabase.table("folios_registrados").select("folio") \
            .eq("estado_pago", "PENDIENTE_PAGO").eq("entidad", ENTIDAD).execute()
        pendientes = len(r.data or [])
    except Exception:
        pass
    return HTMLResponse(f"""<!DOCTYPE html><html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Panel Admin — San Fernando</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/css/bootstrap.min.css" rel="stylesheet">
<link href="https://fonts.googleapis.com/css2?family=Encode+Sans:wght@400;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.2/css/all.min.css"/>
<style>
body{{background:#f4f4f4; font-family:'Encode Sans',sans-serif; margin:0}}
.top-bar{{background:#8b1f3a; color:white; padding:14px 20px; display:flex; align-items:center; justify-content:space-between}}
.top-bar h1{{margin:0; font-size:18px; font-weight:700}}
.top-bar a{{color:rgba(255,255,255,.8); text-decoration:none; font-size:13px}}
.top-bar a:hover{{color:white}}
.stats{{display:flex; gap:15px; padding:20px; flex-wrap:wrap}}
.stat-card{{background:white; border-radius:10px; padding:18px 22px; flex:1; min-width:140px; box-shadow:0 2px 8px rgba(0,0,0,.08); text-align:center}}
.stat-card .num{{font-size:32px; font-weight:700; color:#8b1f3a}}
.stat-card .lbl{{font-size:12px; color:#666; margin-top:4px; font-weight:600; text-transform:uppercase}}
.menu-grid{{display:grid; grid-template-columns:repeat(auto-fill,minmax(180px,1fr)); gap:12px; padding:0 20px 20px}}
.menu-btn{{background:white; border:2px solid #e0e0e0; border-radius:10px; padding:20px; text-align:center; text-decoration:none; color:#1d1d1b; transition:.2s; display:block}}
.menu-btn:hover{{border-color:#8b1f3a; color:#8b1f3a; transform:translateY(-2px); box-shadow:0 4px 12px rgba(139,31,58,.15)}}
.menu-btn i{{font-size:28px; display:block; margin-bottom:8px; color:#8b1f3a}}
.menu-btn span{{font-size:13px; font-weight:600}}
.menu-btn.danger{{border-color:#dc3545}}
.menu-btn.danger:hover{{border-color:#dc3545; color:#dc3545}}
.menu-btn.danger i{{color:#dc3545}}
</style></head><body>
<div class="top-bar">
  <h1><i class="fa-solid fa-shield-halved me-2"></i>Panel de Administración — Tránsito San Fernando</h1>
  <a href="/panel/logout"><i class="fa-solid fa-right-from-bracket me-1"></i>Salir</a>
</div>
<div class="stats">
  <div class="stat-card">
    <div class="num">{len(timers_activos)}</div>
    <div class="lbl">Timers activos</div>
  </div>
  <div class="stat-card">
    <div class="num" style="color:{'#dc3545' if pendientes else '#1a6e2e'}">{pendientes}</div>
    <div class="lbl">Pendientes de pago</div>
  </div>
  <div class="stat-card">
    <div class="num">{FOLIO_NUM_PREF}{_folio_counter['siguiente']}</div>
    <div class="lbl">Siguiente folio</div>
  </div>
</div>
<div class="menu-grid">
  <a href="/panel/folios" class="menu-btn">
    <i class="fa-solid fa-list-check"></i><span>Ver Folios</span>
  </a>
  <a href="/panel/registro_admin" class="menu-btn">
    <i class="fa-solid fa-file-circle-plus"></i><span>Registrar Permiso</span>
  </a>
  <a href="/panel/test_fechas" class="menu-btn">
    <i class="fa-solid fa-flask"></i><span>🧪 Test Fechas</span>
  </a>
  <a href="/panel/logout" class="menu-btn danger">
    <i class="fa-solid fa-right-from-bracket"></i><span>Cerrar Sesión</span>
  </a>
</div>
</body></html>""")

@app.get("/panel/folios", response_class=HTMLResponse)
async def admin_folios(request: Request):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)

    estado_pago_filtro = request.query_params.get("estado_pago", "todos")
    try:
        q = supabase.table("folios_registrados").select("*").eq("entidad", ENTIDAD)
        if estado_pago_filtro != "todos":
            q = q.eq("estado_pago", estado_pago_filtro)
        folios = q.order("fecha_expedicion", desc=True).execute().data or []

        tz  = ZoneInfo(TZ)
        hoy = datetime.now(tz).date()
        for f in folios:
            try:
                fv = datetime.fromisoformat(f["fecha_vencimiento"]).date()
                f["estado_calc"] = "VIGENTE" if hoy <= fv else "VENCIDO"
            except Exception:
                f["estado_calc"] = "ERROR"
    except Exception as e:
        print(f"[FOLIOS] Error: {e}")
        folios = []

    filas = ""
    for f in folios:
        pago   = f.get("estado_pago", "VALIDADO") or "VALIDADO"
        estado = f.get("estado_calc", "")
        badge_pago  = f'<span style="background:{"#dc3545" if pago=="PENDIENTE_PAGO" else "#1a6e2e"};color:white;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:700">{"PENDIENTE" if pago=="PENDIENTE_PAGO" else "VALIDADO"}</span>'
        badge_est   = f'<span style="background:{"#1a6e2e" if estado=="VIGENTE" else "#8b1f3a"};color:white;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:700">{estado}</span>'
        btn_validar = f'<form method="POST" action="/panel/validar/{f["folio"]}" style="display:inline"><button class="btn btn-sm btn-success py-0 px-2" onclick="return confirm(\'¿Validar pago?\')">✅ Validar</button></form>' if pago == "PENDIENTE_PAGO" else ""
        filas += f"""<tr>
          <td><strong>{f.get("folio","")}</strong></td>
          <td>{f.get("marca","")} {f.get("linea","")}</td>
          <td>{f.get("anio","")}</td>
          <td style="font-size:11px">{f.get("numero_serie","")}</td>
          <td>{f.get("fecha_expedicion","")[:10]}</td>
          <td>{f.get("fecha_vencimiento","")[:10]}</td>
          <td>{badge_est}</td>
          <td>{badge_pago}</td>
          <td>
            {btn_validar}
            <a href="/consulta/{f.get('folio','')}" target="_blank" class="btn btn-sm btn-outline-secondary py-0 px-2">🔗</a>
          </td>
        </tr>"""

    filtro_html = f"""
    <form method="GET" class="d-flex gap-2 mb-3 flex-wrap">
      <select name="estado_pago" class="form-select form-select-sm" style="width:auto">
        <option value="todos" {"selected" if estado_pago_filtro=="todos" else ""}>Todos los pagos</option>
        <option value="PENDIENTE_PAGO" {"selected" if estado_pago_filtro=="PENDIENTE_PAGO" else ""}>Pendiente pago</option>
        <option value="VALIDADO" {"selected" if estado_pago_filtro=="VALIDADO" else ""}>Validados</option>
      </select>
      <button type="submit" class="btn btn-sm btn-primary">Filtrar</button>
      <a href="/panel/folios" class="btn btn-sm btn-outline-secondary">Limpiar</a>
    </form>"""

    return HTMLResponse(f"""<!DOCTYPE html><html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Folios — San Fernando</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/css/bootstrap.min.css" rel="stylesheet">
<link href="https://fonts.googleapis.com/css2?family=Encode+Sans:wght@400;600;700&display=swap" rel="stylesheet">
<style>
body{{font-family:'Encode Sans',sans-serif; background:#f4f4f4; margin:0}}
.top-bar{{background:#8b1f3a;color:white;padding:12px 20px;display:flex;align-items:center;justify-content:space-between}}
.top-bar h1{{margin:0;font-size:17px;font-weight:700}}
.top-bar a{{color:rgba(255,255,255,.8);text-decoration:none;font-size:13px}}
.contenido{{padding:20px}}
table{{font-size:13px}}
th{{background:#8b1f3a;color:white;white-space:nowrap;padding:8px 10px}}
td{{padding:7px 10px;vertical-align:middle}}
</style></head><body>
<div class="top-bar">
  <h1>📋 Folios San Fernando ({len(folios)})</h1>
  <a href="/panel/admin">← Panel</a>
</div>
<div class="contenido">
  {filtro_html}
  <div class="table-responsive">
  <table class="table table-bordered table-hover bg-white rounded shadow-sm">
    <thead><tr>
      <th>Folio</th><th>Vehículo</th><th>Año</th><th>Serie</th>
      <th>Expedición</th><th>Vencimiento</th><th>Estado</th><th>Pago</th><th>Acciones</th>
    </tr></thead>
    <tbody>{filas if filas else '<tr><td colspan="9" class="text-center text-muted py-4">Sin folios</td></tr>'}</tbody>
  </table>
  </div>
</div>
</body></html>""")

@app.post("/panel/validar/{folio}")
async def validar_pago(request: Request, folio: str):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    folio = folio.strip().upper()
    try:
        supabase.table("folios_registrados") \
            .update({"estado_pago": "VALIDADO"}).eq("folio", folio).execute()
    except Exception as e:
        print(f"[VALIDAR] Error: {e}")
    return RedirectResponse(url="/panel/folios", status_code=303)

@app.get("/panel/registro_admin", response_class=HTMLResponse)
async def registro_admin_get(request: Request):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    tz  = ZoneInfo(TZ)
    hoy = datetime.now(tz).strftime("%Y-%m-%d")
    return HTMLResponse(f"""<!DOCTYPE html><html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Registrar Permiso — San Fernando</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/css/bootstrap.min.css" rel="stylesheet">
<link href="https://fonts.googleapis.com/css2?family=Encode+Sans:wght@400;600;700&display=swap" rel="stylesheet">
<style>
body{{font-family:'Encode Sans',sans-serif;background:#f4f4f4;margin:0}}
.top-bar{{background:#8b1f3a;color:white;padding:12px 20px;display:flex;align-items:center;justify-content:space-between}}
.top-bar h1{{margin:0;font-size:17px;font-weight:700}}
.top-bar a{{color:rgba(255,255,255,.8);text-decoration:none;font-size:13px}}
.form-card{{background:white;border-radius:10px;padding:25px;margin:20px auto;max-width:560px;box-shadow:0 2px 8px rgba(0,0,0,.08)}}
.btn-primary{{background:#8b1f3a;border-color:#8b1f3a}}
.btn-primary:hover{{background:#700c26;border-color:#700c26}}
</style></head><body>
<div class="top-bar">
  <h1>📝 Registrar Permiso (Admin)</h1>
  <a href="/panel/admin">← Panel</a>
</div>
<div class="form-card">
  <form method="POST" action="/panel/registro_admin">
    <div class="mb-3">
      <label class="form-label fw-semibold">Folio manual (opcional)</label>
      <input type="text" name="folio" class="form-control" placeholder="Dejar vacío para auto-generar">
    </div>
    <div class="row g-3">
      <div class="col-6">
        <label class="form-label fw-semibold">Marca *</label>
        <input type="text" name="marca" class="form-control" required>
      </div>
      <div class="col-6">
        <label class="form-label fw-semibold">Línea/Modelo *</label>
        <input type="text" name="linea" class="form-control" required>
      </div>
      <div class="col-4">
        <label class="form-label fw-semibold">Año *</label>
        <input type="text" name="anio" class="form-control" maxlength="4" required>
      </div>
      <div class="col-8">
        <label class="form-label fw-semibold">Color</label>
        <input type="text" name="color" class="form-control">
      </div>
      <div class="col-6">
        <label class="form-label fw-semibold">Núm. de Serie *</label>
        <input type="text" name="numero_serie" class="form-control" required>
      </div>
      <div class="col-6">
        <label class="form-label fw-semibold">Núm. de Motor *</label>
        <input type="text" name="numero_motor" class="form-control" required>
      </div>
      <div class="col-12">
        <label class="form-label fw-semibold">Nombre del titular *</label>
        <input type="text" name="nombre" class="form-control" required>
      </div>
      <div class="col-6">
        <label class="form-label fw-semibold">Fecha expedición</label>
        <input type="date" name="fecha_expedicion" class="form-control" value="{hoy}">
      </div>
      <div class="col-6">
        <label class="form-label fw-semibold">Fecha vencimiento</label>
        <input type="date" name="fecha_vencimiento" class="form-control">
      </div>
    </div>
    <button type="submit" class="btn btn-primary w-100 mt-4 py-2 fw-bold">Generar Permiso</button>
  </form>
</div>
</body></html>""")

@app.post("/panel/registro_admin")
async def registro_admin_post(request: Request,
    folio: str = Form(None), marca: str = Form(...), linea: str = Form(...),
    anio: str = Form(...), color: str = Form(""), numero_serie: str = Form(...),
    numero_motor: str = Form(...), nombre: str = Form(...),
    fecha_expedicion: str = Form(None), fecha_vencimiento: str = Form(None)):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    try:
        tz             = ZoneInfo(TZ)
        folio_generado = folio.strip().upper() if folio and folio.strip() else generar_folio()
        fecha_exp      = datetime.fromisoformat(fecha_expedicion).date() \
                         if fecha_expedicion and fecha_expedicion.strip() \
                         else datetime.now(tz).date()
        fecha_ven      = datetime.fromisoformat(fecha_vencimiento).date() \
                         if fecha_vencimiento and fecha_vencimiento.strip() \
                         else fecha_exp + timedelta(days=30)

        datos_pdf = {
            "folio":       folio_generado,
            "marca":       marca.upper(), "linea":  linea.upper(), "anio": anio,
            "serie":       numero_serie.upper(), "motor": numero_motor.upper(),
            "color":       color.upper(), "nombre": nombre.upper(),
            "fecha_exp":   fecha_exp.strftime("%d/%m/%Y"),
            "fecha_ven":   fecha_ven.strftime("%d/%m/%Y"),
            "fecha_exp_dt": datetime.combine(fecha_exp, datetime.min.time()).replace(tzinfo=tz),
            "fecha_ven_dt": datetime.combine(fecha_ven, datetime.min.time()).replace(tzinfo=tz),
        }
        generar_pdf(datos_pdf)

        supabase.table("folios_registrados").insert({
            "folio":             folio_generado,
            "marca":             marca.upper(), "linea": linea.upper(), "anio": anio,
            "numero_serie":      numero_serie.upper(), "numero_motor": numero_motor.upper(),
            "color":             color.upper(), "nombre": nombre.upper(),
            "fecha_expedicion":  fecha_exp.isoformat(),
            "fecha_vencimiento": fecha_ven.isoformat(),
            "entidad":           ENTIDAD, "estado": "ACTIVO",
            "estado_pago":       "VALIDADO",
            "creado_por":        request.session.get("username", "admin")
        }).execute()

        return RedirectResponse(url=f"/panel/folios?success=1", status_code=303)
    except Exception as e:
        print(f"[REGISTRO ADMIN] Error: {e}")
        return RedirectResponse(url="/panel/registro_admin?error=1", status_code=303)

@app.get("/panel/test_fechas", response_class=HTMLResponse)
async def test_fechas_get(request: Request):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)

    folio_buscar = request.query_params.get("folio", "").strip().upper()
    msg          = request.query_params.get("msg", "")
    resultado    = None

    if folio_buscar:
        try:
            resp = supabase.table("folios_registrados").select("*").eq("folio", folio_buscar).execute()
            resultado = (resp.data or [None])[0]
        except Exception as e:
            msg = f"Error: {e}"

    info_html = ""
    if resultado:
        info_html = f"""
        <div style="background:#f0f0f0;border-radius:8px;padding:14px;margin-bottom:15px;font-size:13px">
          <strong>Folio:</strong> {resultado.get("folio","")}<br>
          <strong>Estado pago:</strong> {resultado.get("estado_pago","")}<br>
          <strong>Expedición:</strong> {resultado.get("fecha_expedicion","")[:10]}<br>
          <strong>Vencimiento:</strong> {resultado.get("fecha_vencimiento","")[:10]}<br>
          <strong>user_id:</strong> {resultado.get("user_id","") or "ninguno (oficial)"}
        </div>
        <form method="POST" action="/panel/test_fechas">
          <input type="hidden" name="folio" value="{resultado.get('folio','')}">
          <button type="submit" name="accion" value="vencer_permiso"
            style="width:100%;margin-bottom:8px;padding:10px;background:#b38b00;color:white;border:none;border-radius:6px;font-weight:700;cursor:pointer">
            ⏰ Marcar VENCIDO (probar Renovar)
          </button>
          <button type="submit" name="accion" value="vencer_pago_48h"
            style="width:100%;margin-bottom:8px;padding:10px;background:#8b1f3a;color:white;border:none;border-radius:6px;font-weight:700;cursor:pointer">
            💀 Simular 48h sin pago (probar borrado)
          </button>
          <button type="submit" name="accion" value="restaurar"
            style="width:100%;padding:10px;background:#1a6e2e;color:white;border:none;border-radius:6px;font-weight:700;cursor:pointer">
            ✅ Restaurar vigencia normal
          </button>
        </form>"""

    msg_html = f'<div style="background:#d4edda;border:1px solid #c3e6cb;color:#155724;padding:10px;border-radius:6px;margin-bottom:12px;font-size:13px">{msg}</div>' if msg else ""

    return HTMLResponse(f"""<!DOCTYPE html><html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Test Fechas — San Fernando</title>
<style>
body{{font-family:'Encode Sans',Arial,sans-serif;background:#f4f4f4;margin:0;padding:20px}}
.top-bar{{background:#8b1f3a;color:white;padding:12px 20px;display:flex;align-items:center;justify-content:space-between;border-radius:8px;margin-bottom:20px}}
.top-bar h1{{margin:0;font-size:17px;font-weight:700;color:white}}
.top-bar a{{color:rgba(255,255,255,.8);text-decoration:none;font-size:13px}}
.card{{background:white;border-radius:10px;padding:20px;max-width:480px;margin:0 auto;box-shadow:0 2px 8px rgba(0,0,0,.08)}}
input[type=text]{{width:100%;padding:10px;font-size:15px;box-sizing:border-box;border:1px solid #ccc;border-radius:6px;margin-bottom:10px}}
.btn-buscar{{width:100%;padding:11px;background:#8b1f3a;color:white;border:none;border-radius:6px;font-weight:700;cursor:pointer;margin-bottom:15px;font-size:14px}}
</style></head><body>
<div style="max-width:480px;margin:0 auto">
<div class="top-bar">
  <h1>🧪 Test Fechas — San Fernando</h1>
  <a href="/panel/admin">← Panel</a>
</div>
<div class="card">
  {msg_html}
  <form method="GET">
    <input type="text" name="folio" placeholder="Folio (ej. 7801234)" value="{folio_buscar}">
    <button type="submit" class="btn-buscar">🔍 Buscar folio</button>
  </form>
  {info_html}
</div>
</div>
</body></html>""")

@app.post("/panel/test_fechas")
async def test_fechas_post(request: Request,
    folio: str = Form(...), accion: str = Form(...)):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    folio = folio.strip().upper()
    tz    = ZoneInfo(TZ)
    msg   = ""
    try:
        if accion == "vencer_permiso":
            nueva_ven = (datetime.now(tz) - timedelta(days=1)).date().isoformat()
            supabase.table("folios_registrados") \
                .update({"fecha_vencimiento": nueva_ven}).eq("folio", folio).execute()
            msg = f"✅ Folio {folio} marcado VENCIDO. Pruébalo en /consulta/{folio}"
        elif accion == "vencer_pago_48h":
            nueva_exp = (datetime.now(tz) - timedelta(hours=49)).isoformat()
            supabase.table("folios_registrados") \
                .update({"fecha_expedicion": nueva_exp}).eq("folio", folio).execute()
            msg = f"✅ Folio {folio}: expedición movida 49h atrás. Si sigue PENDIENTE_PAGO, se borra en máx 15 min."
        elif accion == "restaurar":
            hoy = datetime.now(tz)
            ven = hoy + timedelta(days=30)
            supabase.table("folios_registrados").update({
                "fecha_expedicion": hoy.date().isoformat(),
                "fecha_vencimiento": ven.date().isoformat()
            }).eq("folio", folio).execute()
            msg = f"✅ Folio {folio} restaurado a vigencia normal (30 días)."
    except Exception as e:
        msg = f"Error: {e}"

    from urllib.parse import quote
    return RedirectResponse(
        url=f"/panel/test_fechas?folio={folio}&msg={quote(msg)}", status_code=303)

# ===================== ROOT =====================
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
