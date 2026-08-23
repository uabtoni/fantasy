"""
Helpers compartidos para recorrer TU liga privada de LaLiga Fantasy Oficial
(clasificacion + plantilla de cada manager), usados por find_player_owner.py
y sync_league_roster.py. Ver tambien sync_league_market.py (mercado, no
plantillas) que usa el mismo patron de autenticacion.

No existe un endpoint de "quien tiene a X" ni de "plantilla de toda la
liga" en la API oficial -- hay que recorrer manualmente (igual que hace
LaLigaApp en src/utils/fetchAllTeamsData.js):
  1. GET /leagues                        -> localizar tu liga
  2. GET /leagues/{id}/standing           -> lista de managers/equipos de tu liga
  3. GET /leagues/{id}/teams/{teamId}     -> plantilla (15 jugadores) de cada uno

El "Historial" de la liga (compras, ventas, cláusulas, blindajes, ganancias
por jornada...) sí tiene endpoint propio -- confirmado leyendo
src/services/api.js y src/components/Activity/ de LaLigaApp
(github.com/Externoak/LaLigaApp), que lo llaman `getLeagueActivity`:
  4. GET /leagues/{id}/activity/{index}   -> pagina `index` (0, 1, 2...) del
                                              historial, mas reciente primero.
                                              Deja de devolver items cuando se
                                              acaba (lista vacia). Ver
                                              test_league_history.py.
"""

import time
import unicodedata

import requests

BASE = "https://fantasy-api.llt-services.com/api/v1/competition/1"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
DELAY_BETWEEN_TEAMS = 0.3  # segundos entre peticion y peticion, para no disparar rate limits

POSITION_MAP = {1: "Portero", 2: "Defensa", 3: "Centrocampista", 4: "Delantero", 5: "Entrenador"}


def _headers(token):
    return {"Authorization": f"Bearer {token}", "x-lang": "es", "x-app": "2", "User-Agent": UA}


def normalizar(texto):
    """minusculas, sin acentos, sin espacios sobrantes -> para cruzar nombres con fiabilidad
    (misma lógica que futbolfantasy_scraper.py, para poder cruzar nombres entre las dos fuentes)."""
    texto = (texto or "").lower().strip()
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    return " ".join(texto.split())


def get_my_league_id(token, forced_id=None):
    if forced_id:
        return forced_id, None
    r = requests.get(f"{BASE}/leagues?x-lang=es", headers=_headers(token), timeout=15)
    r.raise_for_status()
    data = r.json()
    leagues = data if isinstance(data, list) else (data.get("elements") or data.get("leagues") or data.get("data") or [])
    if not leagues:
        raise RuntimeError("No se encontro ninguna liga para este usuario.")
    return leagues[0]["id"], leagues[0].get("name")


def get_standing(token, league_id):
    r = requests.get(f"{BASE}/leagues/{league_id}/standing?x-lang=es", headers=_headers(token), timeout=15)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else (data.get("elements") or data.get("data") or [])


def get_team_roster(token, league_id, team_id):
    r = requests.get(f"{BASE}/leagues/{league_id}/teams/{team_id}?x-lang=es", headers=_headers(token), timeout=15)
    r.raise_for_status()
    return r.json().get("players", [])


def walk_all_rosters(token, league_id, standing=None):
    """
    Recorre la plantilla de cada manager de la liga. Devuelve una lista de
    dicts (uno por jugador fichado) con todo lo util: quien lo tiene,
    cláusula, si esta blindado, valor de mercado, etc.
    """
    if standing is None:
        standing = get_standing(token, league_id)

    out = []
    for entry in standing:
        team = entry.get("team", {}) or {}
        team_id = team.get("id")
        manager_name = (team.get("manager") or {}).get("managerName", "?")
        manager_id = (team.get("manager") or {}).get("id")
        if not team_id:
            continue

        roster = get_team_roster(token, league_id, team_id)
        time.sleep(DELAY_BETWEEN_TEAMS)

        for pt in roster:
            pm = pt.get("playerMaster", {}) or {}
            out.append({
                "playerTeamId": pt.get("playerTeamId") or pt.get("id"),
                "player_id": pm.get("id"),
                "player_name": pm.get("name") or pm.get("nickname"),
                "nickname": pm.get("nickname"),
                "position": POSITION_MAP.get(pm.get("positionId"), "?"),
                "real_team": (pm.get("team") or {}).get("name"),
                "market_value": pm.get("marketValue"),
                "team_id": team_id,
                "manager_id": manager_id,
                "manager_name": manager_name,
                "buyout_clause": pt.get("buyoutClause"),
                "clause_locked_until": pt.get("buyoutClauseLockedEndTime"),
                "shielded": bool(pt.get("isShielded")),
            })
    return out


