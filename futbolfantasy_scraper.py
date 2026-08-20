"""
Scraper de FutbolFantasy (equipos de LaLiga) -> Supabase

Fuente de datos (verificado contra el HTML real del sitio, no adivinado):

1. https://www.futbolfantasy.com/analytics/laliga-fantasy/mercado
   Esta tabla se pinta con JavaScript (por eso hace falta Playwright, un
   navegador headless real -- con requests normal devuelve 0 filas).
   Cada fila es <tr class="elemento_jugador" data-nombre="..."
   data-posicion="..." data-valor="..."> y esos atributos ya traen,
   directamente y sin necesidad de parsear texto, el NOMBRE, la POSICIÓN
   y el PRECIO actual de cada jugador de LaLiga. Es la fuente principal:
   una sola carga para los ~374+ jugadores de toda la liga.

2. Página de alineación probable de cada equipo (team_url, con requests
   normal, sin Playwright) -> únicamente para el ESTADO (Disponible /
   Lesionado-Duda) vía el atributo data-lesion de cada tarjeta de
   jugador, que no viene en la tabla de mercado.
"""

import time
import random
import re
import sys
import logging
import os
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from bs4 import BeautifulSoup
from supabase import create_client, Client
from playwright.sync_api import sync_playwright

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

BASE_URL = "https://www.futbolfantasy.com"
TEAMS_URL = BASE_URL
MERCADO_URL = f"{BASE_URL}/analytics/laliga-fantasy/mercado"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
    "Accept-Language": "es-ES,es;q=0.9",
    "Referer": "https://www.google.com/"
}

POSICIONES_VALIDAS = {"Portero", "Defensa", "Mediocampista", "Delantero"}
STORAGE_BUCKET = "player-photos"
FOTOS_WORKERS = 8  # descargas/subidas de fotos en paralelo (I/O-bound, no CPU-bound)
UPSERT_BLOQUE = 200  # filas por lote al subir a Supabase (mismo límite que ya usa el histórico)

# Sesiones reutilizadas para aprovechar el connection pooling de requests en
# vez de abrir una conexión TCP/TLS nueva por cada petición (misma lógica,
# solo más rápido). Son seguras para usarse desde varios hilos a la vez.
_html_session = requests.Session()
_html_session.headers.update(HEADERS)

_img_session = requests.Session()
_img_session.headers.update({
    "User-Agent": HEADERS["User-Agent"],
    "Referer": BASE_URL + "/",
})


def normalizar(texto):
    """minusculas, sin acentos, sin espacios sobrantes -> para cruzar nombres con fiabilidad"""
    texto = (texto or "").lower().strip()
    texto = unicodedata.normalize('NFKD', texto).encode('ascii', 'ignore').decode('ascii')
    return " ".join(texto.split())


def get_html(url):
    try:
        time.sleep(random.uniform(1.0, 2.5))
        response = _html_session.get(url, timeout=20)
        response.raise_for_status()
        return response.text
    except requests.RequestException as e:
        logging.error(f"Error al acceder a {url}: {e}")
        return None


def fetch_teams():
    html = get_html(TEAMS_URL)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    teams_links = []
    candidates = soup.select("a.team") or soup.select('a[href*="/laliga/equipos/"]')

    for link in candidates:
        href = link.get('href')
        if not href or "/equipos/" not in href:
            continue
        full_url = href if href.startswith("http") else f"{BASE_URL}{href}"
        full_url = full_url.split("?")[0].rstrip("/")
        if full_url not in teams_links:
            teams_links.append(full_url)

    primera_division = teams_links[:20]
    logging.info(f"Se han filtrado {len(primera_division)} equipos de Primera División.")
    return primera_division


