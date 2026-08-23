"""
Busca en TODA tu liga quien tiene fichado a un jugador concreto -- util para
saber si esta libre (mercado) o de que manager tendrias que conseguirlo
(pujar si sale al mercado, cláusula, oferta directa...).

Usa league_data.py (recorre clasificacion + plantilla de cada manager).
Solo lectura -- no cambia nada en tu cuenta.

Uso:
    python find_player_owner.py "gaya"
    python find_player_owner.py "jose luis gaya"
"""

import sys

from laliga_auth import get_valid_access_token
from league_data import get_my_league_id, walk_all_rosters, normalizar


def find_owner(query, league_id=None):
    token = get_valid_access_token()
    real_league_id, league_name = get_my_league_id(token, forced_id=league_id)
    roster = walk_all_rosters(token, real_league_id)

    q = normalizar(query)
    matches = [
        p for p in roster
        if q in normalizar(p["player_name"]) or q in normalizar(p["nickname"])
    ]
    return matches, league_name


def fmt_money(n):
    return f"€{n:,}".replace(",", ".") if n else "—"


def main():
    if len(sys.argv) < 2:
        print('Uso: python find_player_owner.py "nombre del jugador"')
        sys.exit(1)
    query = " ".join(sys.argv[1:])

    print(f"Buscando '{query}' en las plantillas de tu liga (recorriendo todos los equipos)...")
    matches, _ = find_owner(query)

    if not matches:
        print(
            "\nNo se encontro ningun jugador fichado con ese nombre en ninguna plantilla. "
            "Puede que este libre en el mercado, o que el nombre no coincida -- "
            "prueba con el apellido o una parte más corta."
        )
        return

    print(f"\n{len(matches)} coincidencia(s):\n")
    for m in matches:
        locked_txt = f" (bloqueada hasta {m['clause_locked_until']})" if m["clause_locked_until"] else ""
        shield_txt = " 🛡️ BLINDADO (no se puede pagar la cláusula)" if m["shielded"] else ""
        print(f"{m['player_name']} ({m['nickname']}) -- {m['position']}, {m['real_team']}")
        print(f"  Lo tiene: {m['manager_name']}")
        print(f"  Valor de mercado: {fmt_money(m['market_value'])}")
        print(f"  Cláusula de rescisión: {fmt_money(m['buyout_clause'])}{locked_txt}{shield_txt}")
        print()


if __name__ == "__main__":
    main()
