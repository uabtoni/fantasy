"""
Compara las dos últimas ejecuciones del scraper (guardadas en
price_history) y manda un mensaje por Telegram con los mayores
movimientos de precio.

Configuración necesaria (una sola vez):
1. Habla con @BotFather en Telegram, crea un bot con /newbot, y guarda
   el token que te da.
2. Habla con @userinfobot (o mira los logs de tu bot tras escribirle)
   para conseguir tu chat_id.
3. Define las variables de entorno:
     TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
   (además de SUPABASE_URL / SUPABASE_KEY, que ya usa el scraper)

Pensado para lanzarse justo después de main.py en la misma tarea
programada, así siempre compara "esta ejecución" con "la anterior".
"""

import os
import logging
import requests
from supabase import create_client, Client

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

UMBRAL_PORCENTAJE = 3.0   # solo avisa de movimientos >= 3% (sube o baja)
MAX_JUGADORES_POR_MENSAJE = 12

# Si quieres que solo te avise de jugadores concretos (tu equipo,
# favoritos...), pon aquí sus nombres exactos tal y como salen en la
# tabla `players`. Déjalo como lista vacía para vigilar TODO el mercado.
LISTA_SEGUIMIENTO = []  # ej: ["Lamine Yamal", "Kylian Mbappé"]


def get_supabase():
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_KEY", "")
    if not url or not key:
        raise RuntimeError("Faltan SUPABASE_URL / SUPABASE_KEY como variables de entorno.")
    return create_client(url, key)


def fetch_last_two_runs(supabase):
    """Devuelve (timestamp_actual, timestamp_anterior) de price_history."""
    resp = (supabase.table('price_history')
            .select('recorded_at')
            .order('recorded_at', desc=True)
            .limit(1000)
            .execute())
    timestamps = sorted({r['recorded_at'] for r in resp.data}, reverse=True)
    if len(timestamps) < 2:
        return None, None
    return timestamps[0], timestamps[1]


def fetch_prices_at(supabase, timestamp):
    resp = (supabase.table('price_history')
            .select('player_id, price')
            .eq('recorded_at', timestamp)
            .execute())
    return {r['player_id']: r['price'] for r in resp.data}


def fetch_player_names(supabase, player_ids):
    if not player_ids:
        return {}
    resp = (supabase.table('players')
            .select('id, name, team')
            .in_('id', list(player_ids))
            .execute())
    return {r['id']: f'{r["name"]} ({r["team"]})' for r in resp.data}


def enviar_telegram(mensaje):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        logging.warning("Faltan TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID. No se envía nada, solo se imprime:")
        print(mensaje)
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, data={
        "chat_id": chat_id,
        "text": mensaje,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })
    if resp.status_code != 200:
        logging.error(f"Error al enviar a Telegram: {resp.status_code} {resp.text}")
    else:
        logging.info("Alerta enviada por Telegram.")


def main():
    supabase = get_supabase()

    actual_ts, anterior_ts = fetch_last_two_runs(supabase)
    if not actual_ts or not anterior_ts:
        logging.info("Aún no hay dos ejecuciones guardadas para comparar. Nada que hacer todavía.")
        return

    precios_actuales = fetch_prices_at(supabase, actual_ts)
    precios_anteriores = fetch_prices_at(supabase, anterior_ts)

    nombres_filtro = {n.lower() for n in LISTA_SEGUIMIENTO}

    movimientos = []
    for player_id, precio_actual in precios_actuales.items():
        precio_anterior = precios_anteriores.get(player_id)
        if not precio_anterior:
            continue
        diff_pct = (precio_actual - precio_anterior) / precio_anterior * 100
        if abs(diff_pct) >= UMBRAL_PORCENTAJE:
            movimientos.append((player_id, precio_anterior, precio_actual, diff_pct))

    if not movimientos:
        logging.info("Sin movimientos relevantes en esta comparación.")
        return

    nombres = fetch_player_names(supabase, [m[0] for m in movimientos])

    # Si hay lista de seguimiento, filtramos por nombre
    if nombres_filtro:
        movimientos = [m for m in movimientos if nombres.get(m[0], "").lower().split(" (")[0] in nombres_filtro]
        if not movimientos:
            logging.info("Sin movimientos relevantes entre los jugadores de tu lista de seguimiento.")
            return

    movimientos.sort(key=lambda m: abs(m[3]), reverse=True)
    movimientos = movimientos[:MAX_JUGADORES_POR_MENSAJE]

    lineas = ["<b>📊 Movimientos de mercado</b>", ""]
    for player_id, antes, ahora, diff_pct in movimientos:
        nombre = nombres.get(player_id, player_id)
        icono = "🟢" if diff_pct > 0 else "🔴"
        signo = "+" if diff_pct > 0 else ""
        lineas.append(
            f"{icono} <b>{nombre}</b>: {antes:,.0f}€ → {ahora:,.0f}€ ({signo}{diff_pct:.1f}%)".replace(",", ".")
        )

    enviar_telegram("\n".join(lineas))


if __name__ == "__main__":
    main()