def get_league_activity(token, league_id, index=0):
    """
    Una pagina del historial de actividad de la liga (mercado, cláusulas,
    blindajes, ganancias por jornada, altas de manager...). `index` empieza
    en 0 (mas reciente) y sube; la API devuelve una lista vacia al llegar al
    final. Cada item trae, segun el tipo, cosas como:
        activityTypeId  (1 compró, 4 blindó, 6 ganancia jornada,
                          7 alineación incorrecta, 9 se unió a la liga,
                          31 fichó, 32 clausuló, 33 vendió)
        amount                          -> importe (cuando aplica)
        user1Id / user1Name             -> quien hace la accion (comprador)
        user2Id / user2Name             -> la otra parte (vendedor), NO
                                            siempre presente de forma
                                            estructurada -- a veces solo esta
                                            dentro de `description` en texto
        playerMasterId / playerName     -> jugador implicado (a veces solo
                                            el id, sin nombre)
        createdAt / timestamp
        description                     -> frase ya redactada, util como
                                            fallback si faltan los campos de
                                            arriba
    """
    r = requests.get(f"{BASE}/leagues/{league_id}/activity/{index}?x-lang=es", headers=_headers(token), timeout=15)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else (data.get("elements") or data.get("data") or [])


def walk_all_activity(token, league_id, max_pages=10, delay=0.3):
    """Recorre paginas de get_league_activity() hasta que una vuelve vacia
    o se alcanza max_pages. Devuelve la lista concatenada (mas reciente
    primero)."""
    out = []
    for index in range(max_pages):
        page = get_league_activity(token, league_id, index)
        if not page:
            break
        out.extend(page)
        time.sleep(delay)
    return out


# Reglas de movimiento de dinero por activityTypeId, deducidas y calibradas
# contra el saldo REAL de un manager (GET /teams/{id}/money, solo visible
# para tu propio equipo -- ver scraper/test_league_history.py). Confirmado
# con datos reales de la liga para los tipos 1/31/33/6. Los tipos 32
# (clausuló) y 4 (blindó) NO se han visto todavia en ninguna liga de
# prueba -- su regla es una extrapolacion razonable (mismo patron
# comprador/vendedor que el tipo 1), no una certeza.
MONEY_RULES_VERIFICADAS = {1, 31, 33, 6}
MONEY_RULES_SIN_VERIFICAR = {32}  # 4 (blindó) se asume sin coste, LaLigaApp tampoco lo trata como gasto


def _money_delta(item, manager_id):
    t = item.get("activityTypeId")
    amount = item.get("amount") or 0
    u1, u2 = item.get("user1Id"), item.get("user2Id")
    delta = 0
    if t in (1, 31, 32):  # compró (oferta directa) / fichó (mercado) / clausuló -> gasto para quien actua
        if u1 == manager_id:
            delta -= amount
        if u2 == manager_id:  # la otra parte (vendedor / antiguo dueño) cobra -- solo aplica al tipo 1 y 32
            delta += amount
    elif t == 33:  # vendió al mercado -> ingreso
        if u1 == manager_id:
            delta += amount
    elif t == 6:  # ganancia por jornada
        if u1 == manager_id:
            delta += amount
    return delta


