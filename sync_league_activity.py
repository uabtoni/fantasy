"""
Sincroniza el HISTORIAL de actividad de tu liga privada (fichajes, ventas,
clausulas, blindajes, ganancias por jornada...) con Supabase, y a partir de
ese historial calcula el dinero ESTIMADO de cada manager.

Usa league_data.py: get_league_activity / walk_all_activity (el historial
en si) y compute_manager_finances (la estimacion de dinero).

AVISO IMPORTANTE -- estimated_money NO es un dato oficial:
La API solo expone el saldo real (GET /teams/{id}/money) para TU PROPIO
equipo -- para el resto de managers da 403 Forbidden, confirmado probando
contra los 9 managers de una liga real (ver conversacion / test_league_history.py).
Este script reconstruye un saldo aproximado asumiendo el MISMO presupuesto
inicial para todos (--starting-budget, por defecto 100.000.000).

Un estimated_money negativo NO es, por si solo, un error: LaLiga Fantasy
permite endeudarte pujando en el mercado (fichajes a la IA) hasta el 20%
del valor de tu plantilla (teamValue) -- lo que no se puede es pagar a
OTRO MANAGER (oferta directa, cláusula) sin tener el dinero. Por eso
is_plausible compara contra ese limite de deuda, no contra cero (ver
DEBT_LIMIT_RATIO en league_data.py). Verificado con datos reales: para el
propio manager autenticado la reconstruccion cuadra casi exacto (calibrado
contra su saldo real); el unico manager de la liga de prueba que dio
negativo estaba, tras aplicar el limite de deuda correcto, DENTRO de lo
permitido -- no era un fallo del modelo.

Destino: tablas public.league_activity y public.league_manager_finances en
Supabase. Ejecuta la migracion
database/migrations/004_add_league_activity_and_finances.sql en el SQL
Editor de Supabase UNA VEZ antes de correr este script por primera vez.

Uso:
    python sync_league_activity.py                              # autodetecta tu liga
    python sync_league_activity.py --league-id 018070031
    python sync_league_activity.py --starting-budget 100000000  # por defecto
    python sync_league_activity.py --full                       # ignora lo ya sincronizado y re-descarga todo (hasta --max-pages)

Ademas, si hay eventos NUEVOS desde la ultima pasada, manda un aviso por
Telegram con TODOS ellos (fichajes, ventas, cláusulas, blindajes, ganancias
por jornada, altas de manager...) -- no filtra por importe ni tipo, a
diferencia de telegram_alert.py (que solo avisa de +-3% de precio en el
mercado publico y esta desactivado en el workflow por peticion del usuario,
ver .github/workflows/scraper.yml). Si no hay eventos nuevos, no manda nada
(no hace spam en cada pasada de 4h).

Variables de entorno necesarias (o en scraper/.env -- ver .env.example):
    SUPABASE_URL
    SUPABASE_SERVICE_ROLE_KEY
    TELEGRAM_BOT_TOKEN     (opcional -- sin esto, los avisos solo se imprimen por consola)
    TELEGRAM_CHAT_ID       (opcional)
"""

import argparse
import html
import logging
import os
import sys

import requests
from dotenv import load_dotenv
from supabase import create_client, Client

from laliga_auth import get_valid_access_token
from league_data import get_my_league_id, get_standing, get_league_activity, get_all_players, compute_manager_finances, ACTIVITY_TYPE_LABELS

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

UPSERT_BLOQUE = 200
MAX_PAGES_DEFAULT = 200  # tope de seguridad para no entrar en bucle infinito si la API cambia de forma
MAX_MOVIMIENTOS_POR_MENSAJE = 25  # limite de Telegram es ~4096 caracteres por mensaje
DASHBOARD_URL = "https://uabtoni.github.io/fantasy-dashboard/"

# Emoji por activityTypeId, para que el mensaje se lea de un vistazo.
ACTIVITY_EMOJI = {1: "🤝", 4: "🛡️", 6: "💶", 7: "🟥", 9: "🆕", 31: "🛒", 32: "🔒", 33: "💰"}


def _fmt_money(n):
    return f"{n:,.0f}€".replace(",", ".") if n else "€0"