# Los iconos de estado físico usan siempre este nombre de archivo,
# independientemente de en qué sección de la página aparezcan (once,
# alternativas, o el bloque "Estado físico de la plantilla"). Detectar
# por el nombre de archivo del icono es más fiable que depender de una
# clase CSS concreta, que puede cambiar entre secciones. (La lógica de
# búsqueda vive dentro del JS de fetch_all_status_maps, ya que esta
# sección se pinta con JavaScript y hace falta Playwright para leerla.)


def fetch_all_status_maps(team_urls):
    """
    (status_map, touched_names) para TODOS los equipos a la vez:
      - status_map: {nombre_normalizado: "Lesionado"|"Duda"} -- incidencias
        reales (baja o duda para el próximo partido).
      - touched_names: set de nombres normalizados marcados por el sitio
        como "tocado o sale de lesión, pero disponible" (icono
        disponible_box_min). NO es una incidencia -- el propio sitio los
        clasifica con la severidad más baja (gravedad-2) y los muestra
        como disponibles para jugar. Se devuelve aparte (y no se mezcla
        en status_map) para no contaminar el conteo de incidencias reales
        ni el campo `status` del jugador con algo que en realidad significa
        "puede jugar".

    CONFIRMADO EN PRUEBA REAL: la sección "Estado físico de la plantilla"
    (y en general los iconos de lesión/duda) se pintan con JavaScript --
    con requests normal se detectan 0 incidencias en las 20 páginas. Por
    eso, igual que con la tabla de Mercado, hace falta renderizar la
    página con Playwright.

    Reutilizamos UN solo navegador para las 20 páginas de equipo en vez
    de abrir uno por equipo (mucho más rápido), y hacemos la búsqueda de
    iconos + nombre de jugador con page.evaluate() en JS directamente en
    el navegador, que es muchísimo más rápido que iterar elemento a
    elemento desde Python.
    """
    status_map = {}
    touched_names = set()

    js_extraer_estados = """
    () => {
      // Solo lesionado_box_min / duda_box_min son incidencias reales.
      // disponible_box_min ("tocado, pero disponible") se recoge aparte
      // en `tocados`, nunca como una incidencia más -- ver docstring de
      // fetch_all_status_maps.
      const incidenciaMap = {
        'lesionado_box_min': 'Lesionado',
        'duda_box_min': 'Duda',
      };
      const prioridadIncidencia = { 'Lesionado': 2, 'Duda': 1 };
      const TOCADO_KEY = 'disponible_box_min';

      const incidencias = {};
      const tocados = {};

      const buscarNombre = (img) => {
        let el = img.parentElement;
        for (let i = 0; i < 6 && el; i++) {
          // Puede haber varios <a href="/jugadores/..."> en el mismo
          // contenedor (uno para la foto, sin texto, y otro para el
          // nombre). Nos quedamos con el primero que SÍ tenga texto.
          const candidatos = el.querySelectorAll('a[href*="/jugadores/"]');
          for (const candidato of candidatos) {
            const texto = candidato.textContent.trim();
            if (texto) return texto;
          }
          el = el.parentElement;
        }
        return null;
      };

      document.querySelectorAll('img[src]').forEach(img => {
        if (img.src.includes(TOCADO_KEY)) {
          const nombre = buscarNombre(img);
          if (nombre) tocados[nombre] = true;
          return;
        }

        for (const key in incidenciaMap) {
          if (!img.src.includes(key)) continue;
          const estado = incidenciaMap[key];
          const nombre = buscarNombre(img);
          if (nombre && (!incidencias[nombre] || prioridadIncidencia[estado] > prioridadIncidencia[incidencias[nombre]])) {
            incidencias[nombre] = estado;
          }
          return;
        }
      });

      return { incidencias, tocados };
    }
    """

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-dev-shm-usage", "--no-sandbox"])
        page = browser.new_page(user_agent=HEADERS["User-Agent"])

        for team_url in team_urls:
            try:
                page.goto(team_url, timeout=45000, wait_until="domcontentloaded")
                page.wait_for_timeout(2000)
                resultado = page.evaluate(js_extraer_estados)
                for nombre, estado in resultado["incidencias"].items():
                    status_map[normalizar(nombre)] = estado
                for nombre in resultado["tocados"]:
                    touched_names.add(normalizar(nombre))
                logging.info(
                    f"Estado físico {team_url.split('/')[-1]}: "
                    f"{len(resultado['incidencias'])} incidencias, {len(resultado['tocados'])} tocados"
                )
            except Exception as e:
                logging.warning(f"No se pudo leer el estado físico de {team_url}: {e}")
                continue

        browser.close()

    return status_map, touched_names