# LaLiga Fantasy SI permite quedarte en negativo pujando en el mercado
# (fichajes a la IA, tipos 31/33) -- el limite de endeudamiento es el 20%
# del valor de tu plantilla (teamValue). Lo que NO se puede es pagar a
# otro manager (oferta directa tipo 1, clausula tipo 32) sin tener el
# dinero -- esas si exigen saldo suficiente en el momento de pagarse.
# Confirmado por el usuario (no es un dato de la API). Por eso
# is_plausible compara contra ese limite, no contra cero.
DEBT_LIMIT_RATIO = 0.20


def compute_manager_finances(activity, standing, starting_budget=100_000_000, debt_limit_ratio=DEBT_LIMIT_RATIO):
    """
    Reconstruye, para cada manager de la liga, un dinero ESTIMADO (no
    oficial) sumando todos los movimientos de `activity` (walk_all_activity)
    a partir de un saldo inicial asumido igual para todos.

    ADVERTENCIA -- esto es una aproximacion, no un dato oficial:
      - La API oficial solo expone el saldo real (GET /teams/{id}/money)
        para TU PROPIO equipo (403 Forbidden en el resto) -- no hay forma
        de verificar el resultado para los demas managers.
      - Se asume el mismo `starting_budget` para todos. Si un manager
        recibio un presupuesto distinto (o el reparto inicial de plantilla
        no es dinero-neutro), su estimacion sera erronea.
      - Un estimated_money negativo NO es, por si solo, señal de error:
        el juego permite endeudarse hasta `debt_limit_ratio` del valor de
        la plantilla via mercado (ver DEBT_LIMIT_RATIO). Solo se marca
        is_plausible=False si supera ESE limite -- eso si seria una señal
        real de que el starting_budget asumido esta mal para ese manager.
      - Los tipos 32 (clausuló) y 4 (blindó) usan una regla no verificada
        (ver MONEY_RULES_SIN_VERIFICAR).

    Devuelve una lista de dicts, uno por manager, con: manager_id,
    manager_name, team_id, team_value, total_spent, total_income,
    estimated_money, debt_limit (el suelo permitido, <= 0),
    is_plausible (False si estimated_money esta POR DEBAJO del limite de
    endeudamiento -- señal de que el estimado esta mal para ese manager),
    has_unverified_events.
    """
    out = []
    for entry in standing:
        team = entry.get("team", {}) or {}
        manager = team.get("manager") or {}
        manager_id = manager.get("id")
        if manager_id is None:
            continue
        manager_id = int(manager_id)

        spent = 0
        income = 0
        has_unverified = False
        for it in activity:
            if it.get("user1Id") != manager_id and it.get("user2Id") != manager_id:
                continue
            if it.get("activityTypeId") in MONEY_RULES_SIN_VERIFICAR:
                has_unverified = True
            d = _money_delta(it, manager_id)
            if d > 0:
                income += d
            elif d < 0:
                spent += -d

        estimated_money = starting_budget + income - spent
        team_value = team.get("teamValue")
        debt_limit = round(-debt_limit_ratio * team_value) if team_value else 0
        out.append({
            "manager_id": manager_id,
            "manager_name": manager.get("managerName", "?"),
            "team_id": team.get("id"),
            "team_value": team_value,
            "total_spent": spent,
            "total_income": income,
            "estimated_money": estimated_money,
            "debt_limit": debt_limit,
            "is_plausible": estimated_money >= debt_limit,
            "has_unverified_events": has_unverified,
        })
    return out


def get_all_players(token):
    """
    Catalogo COMPLETO de jugadores de la competicion (no solo los fichados
    en tu liga) -- id -> nombre/equipo/etc. Necesario para resolver
    playerMasterId del historial de actividad cuando el jugador ya no esta
    en ninguna plantilla actual (walk_all_rosters() solo ve plantillas
    vigentes). Mismo endpoint que usa LaLigaApp como `getAllPlayers`
    (src/services/api.js): GET /players.
    """
    r = requests.get(f"{BASE}/players?x-lang=es", headers=_headers(token), timeout=20)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else (data.get("elements") or data.get("data") or [])