def format_movimiento(item, managers_by_id, players_by_id):
    """Una linea legible (HTML de Telegram) para un item del historial."""
    t = item.get("activityTypeId")
    u1 = html.escape(managers_by_id.get(str(item.get("user1Id")), f"manager {item.get('user1Id')}"))
    u2_id = item.get("user2Id")
    u2 = html.escape(managers_by_id.get(str(u2_id), "")) if u2_id else None
    pid = str(item.get("playerMasterId") or item.get("playerId") or "")
    jugador = players_by_id.get(pid) if pid else None
    jugador = html.escape(jugador) if jugador else None
    amount = item.get("amount")
    emoji = ACTIVITY_EMOJI.get(t, "🔁")

    if t == 6:
        return f"{emoji} <b>{u1}</b> ganó {_fmt_money(amount)} en la jornada {item.get('weekNumber', '?')}"
    if t == 7:
        return f"{emoji} <b>{u1}</b> no puntuó por alineación incorrecta (jornada {item.get('weekNumber', '?')})"
    if t == 9:
        return f"{emoji} <b>{u1}</b> se unió a la liga"

    verbo = ACTIVITY_TYPE_LABELS.get(t, f"hizo un movimiento sin catalogar (tipo {t})")
    jugador_txt = f" a <b>{jugador}</b>" if jugador else ""
    precio_txt = f" por {_fmt_money(amount)}" if amount else ""
    # user2 solo aparece en operaciones directas (tipo 1/32) -- es la otra
    # parte de la transaccion (vendedor si u1 compra, comprador si u1 vende).
    contraparte_txt = f" (con {u2})" if u2 else ""
    return f"{emoji} <b>{u1}</b> {verbo}{jugador_txt}{precio_txt}{contraparte_txt}"


def enviar_telegram(mensaje):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        logging.warning("Faltan TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID. No se envía nada, solo se imprime:")
        try:
            print(mensaje)
        except UnicodeEncodeError:
            # Consola local en Windows (cp1252) que no sabe pintar emojis --
            # no afecta al envio real por Telegram (siempre UTF-8 por HTTP).
            print(mensaje.encode("ascii", "replace").decode("ascii"))
        return
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": mensaje, "parse_mode": "HTML", "disable_web_page_preview": True},
    )
    if resp.status_code != 200:
        logging.error(f"Error al enviar a Telegram: {resp.status_code} {resp.text}")
    else:
        logging.info("Aviso de movimientos enviado por Telegram.")


def avisar_movimientos_nuevos(nuevos, standing, all_players):
    """Manda UN mensaje de Telegram con todos los eventos nuevos de esta
    pasada (mas antiguo primero, para que se lea como una linea de tiempo).
    No manda nada si `nuevos` esta vacio."""
    if not nuevos:
        return

    managers_by_id = {}
    for entry in standing:
        team = entry.get("team", {}) or {}
        manager = team.get("manager") or {}
        if manager.get("id"):
            managers_by_id[str(manager["id"])] = manager.get("managerName", "?")

    players_by_id = {}
    for p in all_players:
        pm = p.get("playerMaster") if isinstance(p.get("playerMaster"), dict) else p
        pid = pm.get("id")
        if pid:
            players_by_id[str(pid)] = pm.get("nickname") or pm.get("name")

    ordenados = sorted(nuevos, key=lambda it: it.get("createdAt") or it.get("timestamp") or "")

    lineas = [f"📋 <b>{len(ordenados)} movimiento{'s' if len(ordenados) != 1 else ''} nuevo{'s' if len(ordenados) != 1 else ''} en tu liga</b>", ""]
    visibles = ordenados[:MAX_MOVIMIENTOS_POR_MENSAJE]
    for it in visibles:
        lineas.append(format_movimiento(it, managers_by_id, players_by_id))

    restantes = len(ordenados) - len(visibles)
    if restantes > 0:
        lineas.append("")
        lineas.append(f"<i>… y {restantes} movimiento{'s' if restantes != 1 else ''} más.</i>")

    lineas.append("")
    lineas.append(f'🔗 <a href="{DASHBOARD_URL}">Ver detalle en el dashboard</a>')

    enviar_telegram("\n".join(lineas))


def to_activity_row(league_id, item):
    return {
        "id": str(item.get("id")),
        "league_id": str(league_id),
        "activity_type_id": item.get("activityTypeId"),
        "amount": item.get("amount"),
        "user1_id": str(item["user1Id"]) if item.get("user1Id") is not None else None,
        "user2_id": str(item["user2Id"]) if item.get("user2Id") is not None else None,
        "player_master_id": str(item["playerMasterId"]) if item.get("playerMasterId") is not None else None,
        "week_number": item.get("weekNumber"),
        "created_at": item.get("createdAt") or item.get("timestamp"),
        "raw": item,
    }


def fetch_new_activity(token, league_id, known_ids, max_pages, full=False):
    """
    Recorre paginas de get_league_activity() (mas reciente primero) y
    devuelve los items NUEVOS (id no visto todavia). Se detiene en la
    primera pagina vacia, o -- si no es --full -- en cuanto una pagina
    entera ya es conocida (asume que el feed no tiene huecos: si ya vimos
    esos ids, hemos alcanzado lo sincronizado en la ultima pasada).
    """
    nuevos = []
    for index in range(max_pages):
        page = get_league_activity(token, league_id, index)
        if not page:
            break
        pagina_ids = {str(it.get("id")) for it in page}
        nuevos.extend(it for it in page if str(it.get("id")) not in known_ids)
        if not full and pagina_ids and pagina_ids.issubset(known_ids):
            break
    return nuevos