def _fetch_all_market_data_intento():
    """
    Único fetch con Playwright a la tabla de Mercado de LaLiga Fantasy
    Oficial (SIN filtrar por equipo). Lee directamente los atributos
    data-nombre / data-posicion / data-valor de cada <tr
    class="elemento_jugador">. Devuelve una lista de dicts con nombre,
    equipo, posición y precio para TODOS los jugadores de LaLiga.
    """
    jugadores = []
    vistos = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-dev-shm-usage", "--no-sandbox"])
        page = browser.new_page(user_agent=HEADERS["User-Agent"])

        logging.info(f"Cargando {MERCADO_URL} con navegador headless...")

        cargado = False
        ultimo_error = None
        for intento in range(1, 4):
            try:
                # domcontentloaded en vez de load: no esperamos anuncios,
                # analíticas ni recursos de terceros, solo el HTML/JS base.
                page.goto(MERCADO_URL, timeout=45000, wait_until="domcontentloaded")
                page.wait_for_selector("tr.elemento_jugador", timeout=30000)
                cargado = True
                break
            except Exception as e:
                ultimo_error = e
                logging.warning(f"Intento {intento}/3 fallido al cargar la tabla de mercado: {e}")
                page.wait_for_timeout(5000 * intento)  # backoff antes de reintentar

        if not cargado:
            logging.error(f"No se pudo cargar la tabla de mercado tras 3 intentos: {ultimo_error}")
            browser.close()
            return []

        page.wait_for_timeout(2000)

        # El banner de cookies no bloquea los datos (ya están en el DOM
        # aunque el banner tape la pantalla), pero lo cerramos por si
        # interfiere con el botón "Siguiente" de la paginación.
        for selector in ["button:has-text('Aceptar todo')", "text=Aceptar todo"]:
            try:
                boton = page.query_selector(selector)
                if boton:
                    boton.click(timeout=3000)
                    page.wait_for_timeout(1000)
                    logging.info("Banner de cookies cerrado.")
                    break
            except Exception:
                continue

        pagina = 1
        paginas_vacias_seguidas = 0
        while True:
            page.wait_for_timeout(800)
            filas = page.query_selector_all("tr.elemento_jugador")
            nuevos = 0

            for fila in filas:
                data_id = fila.get_attribute("data-id")
                if not data_id or data_id in vistos:
                    continue

                posicion_raw = fila.get_attribute("data-posicion") or ""
                if posicion_raw not in POSICIONES_VALIDAS:
                    continue  # descarta entrenadores u otras filas no-jugador

                valor_raw = fila.get_attribute("data-valor") or "0"
                try:
                    precio = int(valor_raw)
                except ValueError:
                    precio = 0

                # Diferencia oficial respecto al mercado anterior (la que
                # el propio sitio muestra en la columna "Diferencia").
                diff_raw = fila.get_attribute("data-diferencia1")
                try:
                    diferencia = int(diff_raw) if diff_raw is not None else 0
                except ValueError:
                    diferencia = 0

                nombre_tag = fila.query_selector(".player-name span")
                nombre = nombre_tag.inner_text().strip() if nombre_tag else (fila.get_attribute("data-nombre") or "").title()

                equipo_tag = fila.query_selector(".player-equipo span")
                equipo = equipo_tag.inner_text().strip() if equipo_tag else ""

                posicion = "Centrocampista" if posicion_raw == "Mediocampista" else posicion_raw

                # Foto del jugador: viene en un <img class="player-foto">
                # dentro de la propia fila. Pedimos una versión más grande
                # (400x400) cambiando el tramo de la URL, ya que la tabla
                # solo carga miniaturas de 80x80.
                foto_url = None
                foto_tag = fila.query_selector("img.player-foto")
                if foto_tag:
                    src = foto_tag.get_attribute("src") or ""
                    if src:
                        foto_url = src.replace("/thumb/80x80/", "/thumb/400x400/")

                # Próximo rival y probabilidad de titularidad: ya vienen
                # en esta misma fila, en el título del bloque ".rival-probability"
                # (ej. "Jornada 2 · Próximo rival: Elche (Fuera)") y en un
                # span interno con la probabilidad ("80%").
                next_rival, rival_home, probabilidad = None, None, None
                rival_el = fila.query_selector(".rival-probability")
                if rival_el:
                    titulo = rival_el.get_attribute("title") or ""
                    m = re.search(r'Próximo rival:\s*(.+?)\s*\((Casa|Fuera)\)', titulo)
                    if m:
                        next_rival = m.group(1).strip()
                        rival_home = (m.group(2) == "Casa")
                    prob_el = rival_el.query_selector("[class*='prob-']")
                    if prob_el:
                        prob_texto = prob_el.inner_text().strip().replace("%", "")
                        if prob_texto.isdigit():
                            probabilidad = int(prob_texto)

                vistos.add(data_id)
                nuevos += 1
                jugadores.append({
                    "nombre": nombre,
                    "team": equipo,
                    "position": posicion,
                    "price": precio,
                    "price_diff": diferencia,
                    "next_rival": next_rival,
                    "rival_home": rival_home,
                    "start_probability": probabilidad,
                    "photo_url": foto_url,
                })

            logging.info(f"Mercado - página {pagina}: {nuevos} jugadores nuevos (total: {len(jugadores)})")

            # Si varias páginas seguidas no traen NINGÚN jugador nuevo, es
            # que en realidad ya está todo cargado desde el principio (la
            # tabla no pagina de verdad, o el botón "Siguiente" no lleva a
            # contenido distinto). Cortamos aquí en vez de seguir clicando
            # decenas de veces hasta que el click acabe dando timeout.
            paginas_vacias_seguidas = paginas_vacias_seguidas + 1 if nuevos == 0 else 0
            if paginas_vacias_seguidas >= 2:
                logging.info("2 páginas seguidas sin jugadores nuevos: se da por completa la tabla de mercado.")
                break

            siguiente = page.query_selector("a:has-text('Siguiente')")
            if not siguiente:
                logging.info("Sin botón de siguiente página: fin de la paginación.")
                break
            clase = siguiente.get_attribute("class") or ""
            if "disabled" in clase:
                logging.info("Botón de siguiente página deshabilitado: fin de la paginación.")
                break
            try:
                siguiente.click()
                pagina += 1
                if pagina > 50:
                    logging.warning("Límite de seguridad de 50 páginas alcanzado.")
                    break
            except Exception as e:
                logging.info(f"No se pudo avanzar de página ({e}): fin de la paginación.")
                break

        browser.close()

    logging.info(f"Mercado: {len(jugadores)} jugadores obtenidos en total.")
    return jugadores


