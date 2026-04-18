import os
import re
import json
import base64
import asyncio
import datetime
import traceback

import httpx
import pandas as pd
import fitz
from dotenv import load_dotenv
from openai import AsyncOpenAI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

load_dotenv()
openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

CATEGORIAS = {
    "cat_1": "Almuerzo",
    "cat_2": "Comida",
    "cat_3": "Cena",
    "cat_4": "Hotel",
    "cat_5": "Transporte",
    "cat_6": "Gastos Varios"
}

MESES_ES = {
    1: "Ene", 2: "Feb", 3: "Mar", 4: "Abr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Ago", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dic"
}

# ─── Utilidades ───────────────────────────────────────────────────────────────

def find_duplicate(tickets: list, temp: dict) -> dict | None:
    fecha = temp.get('fecha', '')
    total = safe_float(temp.get('total'))
    proveedor = (temp.get('proveedor') or '').strip().lower()
    for t in tickets:
        mismo_dia = t.get('fecha', '') == fecha
        mismo_monto = abs(safe_float(t.get('total')) - total) < 0.01
        mismo_lugar = t.get('proveedor', '').strip().lower() == proveedor
        if mismo_dia and mismo_monto and mismo_lugar:
            return t
    return None

def safe_float(val, default=0.0):
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default

def fmt_es(d):
    return f"{d.day:02d}/{MESES_ES[d.month]}"

def get_week_label(date_str):
    try:
        d = datetime.datetime.strptime(date_str, "%d/%m/%Y").date()
        offset = d.isoweekday() % 7
        sunday = d - datetime.timedelta(days=offset)
        saturday = sunday + datetime.timedelta(days=6)
        return sunday, saturday
    except ValueError:
        return None, None

async def safe_edit_text(query, text, **kwargs):
    try:
        await query.edit_message_text(text, **kwargs)
    except Exception:
        pass

# ─── Tipo de cambio live ──────────────────────────────────────────────────────

async def get_tc(currency: str) -> float | None:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"https://open.er-api.com/v6/latest/{currency}")
            data = r.json()
            if data.get("result") != "success":
                return None
            return round(data["rates"]["MXN"], 4)
    except Exception:
        return None

# ─── OCR con reintentos ───────────────────────────────────────────────────────

async def extraer_datos_ticket(image_bytes: bytes) -> dict:
    base64_image = base64.b64encode(image_bytes).decode("utf-8")

    prompt = """Eres un asistente de Cuentas por Pagar. Extrae los datos del ticket con estas reglas:

- Si un dato no existe o no aplica, asigna 0. Si es ilegible, pon null.
- No inventes datos.

IMPUESTO (campo "iva"):
  Busca: IVA, Impuesto, Tax, Taxes, I.V.A., VAT, Imp.
  Si hay varios impuestos distintos al ISH, súmalos en "iva".

IMPUESTO HOTELERO (campo "ish"):
  Busca: ISH, Impuesto sobre Hospedaje, Hotel Tax, Lodging Tax, City Tax, Tourism Tax.
  Si no aparece, pon 0.

PROPINA (campo "propina"):
  Busca: Propina, Tip, Gratuity, Gratuidad, Servicio, Service Charge, Service Fee.
  Captura solo el monto cobrado, NO el total. Si es sugerida pero no cobrada, pon 0.

MONEDA: detecta el código ISO de la moneda. Principales: MXN, USD, EUR, GBP, BRL, CNY, JPY, CAD, AUD, CHF, COP, ARS, CLP, PEN, GTQ, HNL, CRC, DOP, BOB, PYG, UYU. Si no se ve claramente, asume MXN.

Responde ÚNICAMENTE con este JSON exacto:
{
  "fecha": "dd/mm/yyyy",
  "subtotal": 100.00,
  "iva": 16.00,
  "ish": 0,
  "propina": 0,
  "total": 116.00,
  "moneda": "MXN",
  "proveedor": "Nombre del Comercio"
}"""

    fallback = {"fecha": None, "subtotal": 0, "iva": 0, "ish": 0,
                "propina": 0, "total": 0, "moneda": None, "proveedor": None}

    for attempt in range(3):
        try:
            response = await openai_client.chat.completions.create(
                model="gpt-4o",
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                    ]
                }],
                temperature=0.0
            )
            content = response.choices[0].message.content
            match = re.search(r'\{.*\}', content, re.DOTALL)
            if not match:
                raise ValueError("No JSON en respuesta")
            return json.loads(match.group())
        except Exception:
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)
            else:
                print("Error OCR tras 3 intentos:", traceback.format_exc())
                return fallback
    return fallback

