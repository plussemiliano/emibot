import os, json, logging, tempfile
from datetime import datetime
import pytz
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters
from google.oauth2 import service_account
from googleapiclient.discovery import build
import anthropic
import speech_recognition as sr
from pydub import AudioSegment

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY  = os.environ["ANTHROPIC_API_KEY"]
TIMEZONE       = "America/Argentina/Buenos_Aires"
GASTOS_FILE    = "gastos.json"
CALENDAR_ID    = os.environ.get("CALENDAR_ID", "plussemiliano@gmail.com")

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

def load_gastos():
    if os.path.exists(GASTOS_FILE):
        with open(GASTOS_FILE) as f:
            return json.load(f)
    return []

def save_gastos(gastos):
    with open(GASTOS_FILE, "w") as f:
        json.dump(gastos, f, ensure_ascii=False, indent=2)

def get_calendar_service():
    sa_info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT"])
    creds = service_account.Credentials.from_service_account_info(sa_info, scopes=["https://www.googleapis.com/auth/calendar"])
    return build("calendar", "v3", credentials=creds)

def create_event(title, start_dt, end_dt, description=""):
    service = get_calendar_service()
    event = {
        "summary": title,
        "description": description,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": TIMEZONE},
        "end":   {"dateTime": end_dt.isoformat(),   "timeZone": TIMEZONE},
        "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 60}]}
    }
    return service.events().insert(calendarId=CALENDAR_ID, body=event).execute().get("htmlLink")

def transcribe_audio(ogg_path):
    wav_path = ogg_path.replace(".ogg", ".wav")
    AudioSegment.from_ogg(ogg_path).export(wav_path, format="wav")
    r = sr.Recognizer()
    with sr.AudioFile(wav_path) as source:
        audio_data = r.record(source)
    os.unlink(wav_path)
    return r.recognize_google(audio_data, language="es-AR")

def interpret_message(text):
    now = datetime.now(pytz.timezone(TIMEZONE))
    prompt = "Hoy es " + now.strftime("%A %d de %B de %Y, %H:%M") + " (Buenos Aires).\nEl usuario dice: \"" + text + "\"\nResponde SOLO con JSON valido: {\"type\": \"evento|gasto|ambos|consulta|analisis_gastos\",\"eventos\": [{\"titulo\": \"...\",\"fecha_inicio\": \"YYYY-MM-DDTHH:MM:SS\",\"fecha_fin\": \"YYYY-MM-DDTHH:MM:SS\",\"descripcion\": \"...\"}],\"gastos\": [{\"descripcion\": \"...\",\"monto\": 0.0,\"moneda\": \"ARS\",\"fecha\": \"YYYY-MM-DD\",\"categoria\": \"comida|transporte|servicios|entretenimiento|trabajo|otro\"}],\"respuesta\": \"mensaje amigable\"}"
    response = anthropic_client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=1000, messages=[{"role": "user", "content": prompt}])
    raw = response.content[0].text.strip()
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())

async def handle_text(update, context):
    await process_message(update, context, update.message.text)

async def handle_voice(update, context):
    await update.message.reply_text("Transcribiendo audio...")
    try:
        voice_file = await update.message.voice.get_file()
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            await voice_file.download_to_drive(tmp.name)
            tmp_path = tmp.name
        try:
            text = transcribe_audio(tmp_path)
        finally:
            os.unlink(tmp_path)
        await update.message.reply_text("Entendi: " + text)
        await process_message(update, context, text)
    except Exception as e:
        await update.message.reply_text("Error: " + str(e), parse_mode=None)

async def process_message(update, context, text):
    try:
        data = interpret_message(text)
        gastos = load_gastos()
        respuestas = []
        for ev in data.get("eventos", []):
            try:
                start = datetime.fromisoformat(ev["fecha_inicio"])
                end = datetime.fromisoformat(ev["fecha_fin"])
                create_event(ev["titulo"], start, end, ev.get("descripcion", ""))
                respuestas.append("Agendado: " + ev["titulo"] + " el " + start.strftime("%d/%m a las %H:%M"))
            except Exception as e:
                respuestas.append("Error: " + str(e))
        for g in data.get("gastos", []):
            gastos.append(g)
            save_gastos(gastos)
            respuestas.append("Gasto: " + g["descripcion"] + " $" + str(g["monto"]))
        if data.get("type") == "analisis_gastos":
            respuestas.append(generar_analisis(gastos))
        msg = "\n\n".join(respuestas) if respuestas else data.get("respuesta", "Listo")
        await update.message.reply_text(msg, parse_mode=None)
    except Exception as e:
        await update.message.reply_text("Error: " + str(e), parse_mode=None)

def generar_analisis(gastos):
    if not gastos:
        return "No hay gastos."
    now = datetime.now(pytz.timezone(TIMEZONE))
    gastos_mes = [g for g in gastos if g.get("fecha","").startswith(now.strftime("%Y-%m"))]
    if not gastos_mes:
        return "No hay gastos este mes."
    response = anthropic_client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=800, messages=[{"role": "user", "content": "Analisis de gastos: " + json.dumps(gastos_mes)}])
    return response.content[0].text

async def start(update, context):
    await update.message.reply_text("Hola Emi! Manda eventos, gastos o audios.")

if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.run_polling()
