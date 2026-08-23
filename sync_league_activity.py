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

Variables de entorno necesarias (o en scraper/.env -- ver .env.example):
    SUPABASE_URL
    SUPABASE_SERVICE_ROLE_KEY
"""

import argparse
import logging
import os
import sys

from dotenv import load_dotenv
from supabase import create_client, Client

from laliga_auth import get_valid_access_token
from league_data import get_my_league_id, get_standing, get_league_activity, compute_manager_finances

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

UPSERT_BLOQUE = 200
MAX_PAGES_DEFAULT = 200  # tope de seguridad para no entrar en bucle infinito si la API cambia de forma


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

    existing = supabase.table("league_activity").select("id").eq("league_id", str(real_league_id)).execute()
    known_ids = {row["id"] for row in existing.data} if not full else set()
    logging.info(f"{len(known_ids)} eventos ya sincronizados previamente." if not full else "Modo --full: re-descargando todo el historial disponible.")

    nuevos = fetch_new_activity(token, real_league_id, known_ids, max_pages, full=full)
    logging.info(f"{len(nuevos)} eventos nuevos descargados.")

    if nuevos:
        rows = [to_activity_row(real_league_id, it) for it in nuevos]
        for i in range(0, len(rows), UPSERT_BLOQUE):
            supabase.table("league_activity").upsert(rows[i:i + UPSERT_BLOQUE]).execute()

    # Recalcula las finanzas con TODO el historial acumulado en Supabase (no
    # solo lo descargado en esta pasada), para que el estimado no dependa de
    # cuantas paginas se hayan traido hoy.
    todo = supabase.table("league_activity").select("raw").eq("league_id", str(real_league_id)).execute()
    activity_completa = [row["raw"] for row in todo.data]
    logging.info(f"Recalculando finanzas con {len(activity_completa)} eventos en total (saldo inicial asumido: €{starting_budget:,})...")

    standing = get_standing(token, real_league_id)
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