# ─── Comandos ─────────────────────────────────────────────────────────────────

async def iniciar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data['active'] = True
    context.user_data['tickets'] = []
    context.user_data['state'] = 'WAITING_RECEIPT'
    context.user_data['temp_ticket'] = {}
    await update.message.reply_text(
        "📝 *Sesión de Reporte Iniciada*\n\n"
        "Manda la foto del primer ticket o la factura en PDF.\n"
        "Usa /ayuda si tienes dudas.",
        parse_mode='Markdown'
    )

async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Sesión cancelada. Usa /iniciar para comenzar de nuevo."
    )

async def ayuda(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    texto = (
        "📖 *Guía de uso*\n\n"
        "1️⃣ /iniciar — Abre una nueva sesión\n"
        "2️⃣ Envía la *foto* o *PDF* de tu ticket\n"
        "3️⃣ Revisa los datos y corrige si es necesario\n"
        "4️⃣ Confirma y elige la *categoría*\n"
        "5️⃣ Si el ticket es en USD/EUR, indica el *tipo de cambio*\n"
        "6️⃣ Repite para cada ticket\n"
        "7️⃣ Genera el *Excel* agrupado por semana\n\n"
        "📋 *Comandos:*\n"
        "/iniciar — Nueva sesión\n"
        "/ver — Ver resumen de tickets\n"
        "/cancelar — Cancelar sesión actual\n"
        "/ayuda — Esta guía\n\n"
        "🏷️ *Categorías:* Almuerzo · Comida · Cena · Hotel · Transporte · Gastos Varios\n"
        "💱 *Monedas:* MXN, USD, EUR"
    )
    await update.message.reply_text(texto, parse_mode='Markdown')

# ─── Flujo principal ──────────────────────────────────────────────────────────

async def show_ticket_preview(chat_id, context: ContextTypes.DEFAULT_TYPE):
    t = context.user_data['temp_ticket']
    context.user_data['state'] = 'CONFIRMING_TICKET'

    def fmt(val):
        try:
            return f"${float(val):,.2f}"
        except (TypeError, ValueError):
            return "❌ No detectado"

    msg = (
        f"📋 *Revisa los datos extraídos:*\n\n"
        f"📅 Fecha: {t.get('fecha') or '❌ No detectada'}\n"
        f"🏪 Proveedor: {t.get('proveedor') or '❌ No detectado'}\n"
        f"💵 Subtotal: {fmt(t.get('subtotal', 0))}\n"
        f"🧾 IVA: {fmt(t.get('iva', 0))}\n"
        f"🏨 ISH: {fmt(t.get('ish', 0))}\n"
        f"🍽️ Propina: {fmt(t.get('propina', 0))}\n"
        f"💰 *Total: {fmt(t.get('total', 0))} {t.get('moneda', 'MXN')}*\n\n"
        f"Si algo está mal, toca el campo a corregir."
    )
    keyboard = [
        [InlineKeyboardButton("✅ Confirmar y elegir categoría", callback_data="confirm_ticket")],
        [InlineKeyboardButton("✏️ Fecha", callback_data="tedit_fecha"),
         InlineKeyboardButton("✏️ Proveedor", callback_data="tedit_proveedor")],
        [InlineKeyboardButton("✏️ Subtotal", callback_data="tedit_subtotal"),
         InlineKeyboardButton("✏️ IVA", callback_data="tedit_iva")],
        [InlineKeyboardButton("✏️ ISH", callback_data="tedit_ish"),
         InlineKeyboardButton("✏️ Propina", callback_data="tedit_propina")],
        [InlineKeyboardButton("✏️ Total", callback_data="tedit_total"),
         InlineKeyboardButton("✏️ Moneda", callback_data="tedit_moneda")],
    ]
    await context.bot.send_message(
        chat_id=chat_id, text=msg,
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown'
    )

async def ask_for_category(chat_id, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['state'] = 'WAITING_CATEGORY'
    keyboard = [
        [InlineKeyboardButton("Almuerzo", callback_data="cat_1"),
         InlineKeyboardButton("Comida", callback_data="cat_2")],
        [InlineKeyboardButton("Cena", callback_data="cat_3"),
         InlineKeyboardButton("Hotel", callback_data="cat_4")],
        [InlineKeyboardButton("Transporte", callback_data="cat_5"),
         InlineKeyboardButton("G.Varios", callback_data="cat_6")]
    ]
    await context.bot.send_message(
        chat_id=chat_id, text="🏷️ Selecciona la Categoría:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def ask_for_tc(chat_id, context: ContextTypes.DEFAULT_TYPE):
    t = context.user_data['temp_ticket']
    context.user_data['state'] = 'WAITING_TC'

    tc_live = await get_tc(t['moneda'])
    if tc_live:
        base = round(tc_live * 2) / 2
        opciones = sorted({round(base - 0.5, 2), round(base, 2),
                           round(base + 0.5, 2), round(base + 1.0, 2)})
        hint = f" _(Referencia live: ${tc_live:,.2f})_"
    else:
        opciones = [17.50, 18.00, 18.50, 19.00]
        hint = ""

    keyboard = [
        [InlineKeyboardButton(f"${o:,.2f}", callback_data=f"tc_{o:.2f}") for o in opciones[:2]],
        [InlineKeyboardButton(f"${o:,.2f}", callback_data=f"tc_{o:.2f}") for o in opciones[2:]],
        [InlineKeyboardButton("✍️ Escribir tipo de cambio", callback_data="tc_manual")]
    ]
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"💱 Detecté *{t['moneda']}*. ¿Qué Tipo de Cambio aplicamos?{hint}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

async def finalize_ticket(chat_id, context: ContextTypes.DEFAULT_TYPE):
    t = context.user_data['temp_ticket']
    tc = float(t.get('tc', 1.0))
    t['monto_mxn'] = safe_float(t['total']) * tc
    t['subtotal_mxn'] = safe_float(t['subtotal']) * tc
    t['iva_mxn'] = safe_float(t['iva']) * tc
    t['ish_mxn'] = safe_float(t.get('ish', 0)) * tc
    t['propina_mxn'] = safe_float(t['propina']) * tc

    sun, sat = get_week_label(t['fecha'])
    t['week_key'] = f"{sun.strftime('%Y%m%d')}_{sat.strftime('%Y%m%d')}" if sun else "Sin_Fecha"
    t['week_label'] = f"{fmt_es(sun)} al {fmt_es(sat)}" if sun else "Desconocida"

    context.user_data['tickets'].append(t)
    context.user_data['temp_ticket'] = {}
    context.user_data['state'] = 'TICKET_DONE'

    keyboard = [
        [InlineKeyboardButton("📸 Agregar otro ticket", callback_data="add_another")],
        [InlineKeyboardButton("✅ Ver Resumen y Finalizar", callback_data="ver_resumen")]
    ]
    msg = (
        f"✅ *Ticket #{t['id']} guardado*\n"
        f"📅 {t['fecha']} | 🏪 {t['proveedor']}\n"
        f"🏷️ {t['categoria']} | 💰 ${t['monto_mxn']:,.2f} MXN\n\n"
        f"¿Tienes más tickets? Manda la siguiente foto o PDF, o finaliza tu reporte."
    )
    await context.bot.send_message(
        chat_id=chat_id, text=msg,
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown'
    )

# ─── Media handler ────────────────────────────────────────────────────────────

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('active'):
        await update.message.reply_text("Usa /iniciar para comenzar tu reporte.")
        return

    state = context.user_data.get('state')
    if state not in ['WAITING_RECEIPT', 'TICKET_DONE']:
        await update.message.reply_text(
            "⚠️ Hay un ticket en proceso. Termínalo o usa /cancelar para reiniciar."
        )
        return

    image_bytes = None

    if update.message.photo:
        photo_file = await update.message.photo[-1].get_file()
        image_bytes = bytes(await photo_file.download_as_bytearray())

    elif update.message.document and update.message.document.file_name.lower().endswith('.pdf'):
        doc_file = await update.message.document.get_file()
        b_array = await doc_file.download_as_bytearray()
        try:
            doc = fitz.open(stream=bytes(b_array), filetype="pdf")
            pix = doc.load_page(0).get_pixmap(dpi=150)
            image_bytes = pix.tobytes("jpeg")
        except Exception:
            await update.message.reply_text("Error abriendo el PDF. ¿Puedes mandarlo como imagen?")
            return
    else:
        return

    msg_wait = await update.message.reply_text("⏳ Procesando con IA Visual...")
    result = await extraer_datos_ticket(image_bytes)
    await msg_wait.delete()

    if result.get("fecha") is None and result.get("total") in [0, None] and result.get("proveedor") is None:
        keyboard = [
            [InlineKeyboardButton("📸 Intentar con otra foto", callback_data="reset_receipt")],
            [InlineKeyboardButton("⌨️ Capturar Manualmente", callback_data="manual_receipt")]
        ]
        context.user_data['state'] = 'WAITING_RECEIPT'
        await update.message.reply_text("❌ No pude leer este ticket.", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    t_id = len(context.user_data.get('tickets', [])) + 1
    context.user_data['temp_ticket'] = {
        "id": t_id,
        "fecha": result.get("fecha"),
        "proveedor": result.get("proveedor"),
        "subtotal": safe_float(result.get("subtotal")),
        "iva": safe_float(result.get("iva")),
        "ish": safe_float(result.get("ish")),
        "propina": safe_float(result.get("propina")),
        "total": safe_float(result.get("total")),
        "moneda": str(result.get("moneda") or "MXN").upper(),
        "tc": 1.0,
        "monto_mxn": 0
    }
    await show_ticket_preview(update.effective_chat.id, context)

# ─── Resumen y finalización ───────────────────────────────────────────────────

async def view_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message or update.callback_query.message
    if not context.user_data.get('active'):
        await message.reply_text("Usa /iniciar para comenzar tu reporte.")
        return

    tickets = context.user_data.get('tickets', [])
    if not tickets:
        await message.reply_text("Lista vacía. Manda tu primer ticket.")
        return

    lines = []
    total = 0.0
    for t in tickets:
        lines.append(f"#{t['id']} | {t['fecha']} | {t['proveedor']} | {t['categoria']} | ${t['monto_mxn']:,.2f} MXN")
        total += t['monto_mxn']
    lines.append(f"\n💰 *Total Acumulado: ${total:,.2f} MXN*")

    keyboard = [
        [InlineKeyboardButton("📊 Generar Excel y Finalizar", callback_data="fin_reporte")],
        [InlineKeyboardButton("✏️ Editar un Ticket", callback_data="edit_start"),
         InlineKeyboardButton("🗑️ Eliminar Ticket", callback_data="delete_start")]
    ]

    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                "\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown'
            )
        except Exception:
            await message.reply_text(
                "\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown'
            )
    else:
        await message.reply_text(
            "\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown'
        )

async def finalizar_reportes(context: ContextTypes.DEFAULT_TYPE, chat_id):
    tickets = context.user_data.get('tickets', [])
    if not tickets:
        await context.bot.send_message(chat_id=chat_id, text="No hay tickets que procesar.")
        return

    por_semana = {}
    for t in tickets:
        wk = t['week_key']
        if wk not in por_semana:
            por_semana[wk] = {'label': t['week_label'], 'tickets': []}
        por_semana[wk]['tickets'].append(t)

    await context.bot.send_message(chat_id=chat_id, text="⏳ Generando reportes...")

    for wk, content in por_semana.items():
        label = content['label']
        df_data = []
        for t in content['tickets']:
            df_data.append({
                "#": t['id'],
                "Fecha": t['fecha'],
                "Proveedor": t['proveedor'],
                "Categoría": t['categoria'],
                "Subtotal Original": t['subtotal'],
                "IVA Original": t['iva'],
                "ISH Original": t.get('ish', 0),
                "Propina Original": t['propina'],
                "Total Original": t['total'],
                "Moneda": t['moneda'],
                "T/C": t['tc'],
                "Total MXN": t['monto_mxn']
            })

        df = pd.DataFrame(df_data)
        filename = f"Gastos_{label.replace(' ', '_').replace('/', '-')}.xlsx"
        df.to_excel(filename, index=False)

        with open(filename, 'rb') as f:
            await context.bot.send_document(
                chat_id=chat_id, document=f, caption=f"📊 Semana: {label}"
            )
        os.remove(filename)

    context.user_data.clear()
    await context.bot.send_message(chat_id=chat_id, text="✅ ¡Listo! Sesión finalizada.")

# ─── Callback handler ─────────────────────────────────────────────────────────

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = update.effective_chat.id

    # ── Flujo de ticket nuevo ──────────────────────────────────────────────────

    if data == "add_another":
        context.user_data['state'] = 'WAITING_RECEIPT'
        await safe_edit_text(query, "📸 Listo, manda la foto o PDF del siguiente ticket.")
        return

    if data == "reset_receipt":
        context.user_data['state'] = 'WAITING_RECEIPT'
        await context.bot.send_message(chat_id=chat_id, text="📸 Manda la nueva foto.")
        return

    if data == "manual_receipt":
        context.user_data['state'] = 'WAITING_MANUAL_CAPTURE'
        await context.bot.send_message(
            chat_id=chat_id,
            text="⌨️ Formato:\n`fecha, proveedor, subtotal, iva, propina, total`\n\nEj:\n`20/04/2025, Uber, 100, 16, 0, 116`",
            parse_mode='Markdown'
        )
        return

    if data == "confirm_ticket":
        t = context.user_data.get('temp_ticket', {})
        if not t.get('fecha'):
            await context.bot.send_message(chat_id=chat_id,
                text="⚠️ Falta la *fecha*. Toca ✏️ Fecha para corregirla.", parse_mode='Markdown')
            return
        if not t.get('proveedor'):
            await context.bot.send_message(chat_id=chat_id,
                text="⚠️ Falta el *proveedor*. Toca ✏️ Proveedor para corregirlo.", parse_mode='Markdown')
            return
        if safe_float(t.get('total')) == 0:
            await context.bot.send_message(chat_id=chat_id,
                text="⚠️ El *total es 0*. Toca ✏️ Total para corregirlo.", parse_mode='Markdown')
            return
        dup = find_duplicate(context.user_data.get('tickets', []), t)
        if dup:
            kb = [
                [InlineKeyboardButton("🗑️ Sí es duplicado, descartar", callback_data="discard_ticket")],
                [InlineKeyboardButton("✅ No es duplicado, continuar", callback_data="force_confirm_ticket")]
            ]
            await safe_edit_text(
                query,
                f"⚠️ *Posible ticket duplicado*\n\n"
                f"Ya existe uno similar:\n"
                f"#{dup['id']} | {dup['fecha']} | {dup['proveedor']} | ${dup['monto_mxn']:,.2f} MXN\n\n"
                f"¿Qué deseas hacer?",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode='Markdown'
            )
            return
        await safe_edit_text(query, "✅ Datos confirmados. Selecciona la categoría...")
        await ask_for_category(chat_id, context)
        return

    if data == "discard_ticket":
        context.user_data['temp_ticket'] = {}
        context.user_data['state'] = 'TICKET_DONE'
        await safe_edit_text(query, "🗑️ Ticket descartado. Manda el siguiente o usa Ver Resumen.")
        return

    if data == "force_confirm_ticket":
        await safe_edit_text(query, "✅ Datos confirmados. Selecciona la categoría...")
        await ask_for_category(chat_id, context)
        return

    if data.startswith("tedit_"):
        field = data[6:]
        context.user_data['temp_edit_field'] = field
        context.user_data['state'] = 'WAITING_TEMP_EDIT'
        labels = {
            'fecha': 'Fecha (dd/mm/yyyy)', 'proveedor': 'Proveedor',
            'subtotal': 'Subtotal', 'iva': 'IVA', 'ish': 'ISH (Impuesto Hotelero)',
            'propina': 'Propina', 'total': 'Total', 'moneda': 'Moneda (MXN, USD, EUR)',
        }
        await safe_edit_text(
            query, f"✏️ Escribe el nuevo valor para *{labels.get(field, field)}*:",
            parse_mode='Markdown'
        )
        return

    # ── Categoría ──────────────────────────────────────────────────────────────

    if data.startswith("cat_"):
        if context.user_data.get('state') == 'WAITING_CATEGORY':
            context.user_data['temp_ticket']['categoria'] = CATEGORIAS[data]
            await safe_edit_text(query, f"🏷️ Categoría: {CATEGORIAS[data]} ✅")
            if context.user_data['temp_ticket'].get('moneda', 'MXN') != 'MXN':
                await ask_for_tc(chat_id, context)
            else:
                await finalize_ticket(chat_id, context)

        elif (context.user_data.get('state') == 'WAITING_EDIT_VALUE'
              and context.user_data.get('edit_field') == 'categoria'):
            t_id = context.user_data['edit_id']
            ticket = next((x for x in context.user_data['tickets'] if x['id'] == t_id), None)
            if ticket:
                ticket['categoria'] = CATEGORIAS[data]
            context.user_data['state'] = 'TICKET_DONE'
            await safe_edit_text(query, f"🏷️ Categoría actualizada: {CATEGORIAS[data]} ✅")
            await view_menu(update, context)
        return

    # ── Tipo de cambio ─────────────────────────────────────────────────────────

    if data.startswith("tc_"):
        if data == "tc_manual":
            context.user_data['state'] = 'WAITING_TC_MANUAL'
            await safe_edit_text(query, "✍️ Escribe el tipo de cambio (ej: 18.50):")
            return
        try:
            tc_val = float(data.split("_")[1])
        except (IndexError, ValueError):
            return
        context.user_data['temp_ticket']['tc'] = tc_val
        await safe_edit_text(query, f"💱 T/C aplicado: ${tc_val:,.2f} ✅")
        await finalize_ticket(chat_id, context)
        return

    # ── Resumen ────────────────────────────────────────────────────────────────

    if data == "ver_resumen":
        await view_menu(update, context)
        return

    if data == "fin_reporte":
        tickets = context.user_data.get('tickets', [])
        total = sum(t['monto_mxn'] for t in tickets)
        kb = [
            [InlineKeyboardButton("✅ Sí, generar reporte", callback_data="confirm_fin_reporte")],
            [InlineKeyboardButton("↩️ Regresar", callback_data="ver_resumen")]
        ]
        await safe_edit_text(
            query,
            f"¿Confirmas generar el reporte?\n\n"
            f"📋 *{len(tickets)} tickets* | 💰 Total: *${total:,.2f} MXN*",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode='Markdown'
        )
        return

    if data == "confirm_fin_reporte":
        await finalizar_reportes(context, chat_id)
        return

    # ── Editar ticket guardado ─────────────────────────────────────────────────

    if data == "edit_start":
        tickets = context.user_data.get('tickets', [])
        if not tickets:
            return
        kb = [[InlineKeyboardButton(
            f"#{t['id']} {t['proveedor']} — ${t['monto_mxn']:,.2f}",
            callback_data=f"edit_select_{t['id']}"
        )] for t in tickets]
        await safe_edit_text(query, "¿Qué ticket deseas editar?", reply_markup=InlineKeyboardMarkup(kb))
        return

    if data.startswith("edit_select_"):
        t_id = int(data.split("_")[2])
        context.user_data['edit_id'] = t_id
        kb = [
            [InlineKeyboardButton("Fecha", callback_data="edit_field_fecha"),
             InlineKeyboardButton("Proveedor", callback_data="edit_field_proveedor")],
            [InlineKeyboardButton("Total", callback_data="edit_field_total"),
             InlineKeyboardButton("Categoría", callback_data="edit_field_categoria")]
        ]
        await safe_edit_text(
            query, f"✏️ Editando Ticket #{t_id}. ¿Qué corregimos?",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    if data.startswith("edit_field_"):
        field = data[len("edit_field_"):]
        context.user_data['edit_field'] = field
        context.user_data['state'] = 'WAITING_EDIT_VALUE'
        if field == "categoria":
            kb = [
                [InlineKeyboardButton("Almuerzo", callback_data="cat_1"),
                 InlineKeyboardButton("Comida", callback_data="cat_2")],
                [InlineKeyboardButton("Cena", callback_data="cat_3"),
                 InlineKeyboardButton("Hotel", callback_data="cat_4")],
                [InlineKeyboardButton("Transporte", callback_data="cat_5"),
                 InlineKeyboardButton("G.Varios", callback_data="cat_6")]
            ]
            await safe_edit_text(query, "Selecciona nueva categoría:", reply_markup=InlineKeyboardMarkup(kb))
        else:
            labels = {"fecha": "Fecha (dd/mm/yyyy)", "proveedor": "Proveedor", "total": "Total"}
            await safe_edit_text(query, f"✏️ Escribe el nuevo valor para *{labels.get(field, field)}*:", parse_mode='Markdown')
        return

    # ── Eliminar ticket guardado ───────────────────────────────────────────────

    if data == "delete_start":
        tickets = context.user_data.get('tickets', [])
        if not tickets:
            return
        kb = [[InlineKeyboardButton(
            f"🗑️ #{t['id']} {t['proveedor']} — ${t['monto_mxn']:,.2f}",
            callback_data=f"delete_confirm_{t['id']}"
        )] for t in tickets]
        kb.append([InlineKeyboardButton("↩️ Cancelar", callback_data="ver_resumen")])
        await safe_edit_text(query, "¿Qué ticket deseas eliminar?", reply_markup=InlineKeyboardMarkup(kb))
        return

    if data.startswith("delete_confirm_"):
        t_id = int(data.split("_")[2])
        context.user_data['tickets'] = [x for x in context.user_data['tickets'] if x['id'] != t_id]
        for i, t in enumerate(context.user_data['tickets'], 1):
            t['id'] = i
        context.user_data['state'] = 'TICKET_DONE'
        await safe_edit_text(query, f"🗑️ Ticket #{t_id} eliminado.")
        await view_menu(update, context)
        return

# ─── Text handler ─────────────────────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('active'):
        await update.message.reply_text("Usa /iniciar para comenzar.")
        return

    state = context.user_data.get('state')
    text = update.message.text.strip()
    chat_id = update.effective_chat.id

    if state == 'WAITING_TC_MANUAL':
        try:
            tc = float(text.replace(',', '.'))
        except ValueError:
            await update.message.reply_text("Ingresa un número válido (ej: 18.50)")
            return
        context.user_data['temp_ticket']['tc'] = tc
        await update.message.reply_text(f"💱 T/C guardado: ${tc:,.2f}")
        await finalize_ticket(chat_id, context)

    elif state == 'WAITING_MANUAL_CAPTURE':
        parts = text.split(',')
        if len(parts) < 6:
            await update.message.reply_text("Faltan campos. Separa por comas: fecha, proveedor, subtotal, iva, propina, total")
            return
        try:
            t_id = len(context.user_data.get('tickets', [])) + 1
            context.user_data['temp_ticket'] = {
                "id": t_id,
                "fecha": parts[0].strip(),
                "proveedor": parts[1].strip(),
                "subtotal": float(parts[2].strip()),
                "iva": float(parts[3].strip()),
                "ish": 0,
                "propina": float(parts[4].strip()),
                "total": float(parts[5].strip()),
                "moneda": "MXN",
                "tc": 1.0,
                "monto_mxn": 0
            }
            await ask_for_category(chat_id, context)
        except ValueError:
            await update.message.reply_text("Formato inválido. Los montos deben ser números.")

    elif state == 'WAITING_EDIT_VALUE':
        field = context.user_data.get('edit_field')
        t_id = context.user_data.get('edit_id')
        ticket = next((x for x in context.user_data['tickets'] if x['id'] == t_id), None)
        if not ticket:
            await update.message.reply_text("Error: ticket no encontrado.")
            return
        if field == "fecha":
            ticket['fecha'] = text
        elif field == "proveedor":
            ticket['proveedor'] = text
        elif field == "total":
            try:
                ticket['total'] = float(text.replace(',', '.'))
                ticket['monto_mxn'] = ticket['total'] * ticket.get('tc', 1.0)
            except ValueError:
                await update.message.reply_text("Número inválido.")
                return
        context.user_data['state'] = 'TICKET_DONE'
        await update.message.reply_text("✅ Valor actualizado.")
        await view_menu(update, context)

    elif state == 'WAITING_TEMP_EDIT':
        field = context.user_data.get('temp_edit_field')
        numeric_fields = ['subtotal', 'iva', 'ish', 'propina', 'total']
        if field in numeric_fields:
            try:
                context.user_data['temp_ticket'][field] = float(text.replace(',', '.'))
            except ValueError:
                await update.message.reply_text("Ingresa un número válido (ej: 116.00)")
                return
        elif field == 'moneda':
            context.user_data['temp_ticket'][field] = text.upper()
        else:
            context.user_data['temp_ticket'][field] = text
        await show_ticket_preview(chat_id, context)

    else:
        await update.message.reply_text("Adjunta la imagen/PDF de tu gasto, o usa los botones.")

# ─── Error handler ────────────────────────────────────────────────────────────

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    print(f"[ERROR] {context.error}")
    traceback.print_exception(type(context.error), context.error, context.error.__traceback__)
    if isinstance(update, Update) and update.effective_chat:
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ Algo salió mal. Si el problema continúa, usa /cancelar e /iniciar de nuevo."
            )
        except Exception:
            pass

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        print("Falta TELEGRAM_TOKEN en .env")
        return

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", iniciar))
    app.add_handler(CommandHandler("iniciar", iniciar))
    app.add_handler(CommandHandler("cancelar", cancelar))
    app.add_handler(CommandHandler("ayuda", ayuda))
    app.add_handler(CommandHandler("ver", view_menu))

    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.PDF, handle_media))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_error_handler(error_handler)

    print("Bot iniciado.")
    app.run_polling()

if __name__ == '__main__':
    main()
