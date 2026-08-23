"""
Sincroniza el mercado de TU liga privada de LaLiga Fantasy Oficial con
Supabase, para poder mostrarlo en la web -- distinto de `players`, que es
el mercado PUBLICO de toda LaLiga (scrapeado de futbolfantasy.com por
futbolfantasy_scraper.py).

Origen de datos: API oficial, via laliga_auth.get_valid_access_token():
    GET /api/v1/competition/1/leagues                  -> localizar tu liga
    GET /api/v1/competition/1/league/{leagueId}/market  -> ofertas actuales

Destino: tabla public.league_market en Supabase. Ejecuta la migracion
database/migrations/002_add_league_market.sql en el SQL Editor de Supabase
UNA VEZ antes de correr este script por primera vez.

Uso:
    python sync_league_market.py                          # autodetecta tu liga
    python sync_league_market.py --league-id 018070031     # fuerza una liga concreta

Variables de entorno necesarias (o en scraper/.env -- ver .env.example):
    SUPABASE_URL
    SUPABASE_SERVICE_ROLE_KEY
"""

import argparse
import logging
import os
import sys

import requests
from dotenv import load_dotenv
from supabase import create_client, Client

from laliga_auth import get_valid_access_token
from league_data import get_my_league_id, BASE, UA, _headers

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

UPSERT_BLOQUE = 200  # mismo tamano de lote que futbolfantasy_scraper.py


def fetch_market(token, league_id):
    r = requests.get(f"{BASE}/league/{league_id}/market?x-lang=es", headers=_headers(token), timeout=20)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else (data.get("elements") or data.get("data") or [])


def to_row(league_id, item):
    pm = item.get("playerMaster", {}) or {}
    image_url = (
        pm.get("images", {}).get("transparent", {}).get("256x256")
        if isinstance(pm.get("images"), dict) else None
    )
    return {
        "id": str(item.get("id")),
        "league_id": str(league_id),
        "player_id": str(pm.get("id")),
        "player_name": pm.get("name") or pm.get("nickname"),
        "position": pm.get("position"),
        "team_id": pm.get("teamId"),
        "image_url": image_url,
        "market_value": pm.get("marketValue"),
        "sale_price": item.get("salePrice"),
        "status": item.get("status"),
        "number_of_bids": item.get("numberOfBids") or 0,
        "expiration_date": item.get("expirationDate"),
        "points": pm.get("points"),
        "average_points": pm.get("averagePoints"),
        "raw": item,
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
    real_league_id, _ = get_my_league_id(token, forced_id=league_id)
    logging.info(f"Sincronizando mercado de la liga {real_league_id}...")

    market = fetch_market(token, real_league_id)
    logging.info(f"{len(market)} jugadores en el mercado ahora mismo.")

    rows = [to_row(real_league_id, item) for item in market]
    current_ids = {row["id"] for row in rows}

    if rows:
        for i in range(0, len(rows), UPSERT_BLOQUE):
            supabase.table("league_market").upsert(rows[i:i + UPSERT_BLOQUE]).execute()

    # Borra ofertas que ya no estan en el mercado (compradas / caducadas) de
    # ESTA liga, sin tocar filas de otras ligas si algun dia se siguen varias.
    existing = supabase.table("league_market").select("id").eq("league_id", str(real_league_id)).execute()
    stale_ids = [row["id"] for row in existing.data if row["id"] not in current_ids]
    if stale_ids:
        supabase.table("league_market").delete().in_("id", stale_ids).execute()
        logging.info(f"{len(stale_ids)} ofertas retiradas del mercado eliminadas de Supabase.")

    logging.info("Sincronizacion completada.")


def main():
    parser = argparse.ArgumentParser(description="Sincroniza el mercado de tu liga de LaLiga Fantasy con Supabase.")
    parser.add_argument("--league-id", help="Fuerza un id de liga concreto (por defecto, autodetecta la primera).")
    args = parser.parse_args()
    sync(league_id=args.league_id)


if __name__ == "__main__":
    main()