# Un mercado completo real tiene ~560-600 jugadores. Si un intento se
# queda muy por debajo (navegador que se cae a medias, /dev/shm
# saturado en el runner de GitHub Actions, etc.), lo tratamos como un
# fallo y reintentamos desde cero en vez de quedarnos con datos a medias.
MINIMO_JUGADORES_VALIDO = 300


def fetch_all_market_data():
    for intento in range(1, 4):
        jugadores = _fetch_all_market_data_intento()
        if len(jugadores) >= MINIMO_JUGADORES_VALIDO:
            return jugadores
        logging.warning(
            f"Intento {intento}/3: el mercado solo trajo {len(jugadores)} jugadores "
            f"(se esperaban {MINIMO_JUGADORES_VALIDO}+). Relanzando navegador desde cero..."
        )
        time.sleep(10)

    logging.error(
        f"No se consiguió un scrapeo válido del mercado tras 3 intentos completos "
        f"(último resultado: {len(jugadores)} jugadores). Se aborta esta ejecución "
        f"SIN tocar Supabase, para no pisar los datos buenos que ya había."
    )
    return []


def scrape_all_data():
    logging.info("Descargando tabla de Mercado (nombre + posición + precio) para toda La Liga...")
    jugadores_mercado = fetch_all_market_data()

    if not jugadores_mercado:
        # fetch_all_market_data ya ha registrado el error con detalle.
        # No seguimos: ni gastamos tiempo en el estado físico, ni
        # llegamos a update_database, así que Supabase se queda tal
        # cual estaba con los datos de la última ejecución buena.
        return []

    team_urls = fetch_teams()

    logging.info("Descargando estado físico (lesión/duda) de todos los equipos con Playwright...")
    status_map, touched_names = fetch_all_status_maps(team_urls)

    logging.info(f"Estados detectados: {dict(Counter(status_map.values()))} "
                 f"(sobre {len(status_map)} jugadores con alguna incidencia)")
    logging.info(f"Jugadores tocados (disponibles, no es incidencia): {len(touched_names)}")

    all_players = []
    for j in jugadores_mercado:
        nombre_norm = normalizar(j["nombre"])
        estado = status_map.get(nombre_norm, "Disponible")
        tocado = nombre_norm in touched_names
        player_id = f'{j["team"]}-{j["nombre"]}'.lower().replace(" ", "-")

        all_players.append({
            "id": player_id,
            "name": j["nombre"],
            "team": j["team"],
            "position": j["position"],
            "price": j["price"],
            "price_diff": j.get("price_diff", 0),
            "points": 0,
            "status": estado,
            "touched": tocado,
            "url": "",
            "stats": {},
            "next_rival": j.get("next_rival"),
            "rival_home": j.get("rival_home"),
            "start_probability": j.get("start_probability"),
            "photo_url": j.get("photo_url"),
        })

    return all_players


