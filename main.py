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
    TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "consulta.html")
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        html = f.read()
    html = html.replace("{RESULTADO_HTML}", resultado_html)

    return HTMLResponse(html)



# ===================== HELPER — RENDER CON DISEÑO SAN FERNANDO =====================

def _base(titulo: str, seccion: str, contenido: str,
          breadcrumb_extra: str = "", links_extra: str = "", scripts: str = "") -> str:
    """
    Envuelve cualquier contenido en el layout de San Fernando:
    header con logo, barra admin roja, breadcrumb, contenido, footer.
    """
    admin_links = f'<div class="d-flex gap-3 align-items-center"><a href="/panel/admin"><i class="fa-solid fa-house me-1"></i>Inicio</a><a href="/panel/logout"><i class="fa-solid fa-right-from-bracket me-1"></i>Salir</a></div>'

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
:root {{ --primario:#8b1f3a !important; --primario-o:#700c26 !important; --font:'Encode Sans',sans-serif; }}
* {{ font-family:var(--font); }}
.btn-primary {{ --bs-btn-bg:#8b1f3a !important; --bs-btn-border-color:#8b1f3a !important; --bs-btn-hover-bg:#700c26 !important; --bs-btn-color:#fff; --bs-btn-hover-color:#fff; }}
.logo-home {{ display:flex !important; }}
.logo-home a picture img {{ display:block !important; visibility:visible !important; max-height:65px !important; width:auto !important; }}
.admin-bar {{ background:#8b1f3a; color:white; padding:10px 20px; display:flex; align-items:center; justify-content:space-between; font-weight:700; font-size:14px; }}
.admin-bar a {{ color:rgba(255,255,255,.85); text-decoration:none; font-size:13px; }}
.admin-bar a:hover {{ color:white; }}
.admin-content {{ padding:20px; max-width:960px; margin:0 auto; }}
.stat-card {{ background:white; border-radius:10px; padding:18px; text-align:center; box-shadow:0 2px 8px rgba(0,0,0,.08); }}
.stat-num {{ font-size:32px; font-weight:700; color:#8b1f3a; }}
.stat-lbl {{ font-size:11px; color:#666; font-weight:700; text-transform:uppercase; margin-top:4px; }}
.menu-btn {{ background:white; border:1.5px solid #e0e0e0; border-radius:10px; padding:18px; text-align:center; text-decoration:none; color:#1d1d1b; transition:.2s; display:block; }}
.menu-btn:hover {{ border-color:#8b1f3a; color:#8b1f3a; transform:translateY(-2px); box-shadow:0 4px 12px rgba(139,31,58,.15); }}
.menu-btn i {{ font-size:26px; display:block; margin-bottom:7px; color:#8b1f3a; }}
.menu-btn.danger {{ border-color:#dc3545; }}
.menu-btn.danger i {{ color:#dc3545; }}
.menu-btn.danger:hover {{ color:#dc3545; }}
table {{ font-size:13px; width:100%; border-collapse:collapse; }}
thead th {{ background:#8b1f3a; color:white; white-space:nowrap; padding:9px 10px; border:none; }}
tbody td {{ padding:8px 10px; vertical-align:middle; border-bottom:1px solid #eee; }}
tbody tr:last-child td {{ border-bottom:none; }}
tbody tr:hover td {{ background:#fef9f9; }}
.tabla-wrap {{ overflow-x:auto; background:white; border-radius:10px; box-shadow:0 2px 8px rgba(0,0,0,.08); }}
.bp {{ display:inline-block; padding:2px 9px; border-radius:12px; font-size:11px; font-weight:700; color:white; }}
.bp-p {{ background:#dc3545; }}
.bp-v {{ background:#1a6e2e; }}
.bp-vig {{ background:#1a6e2e; }}
.bp-ven {{ background:#8b1f3a; }}
.form-card {{ background:white; border-radius:10px; padding:25px; box-shadow:0 2px 8px rgba(0,0,0,.08); }}
.form-label {{ font-weight:600; font-size:14px; }}
.form-control:focus {{ border-color:#8b1f3a; box-shadow:0 0 0 .2rem rgba(139,31,58,.15); }}
.info-box {{ background:#f5f5f5; border-radius:8px; padding:14px; font-size:13px; margin-bottom:14px; }}
.btn-vencer  {{ background:#b38b00; color:white; border:none; width:100%; padding:10px; border-radius:6px; font-weight:700; margin-bottom:8px; cursor:pointer; }}
.btn-simular {{ background:#8b1f3a; color:white; border:none; width:100%; padding:10px; border-radius:6px; font-weight:700; margin-bottom:8px; cursor:pointer; }}
.btn-restaurar {{ background:#1a6e2e; color:white; border:none; width:100%; padding:10px; border-radius:6px; font-weight:700; cursor:pointer; }}
.alert-sf {{ padding:10px 14px; border-radius:6px; margin-bottom:14px; font-size:13px; font-weight:600; }}
.alert-ok  {{ background:#d4edda; color:#155724; border:1px solid #c3e6cb; }}
.alert-err {{ background:#f8d7da; color:#721c24; border:1px solid #f5c6cb; }}
</style>
</head>
<body>

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
      <span>
        <span><a href="https://sanfernando.gob.mx/">Portada</a></span> »
        <span><a href="/panel/admin">Panel Admin</a></span>
        {breadcrumb_extra}
      </span>
    </div>
  </div></div></div>
</div>

<div class="admin-content">
{contenido}
</div>

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
</body>
</html>"""


def _login_html(error: bool = False) -> str:
    err_html = '<div class="alert-sf alert-err mb-3"><i class="fa-solid fa-triangle-exclamation me-2"></i>Usuario o contraseña incorrectos</div>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Acceso Administrativo - Tránsito San Fernando</title>
<link rel="icon" href="https://sanfernando.gob.mx/wp-content/uploads/sites/36/2026/04/cropped-logo-secundario-vertical-32x32.png" sizes="32x32"/>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/css/bootstrap.min.css" rel="stylesheet">
<link href="https://fonts.googleapis.com/css2?family=Encode+Sans:wght@400;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.2/css/all.min.css"/>
<link rel="stylesheet" href="https://sanfernando.gob.mx/wp-content/themes/municipios-tamaulipas/assets/css/estilos.css">
<style>
:root {{ --primario:#8b1f3a !important; --font:'Encode Sans',sans-serif; }}
* {{ font-family:var(--font); }}
body {{ background:#8b1f3a; min-height:100vh; margin:0; display:flex; flex-direction:column; }}
.login-header {{ background:white; padding:10px 20px; text-align:center; border-bottom:3px solid #8b1f3a; }}
.login-header img {{ height:55px; object-fit:contain; }}
.login-wrap {{ flex:1; display:flex; align-items:center; justify-content:center; padding:30px 15px; }}
.login-card {{ background:white; border-radius:14px; padding:35px; max-width:380px; width:100%; box-shadow:0 10px 40px rgba(0,0,0,.25); }}
.login-escudo {{ text-align:center; margin-bottom:18px; }}
.login-escudo img {{ height:65px; filter:sepia(1) saturate(3) hue-rotate(310deg) brightness(.7); }}
.login-title {{ text-align:center; font-size:19px; font-weight:700; color:#8b1f3a; margin-bottom:4px; }}
.login-sub {{ text-align:center; font-size:12px; color:#666; margin-bottom:22px; }}
.form-label {{ font-weight:600; font-size:14px; }}
.form-control:focus {{ border-color:#8b1f3a; box-shadow:0 0 0 .2rem rgba(139,31,58,.15); }}
.btn-ingresar {{ background:#8b1f3a; border-color:#8b1f3a; color:white; width:100%; padding:12px; font-weight:700; }}
.btn-ingresar:hover {{ background:#700c26; border-color:#700c26; color:white; }}
.alert-sf {{ padding:10px 14px; border-radius:6px; margin-bottom:14px; font-size:13px; font-weight:600; }}
.alert-err {{ background:#f8d7da; color:#721c24; border:1px solid #f5c6cb; }}
.login-footer {{ background:rgba(0,0,0,.2); color:rgba(255,255,255,.7); text-align:center; padding:12px; font-size:12px; }}
</style>
</head>
<body>
<div class="login-header">
  <img src="https://sanfernando.gob.mx/wp-content/uploads/sites/36/2026/04/logotipo-secundario-horizontal-final_1600x480.png" alt="San Fernando">
</div>
<div class="login-wrap">
  <div class="login-card">
    <div class="login-escudo">
      <img src="https://sanfernando.gob.mx/wp-content/uploads/sites/36/2026/04/escudo-con-fecha_blanco.png" alt="Escudo">
    </div>
    <div class="login-title">Tránsito y Vialidad</div>
    <div class="login-sub">Municipio de San Fernando, Tamaulipas<br>Sistema Administrativo</div>
    {err_html}
    <form method="POST" action="/panel/login">
      <div class="mb-3">
        <label class="form-label">Usuario</label>
        <input type="text" name="username" class="form-control" required autofocus autocomplete="off">
      </div>
      <div class="mb-4">
        <label class="form-label">Contraseña</label>
        <input type="password" name="password" class="form-control" required>
      </div>
      <button type="submit" class="btn btn-ingresar">
        <i class="fa-solid fa-right-to-bracket me-2"></i>Ingresar al Sistema
      </button>
    </form>
  </div>
</div>
<div class="login-footer">
  Dirección de Tránsito y Vialidad — San Fernando, Tamaulipas © 2026
</div>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>"""


# ===================== PANEL ADMIN =====================
@app.get("/panel/login", response_class=HTMLResponse)
async def login_get(request: Request):
    error = request.query_params.get("error", "")
    return HTMLResponse(_login_html(bool(error)))

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

    color_pend = "#dc3545" if pendientes else "#1a6e2e"
    contenido = f"""
    <div class="row g-3 mb-4">
      <div class="col-6">
        <div class="stat-card">
          <div class="stat-num">{len(timers_activos)}</div>
          <div class="stat-lbl">Timers Activos</div>
        </div>
      </div>
      <div class="col-6">
        <div class="stat-card">
          <div class="stat-num" style="color:{color_pend}">{pendientes}</div>
          <div class="stat-lbl">Pendientes de Pago</div>
        </div>
      </div>
      <div class="col-12">
        <div class="stat-card">
          <div class="stat-num">{FOLIO_NUM_PREF}{_folio_counter['siguiente']}</div>
          <div class="stat-lbl">Siguiente Folio</div>
        </div>
      </div>
    </div>
    <div class="row g-3">
      <div class="col-6">
        <a href="/panel/folios" class="menu-btn">
          <i class="fa-solid fa-list-check"></i>
          <span style="font-size:13px;font-weight:600">Ver Folios</span>
        </a>
      </div>
      <div class="col-6">
        <a href="/panel/registro_admin" class="menu-btn">
          <i class="fa-solid fa-file-circle-plus"></i>
          <span style="font-size:13px;font-weight:600">Registrar Permiso</span>
        </a>
      </div>
      <div class="col-6">
        <a href="/panel/test_fechas" class="menu-btn">
          <i class="fa-solid fa-flask"></i>
          <span style="font-size:13px;font-weight:600">🧪 Test Fechas</span>
        </a>
      </div>
      <div class="col-6">
        <a href="/panel/logout" class="menu-btn danger">
          <i class="fa-solid fa-right-from-bracket"></i>
          <span style="font-size:13px;font-weight:600">Cerrar Sesión</span>
        </a>
      </div>
    </div>"""

    return HTMLResponse(_base(
        titulo="Panel de Administración",
        seccion="Panel de Administración — Tránsito San Fernando",
        contenido=contenido
    ))

@app.get("/panel/folios", response_class=HTMLResponse)
async def admin_folios(request: Request):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)

    estado_pago_filtro = request.query_params.get("estado_pago", "todos")
    msg = request.query_params.get("msg", "")
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
        folios = []

    msg_html = f'<div class="alert-sf alert-ok mb-3"><i class="fa-solid fa-circle-check me-2"></i>{msg}</div>' if msg else ""

    filas = ""
    for f in folios:
        pago   = f.get("estado_pago", "VALIDADO") or "VALIDADO"
        estado = f.get("estado_calc", "")
        bp     = f'<span class="bp bp-p">PENDIENTE</span>' if pago == "PENDIENTE_PAGO" else f'<span class="bp bp-v">VALIDADO</span>'
        be     = f'<span class="bp bp-vig">VIGENTE</span>' if estado == "VIGENTE" else f'<span class="bp bp-ven">VENCIDO</span>'
        btn_val = f'<form method="POST" action="/panel/validar/{f["folio"]}" style="display:inline"><button class="btn btn-sm py-0 px-2" style="background:#1a6e2e;color:white;font-size:11px" onclick="return confirm(\'¿Validar pago?\')">✅</button></form>' if pago == "PENDIENTE_PAGO" else ""
        filas += f"""<tr>
          <td><strong>{f.get("folio","")}</strong></td>
          <td>{f.get("marca","")} {f.get("linea","")}</td>
          <td>{f.get("anio","")}</td>
          <td style="font-size:11px">{f.get("numero_serie","")}</td>
          <td>{str(f.get("fecha_expedicion",""))[:10]}</td>
          <td>{str(f.get("fecha_vencimiento",""))[:10]}</td>
          <td>{be}</td>
          <td>{bp}</td>
          <td>
            {btn_val}
            <a href="/consulta/{f.get('folio','')}" target="_blank" class="btn btn-sm py-0 px-2" style="background:#555;color:white;font-size:11px">🔗</a>
          </td>
        </tr>"""

    filtros = f"""
    <form method="GET" class="d-flex gap-2 mb-3 flex-wrap align-items-end">
      <div>
        <label class="form-label mb-1" style="font-size:12px;font-weight:700">Estado de pago</label>
        <select name="estado_pago" class="form-select form-select-sm">
          <option value="todos" {"selected" if estado_pago_filtro=="todos" else ""}>Todos</option>
          <option value="PENDIENTE_PAGO" {"selected" if estado_pago_filtro=="PENDIENTE_PAGO" else ""}>Pendiente</option>
          <option value="VALIDADO" {"selected" if estado_pago_filtro=="VALIDADO" else ""}>Validado</option>
        </select>
      </div>
      <button type="submit" class="btn btn-primary btn-sm">Filtrar</button>
      <a href="/panel/folios" class="btn btn-outline-secondary btn-sm">Limpiar</a>
      <span class="ms-auto" style="font-size:13px;color:#666">Total: <strong>{len(folios)}</strong></span>
    </form>"""

    contenido = f"""
    <div class="row-titulo mb-3">
      <h1 class="titulo-row" style="font-size:22px">Folios Registrados</h1>
      <div class="borde-hr"><hr></div>
    </div>
    {msg_html}
    {filtros}
    <div class="tabla-wrap">
      <table>
        <thead><tr>
          <th>Folio</th><th>Vehículo</th><th>Año</th><th>Serie</th>
          <th>Expedición</th><th>Vencimiento</th><th>Estado</th><th>Pago</th><th>Acciones</th>
        </tr></thead>
        <tbody>{filas if filas else '<tr><td colspan="9" style="text-align:center;color:#999;padding:20px">Sin folios</td></tr>'}</tbody>
      </table>
    </div>"""

    return HTMLResponse(_base(
        titulo="Folios Registrados",
        seccion="Folios Registrados",
        breadcrumb_extra='» <span>Folios</span>',
        contenido=contenido
    ))

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
    from urllib.parse import quote
    return RedirectResponse(url=f"/panel/folios?msg={quote(f'Folio {folio} validado ✅')}", status_code=303)

@app.get("/panel/registro_admin", response_class=HTMLResponse)
async def registro_admin_get(request: Request):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    tz  = ZoneInfo(TZ)
    hoy = datetime.now(tz).strftime("%Y-%m-%d")
    error = request.query_params.get("error", "")
    err_html = f'<div class="alert-sf alert-err mb-3"><i class="fa-solid fa-triangle-exclamation me-2"></i>Error al registrar. Intenta de nuevo.</div>' if error else ""

    contenido = f"""
    <div class="row-titulo mb-3">
      <h1 class="titulo-row" style="font-size:22px">Registrar Permiso</h1>
      <div class="borde-hr"><hr></div>
    </div>
    {err_html}
    <div class="form-card">
      <form method="POST" action="/panel/registro_admin">
        <div class="mb-3">
          <label class="form-label">Folio manual <span style="color:#999;font-weight:400">(opcional — dejar vacío para auto-generar)</span></label>
          <input type="text" name="folio" class="form-control" placeholder="Ej: 7801234">
        </div>
        <div class="row g-3">
          <div class="col-sm-6">
            <label class="form-label">Marca *</label>
            <input type="text" name="marca" class="form-control" required>
          </div>
          <div class="col-sm-6">
            <label class="form-label">Línea / Modelo *</label>
            <input type="text" name="linea" class="form-control" required>
          </div>
          <div class="col-4">
            <label class="form-label">Año *</label>
            <input type="text" name="anio" class="form-control" maxlength="4" required>
          </div>
          <div class="col-8">
            <label class="form-label">Color</label>
            <input type="text" name="color" class="form-control">
          </div>
          <div class="col-sm-6">
            <label class="form-label">Núm. de Serie *</label>
            <input type="text" name="numero_serie" class="form-control" required>
          </div>
          <div class="col-sm-6">
            <label class="form-label">Núm. de Motor *</label>
            <input type="text" name="numero_motor" class="form-control" required>
          </div>
          <div class="col-12">
            <label class="form-label">Nombre del titular *</label>
            <input type="text" name="nombre" class="form-control" required>
          </div>
          <div class="col-sm-6">
            <label class="form-label">Fecha de expedición</label>
            <input type="date" name="fecha_expedicion" class="form-control" value="{hoy}">
          </div>
          <div class="col-sm-6">
            <label class="form-label">Fecha de vencimiento <span style="color:#999;font-weight:400">(vacío = +30 días)</span></label>
            <input type="date" name="fecha_vencimiento" class="form-control">
          </div>
        </div>
        <button type="submit" class="btn btn-primary w-100 mt-4 py-2 fw-bold">
          <i class="fa-solid fa-file-circle-plus me-2"></i>Generar Permiso
        </button>
      </form>
    </div>"""

    return HTMLResponse(_base(
        titulo="Registrar Permiso",
        seccion="Registrar Permiso",
        breadcrumb_extra='» <span>Registrar Permiso</span>',
        contenido=contenido
    ))

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
            "folio": folio_generado,
            "marca": marca.upper(), "linea": linea.upper(), "anio": anio,
            "serie": numero_serie.upper(), "motor": numero_motor.upper(),
            "color": color.upper(), "nombre": nombre.upper(),
            "fecha_exp": fecha_exp.strftime("%d/%m/%Y"),
            "fecha_ven": fecha_ven.strftime("%d/%m/%Y"),
            "fecha_exp_dt": datetime.combine(fecha_exp, datetime.min.time()).replace(tzinfo=tz),
            "fecha_ven_dt": datetime.combine(fecha_ven, datetime.min.time()).replace(tzinfo=tz),
        }
        generar_pdf(datos_pdf)
        supabase.table("folios_registrados").insert({
            "folio": folio_generado,
            "marca": marca.upper(), "linea": linea.upper(), "anio": anio,
            "numero_serie": numero_serie.upper(), "numero_motor": numero_motor.upper(),
            "color": color.upper(), "nombre": nombre.upper(),
            "fecha_expedicion": fecha_exp.isoformat(),
            "fecha_vencimiento": fecha_ven.isoformat(),
            "entidad": ENTIDAD, "estado": "ACTIVO", "estado_pago": "VALIDADO",
            "creado_por": request.session.get("username", "admin")
        }).execute()
        from urllib.parse import quote
        return RedirectResponse(url=f"/panel/folios?msg={quote(f'Permiso {folio_generado} generado ✅')}", status_code=303)
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

    msg_html = f'<div class="alert-sf alert-ok mb-3"><i class="fa-solid fa-circle-check me-2"></i>{msg}</div>' if msg else ""

    info_html = ""
    acciones_html = ""
    if resultado:
        info_html = f"""
        <div class="info-box">
          <strong>Folio:</strong> {resultado.get("folio","")}<br>
          <strong>Estado pago:</strong> {resultado.get("estado_pago","")}<br>
          <strong>Expedición:</strong> {str(resultado.get("fecha_expedicion",""))[:10]}<br>
          <strong>Vencimiento:</strong> {str(resultado.get("fecha_vencimiento",""))[:10]}<br>
          <strong>user_id:</strong> {resultado.get("user_id","") or "ninguno (folio oficial)"}<br>
          <strong>Ver en público:</strong> <a href="/consulta/{resultado.get('folio','')}" target="_blank" style="color:#8b1f3a">
            /consulta/{resultado.get('folio','')} →
          </a>
        </div>"""
        acciones_html = f"""
        <form method="POST" action="/panel/test_fechas">
          <input type="hidden" name="folio" value="{resultado.get('folio','')}">
          <button type="submit" name="accion" value="vencer_permiso" class="btn-vencer">
            ⏰ Marcar VENCIDO — probar botón Renovar
          </button>
          <button type="submit" name="accion" value="vencer_pago_48h" class="btn-simular">
            💀 Simular 48h sin pago — probar borrado auto
          </button>
          <button type="submit" name="accion" value="restaurar" class="btn-restaurar">
            ✅ Restaurar vigencia normal (30 días)
          </button>
        </form>"""

    contenido = f"""
    <div class="row-titulo mb-3">
      <h1 class="titulo-row" style="font-size:22px">🧪 Test Fechas</h1>
      <div class="borde-hr"><hr></div>
    </div>
    {msg_html}
    <div class="form-card" style="max-width:500px">
      <form method="GET">
        <label class="form-label">Folio a probar</label>
        <div class="d-flex gap-2 mb-3">
          <input type="text" name="folio" class="form-control" placeholder="Ej: 7801234" value="{folio_buscar}">
          <button type="submit" class="btn btn-primary px-4">Buscar</button>
        </div>
      </form>
      {info_html}
      {acciones_html}
    </div>"""

    return HTMLResponse(_base(
        titulo="Test Fechas",
        seccion="Test Fechas",
        breadcrumb_extra='» <span>Test Fechas</span>',
        contenido=contenido
    ))

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
            msg = f"Folio {folio} marcado VENCIDO. Pruébalo en /consulta/{folio}"
        elif accion == "vencer_pago_48h":
            nueva_exp = (datetime.now(tz) - timedelta(hours=49)).isoformat()
            supabase.table("folios_registrados") \
                .update({"fecha_expedicion": nueva_exp}).eq("folio", folio).execute()
            msg = f"Folio {folio}: expedición movida 49h atrás. Se borrará en máx 15 min si sigue PENDIENTE_PAGO."
        elif accion == "restaurar":
            hoy = datetime.now(tz)
            ven = hoy + timedelta(days=30)
            supabase.table("folios_registrados").update({
                "fecha_expedicion": hoy.date().isoformat(),
                "fecha_vencimiento": ven.date().isoformat()
            }).eq("folio", folio).execute()
            msg = f"Folio {folio} restaurado a vigencia normal."
    except Exception as e:
        msg = f"Error: {e}"

    from urllib.parse import quote
    return RedirectResponse(
        url=f"/panel/test_fechas?folio={folio}&msg={quote(msg)}", status_code=303)

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