def sync(league_id=None, starting_budget=100_000_000, max_pages=MAX_PAGES_DEFAULT, full=False):
    load_dotenv()
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY")
    if not supabase_url or not supabase_key:
        logging.error(
            "Faltan SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY "
            "(defínelas como variables de entorno o crea scraper/.env a partir de .env.example)."
        )
        sys.exit(1)
    supabase: Client = create_client(supabase_url, supabase_key)

    token = get_valid_access_token()
    real_league_id, league_name = get_my_league_id(token, forced_id=league_id)
    logging.info(f"Sincronizando historial de la liga {real_league_id} ({league_name})...")

    standing = get_standing(token, real_league_id)  # se reusa para el aviso de Telegram y para las finanzas

    existing = supabase.table("league_activity").select("id").eq("league_id", str(real_league_id)).execute()
    known_ids = {row["id"] for row in existing.data} if not full else set()
    logging.info(f"{len(known_ids)} eventos ya sincronizados previamente." if not full else "Modo --full: re-descargando todo el historial disponible.")

    nuevos = fetch_new_activity(token, real_league_id, known_ids, max_pages, full=full)
    logging.info(f"{len(nuevos)} eventos nuevos descargados.")

    if nuevos:
        rows = [to_activity_row(real_league_id, it) for it in nuevos]
        for i in range(0, len(rows), UPSERT_BLOQUE):
            supabase.table("league_activity").upsert(rows[i:i + UPSERT_BLOQUE]).execute()

        # Solo se pide el catalogo completo de jugadores (~800, para resolver
        # nombres aunque ya no esten en ninguna plantilla actual) cuando hay
        # algo nuevo que avisar -- se ahorra la llamada en las pasadas donde
        # no ha pasado nada (la mayoria).
        all_players = get_all_players(token)
        avisar_movimientos_nuevos(nuevos, standing, all_players)

    # Recalcula las finanzas con TODO el historial acumulado en Supabase (no
    # solo lo descargado en esta pasada), para que el estimado no dependa de
    # cuantas paginas se hayan traido hoy.
    todo = supabase.table("league_activity").select("raw").eq("league_id", str(real_league_id)).execute()
    activity_completa = [row["raw"] for row in todo.data]
    logging.info(f"Recalculando finanzas con {len(activity_completa)} eventos en total (saldo inicial asumido: €{starting_budget:,})...")

    finanzas = compute_manager_finances(activity_completa, standing, starting_budget=starting_budget)

    no_plausibles = [f for f in finanzas if not f["is_plausible"]]
    if no_plausibles:
        nombres = ", ".join(f"{f['manager_name']} ({f['estimated_money']:,} < limite {f['debt_limit']:,.0f})" for f in no_plausibles)
        logging.warning(
            f"{len(no_plausibles)} manager(es) por DEBAJO del limite de deuda permitido (20% de su plantilla): {nombres}. "
            "El presupuesto inicial asumido no encaja para ellos -- se guarda igualmente, marcado is_plausible=false."
        )

    rows = [{
        "league_id": str(real_league_id),
        "manager_id": str(f["manager_id"]),
        "manager_name": f["manager_name"],
        "team_id": str(f["team_id"]) if f["team_id"] else None,
        "team_value": f["team_value"],
        "total_spent": f["total_spent"],
        "total_income": f["total_income"],
        "estimated_money": f["estimated_money"],
        "debt_limit": f["debt_limit"],
        "starting_budget_assumed": starting_budget,
        "is_plausible": f["is_plausible"],
        "has_unverified_events": f["has_unverified_events"],
    } for f in finanzas]

    if rows:
        supabase.table("league_manager_finances").upsert(rows).execute()

    logging.info("Sincronizacion completada.")


def main():
    parser = argparse.ArgumentParser(description="Sincroniza el historial de actividad de tu liga y estima el dinero de cada manager.")
    parser.add_argument("--league-id", help="Fuerza un id de liga concreto (por defecto, autodetecta la primera).")
    parser.add_argument("--starting-budget", type=int, default=100_000_000, help="Presupuesto inicial asumido para todos los managers (por defecto 100.000.000).")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES_DEFAULT, help=f"Tope de paginas de historial a recorrer por pasada (por defecto {MAX_PAGES_DEFAULT}).")
    parser.add_argument("--full", action="store_true", help="Ignora lo ya sincronizado y recorre el historial completo de nuevo.")
    args = parser.parse_args()
    sync(league_id=args.league_id, starting_budget=args.starting_budget, max_pages=args.max_pages, full=args.full)


if __name__ == "__main__":
    main()