def subir_foto_a_storage(supabase, player_id, foto_url):
    """
    Descarga la foto desde futbolfantasy.com (con nuestro propio scraper,
    que sí tiene un Referer legítimo de su propio dominio) y la vuelve a
    subir a nuestro bucket de Supabase Storage. Así, cuando el navegador
    del usuario pida la imagen, la pide a Supabase (nuestro dominio),
    nunca directamente a futbolfantasy.com -- evitando cualquier
    protección anti-hotlink de su CDN.

    Devuelve la URL pública en Supabase, o None si algo falla (en cuyo
    caso se deja el campo de foto vacío en vez de romper el resto).
    """
    if not foto_url:
        return None
    try:
        resp = _img_session.get(foto_url, timeout=15)
        resp.raise_for_status()

        # Supabase Storage rechaza (400) claves de objeto con tildes/ñ, así que
        # usamos una versión ASCII de player_id solo para el nombre de archivo;
        # el id original (con acentos) se conserva intacto en la fila de la BD.
        path = f"{normalizar(player_id)}.png"
        supabase.storage.from_(STORAGE_BUCKET).upload(
            path,
            resp.content,
            {"content-type": "image/png", "upsert": "true"},
        )
        return supabase.storage.from_(STORAGE_BUCKET).get_public_url(path)
    except Exception as e:
        logging.debug(f"No se pudo subir la foto de {player_id}: {e}")
        return None


