"""
Sincroniza QUIEN TIENE FICHADO A CADA JUGADOR en tu liga privada (las 15
plantillas completas) con Supabase, para poder mostrarlo en la web al lado
de cada jugador -- tanto en el mercado público (`players`) como en el
mercado de tu liga (`league_market`, ver sync_league_market.py).

Usa league_data.py (misma lógica que find_player_owner.py).

Destino: tabla public.league_roster en Supabase. Ejecuta la migracion
database/migrations/003_add_league_roster.sql en el SQL Editor de Supabase
UNA VEZ antes de correr este script por primera vez.

Uso:
    python sync_league_roster.py                          # autodetecta tu liga
    python sync_league_roster.py --league-id 018070031     # fuerza una liga concreta

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
from league_data import get_my_league_id, walk_all_rosters

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

UPSERT_BLOQUE = 200  # mismo tamano de lote que futbolfantasy_scraper.py / sync_league_market.py


def to_row(league_id, p):
    return {
        "id": str(p["playerTeamId"]),
        "league_id": str(league_id),
        "player_id": str(p["player_id"]),
        "player_name": p["player_name"],
        "nickname": p["nickname"],
        "position": p["position"],
        "real_team": p["real_team"],
        "market_value": p["market_value"],
        "team_id": str(p["team_id"]),
        "manager_id": str(p["manager_id"]) if p["manager_id"] else None,
        "manager_name": p["manager_name"],
        "buyout_clause": p["buyout_clause"],
        "clause_locked_until": p["clause_locked_until"],
        "shielded": p["shielded"],
    }


def sync(league_id=None):
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
    logging.info(f"Sincronizando plantillas de la liga {real_league_id} ({league_name})...")

    roster = walk_all_rosters(token, real_league_id)
    logging.info(f"{len(roster)} jugadores fichados en total (todos los managers).")

    rows = [to_row(real_league_id, p) for p in roster]
    current_ids = {row["id"] for row in rows}

    if rows:
        for i in range(0, len(rows), UPSERT_BLOQUE):
            supabase.table("league_roster").upsert(rows[i:i + UPSERT_BLOQUE]).execute()

    # Borra fichas que ya no existen (jugador vendido/liberado) de ESTA liga.
    existing = supabase.table("league_roster").select("id").eq("league_id", str(real_league_id)).execute()
    stale_ids = [row["id"] for row in existing.data if row["id"] not in current_ids]
    if stale_ids:
        supabase.table("league_roster").delete().in_("id", stale_ids).execute()
        logging.info(f"{len(stale_ids)} fichas obsoletas eliminadas de Supabase.")

    logging.info("Sincronizacion completada.")


def main():
    parser = argparse.ArgumentParser(description="Sincroniza quien tiene fichado a cada jugador de tu liga con Supabase.")
    parser.add_argument("--league-id", help="Fuerza un id de liga concreto (por defecto, autodetecta la primera).")
    args = parser.parse_args()
    sync(league_id=args.league_id)


if __name__ == "__main__":
    main()