def update_database(players_list):
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://jcfzocfbgidbdwatiisr.supabase.co")
    # El workflow de GitHub Actions (y .env.example) usan SUPABASE_SERVICE_ROLE_KEY;
    # se mantiene SUPABASE_KEY como alias por compatibilidad con configuraciones locales.
    SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImpjZnpvY2ZiZ2lkYmR3YXRpaXNyIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4NjM5NzY3NywiZXhwIjoyMTAxOTczNjc3fQ.zb1wfhkJHksCZhW_Y3-ywX0HsOiWY2URiWbB_mBlafM")

    if not SUPABASE_URL or not SUPABASE_KEY:
        logging.warning("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY no configuradas como variables de entorno. "
                         "No se sube nada, solo se muestra en consola.")
        return

    try:
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

        # Descargas/subidas en paralelo (I/O-bound): mismo resultado que
        # hacerlo secuencial, pero sin esperar una foto detrás de otra.
        logging.info("Descargando y subiendo fotos de jugadores a Supabase Storage...")
        subidas_ok = 0
        procesadas = 0
        with ThreadPoolExecutor(max_workers=FOTOS_WORKERS) as executor:
            futuro_a_player = {
                executor.submit(subir_foto_a_storage, supabase, player["id"], player.get("photo_url")): player
                for player in players_list
            }
            for futuro in as_completed(futuro_a_player):
                player = futuro_a_player[futuro]
                nueva_url = futuro.result()
                if nueva_url:
                    player["photo_url"] = nueva_url
                    subidas_ok += 1
                procesadas += 1
                if procesadas % 50 == 0:
                    logging.info(f"Fotos procesadas: {procesadas}/{len(players_list)}")
        logging.info(f"Fotos subidas correctamente: {subidas_ok}/{len(players_list)}")

        # Upsert por lotes en vez de una llamada por jugador: mismo efecto
        # final en la tabla, muchas menos peticiones HTTP.
        logging.info("Subiendo jugadores a Supabase...")
        for i in range(0, len(players_list), UPSERT_BLOQUE):
            supabase.table('players').upsert(players_list[i:i + UPSERT_BLOQUE]).execute()
        logging.info("¡Jugadores actualizados!")

        # Histórico de precios: una fila nueva por jugador en cada
        # ejecución (no se sobrescribe nada), para poder pintar la
        # evolución del precio más adelante.
        logging.info("Guardando histórico de precios...")
        historial = [
            {"player_id": p["id"], "price": p["price"]}
            for p in players_list if p["price"] and p["price"] > 0
        ]
        # Insertamos en bloques para no exceder límites de tamaño de payload
        for i in range(0, len(historial), UPSERT_BLOQUE):
            supabase.table('price_history').insert(historial[i:i + UPSERT_BLOQUE]).execute()
        logging.info(f"Histórico guardado: {len(historial)} registros.")

        logging.info("¡Base de datos actualizada con éxito!")
    except Exception as e:
        logging.error(f"Error al subir a Supabase: {e}")


if __name__ == "__main__":
    logging.info("Iniciando extracción de FutbolFantasy...")
    datos_jugadores = scrape_all_data()
    logging.info(f"Extracción completada. Total de jugadores: {len(datos_jugadores)}")

    if not datos_jugadores:
        logging.error("Sin datos válidos esta ejecución. Terminando con error para que quede marcado en GitHub Actions.")
        sys.exit(1)

    con_precio = sum(1 for p in datos_jugadores if p["price"] > 0)
    logging.info(f"Con precio > 0: {con_precio}/{len(datos_jugadores)}")
    # Nota: antes había un contador "con posición conocida" comparando contra
    # "Desconocida", una posición que este scraper nunca asigna (la fila se
    # descarta en el filtro de POSICIONES_VALIDAS si no es una de las 4
    # válidas) -- esa métrica siempre daba el 100%, era código muerto que no
    # detectaba nada. El desglose por posición sí es información real.
    logging.info(f"Desglose por posición: {dict(Counter(p['position'] for p in datos_jugadores))}")

    print("\nEjemplo de 3 jugadores extraídos:")
    for p in datos_jugadores[-3:]:
        print(p)

    update_database(datos_jugadores)
