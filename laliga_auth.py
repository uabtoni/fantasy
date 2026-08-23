"""
Login interactivo (OAuth2 + PKCE, incluye "Continuar con Google") contra la
API OFICIAL de LaLiga Fantasy, para conseguir un access_token/refresh_token
reutilizable por el resto del scraper -- sin tener que copiar el token a
mano desde las DevTools cada vez.

Lógica calcada (no adivinada) de la app de escritorio LaLigaApp
(https://github.com/Externoak/LaLigaApp), en concreto de:
  - src/services/authService.js  (endpoints, client_id, exchange/refresh)
  - src/utils/pkce.js            (code_verifier / code_challenge S256)
  - electron/ipc-handlers.js     (captura del `code` interceptando la
    navegación al redirect_uri de esquema personalizado ANTES de que falle)
  - src/components/Auth/Login.js (armado de la URL de /authorize)

fantasy.laliga.com NO es jugable en web (solo landing que manda a la app);
el login real es contra el tenant Azure AD B2C de LaLiga
(login.laliga.es/laligadspprob2c.onmicrosoft.com), con política de
sign-in "B2C_1A_5ULAIP_PARAMETRIZED_SIGNIN" que deja elegir entre
email/contraseña o un IdP externo (Google).

FLUJO:
  1. Se genera un par PKCE (code_verifier + code_challenge S256) y un
     `state` aleatorio.
  2. Se abre una ventana de Chromium real (Playwright, NO headless) en la
     URL de /authorize, usando el client_id "nativo" (el mismo que usa el
     login por email de la app -- es el único que LaLiga tiene registrado
     con el redirect_uri de esquema personalizado `authredirect://...`).
     Esa página dejará elegir "Continuar con Google" o email/contraseña.
  3. El usuario inicia sesión con normalidad. B2C termina redirigiendo el
     navegador a `authredirect://com.lfp.laligafantasy?code=...&state=...`.
     Ese esquema no existe de verdad (no hay ninguna app registrada para
     manejarlo), así que Chromium nunca "navega" -- pero SÍ emite la
     petición de red antes de rendirse, y Playwright puede interceptarla
     (`context.route`) para leer el `code` de la query string. Es
     exactamente el mismo truco que usa LaLigaApp con los eventos
     `will-navigate` / `will-redirect` / `did-fail-load` de Electron.
  4. Con el `code` capturado se intercambia por tokens en POST a
     /oauth2/v2.0/token (grant_type=authorization_code + code_verifier).
  5. Los tokens (access_token / id_token / refresh_token / ...) se guardan
     en JSON local (laliga_tokens.json, junto a este script). El
     refresh_token permite renovar el access_token sin volver a abrir
     ningún navegador -- ver refresh_tokens() / get_valid_access_token().

Requisitos:
    pip install -r requirements.txt
    playwright install chromium      # solo hace falta una vez

Uso (PowerShell):
    python laliga_auth.py            # login interactivo, guarda tokens
    python laliga_auth.py --refresh  # solo refresca con el refresh_token guardado (sin navegador)
    python laliga_auth.py --show     # muestra el token actual (truncado) y su caducidad

Desde otro script del scraper:
    from laliga_auth import get_valid_access_token
    token = get_valid_access_token()   # login/refresca automáticamente si hace falta
"""

import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import time
from datetime import datetime
from urllib.parse import urlencode, urlparse, parse_qs

import requests
from playwright.sync_api import sync_playwright

# --- Configuración del tenant B2C de LaLiga (idéntica a authService.js) ----

TOKEN_ENDPOINT = "https://login.laliga.es/laligadspprob2c.onmicrosoft.com/oauth2/v2.0/token"
AUTHORIZE_ENDPOINT = "https://login.laliga.es/laligadspprob2c.onmicrosoft.com/oauth2/v2.0/authorize"
SIGNIN_POLICY = "B2C_1A_5ULAIP_PARAMETRIZED_SIGNIN"

# Client "nativo" (el mismo que LaLigaApp usa por defecto para el login
# interactivo): es el único con el redirect_uri de esquema personalizado
# registrado. El client_id del login web normal (6457fa17-...) NO sirve
# aquí -- solo acepta miliga.laliga.com como redirect_uri.
OAUTH_CLIENT_ID = "af88bcff-1157-40a0-b579-030728aacf0b"

REDIRECT_URI = "authredirect://com.lfp.laligafantasy"
SCOPE = "openid offline_access"

TOKENS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "laliga_tokens.json")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# API real de LaLiga Fantasy (la misma que usa test_laliga_api.py), solo para
# verificar que el token funciona de verdad, no solo que el canje respondio 200.
FANTASY_API_BASE = "https://fantasy-api.llt-services.com"


# --- PKCE (RFC 7636) --------------------------------------------------------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_verifier() -> str:
    """32 bytes aleatorios -> 43 caracteres base64url (dentro del rango 43-128)."""
    return _b64url(secrets.token_bytes(32))


def challenge_from_verifier(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return _b64url(digest)


def random_state() -> str:
    return _b64url(secrets.token_bytes(16))


def build_authorize_url(redirect_uri: str, code_challenge: str, state: str) -> str:
    # Deliberadamente SIN prompt=login: esta política B2C a veces la rechaza,
    # y omitirla permite completar por SSO si ya hay sesión (igual que la app).
    params = {
        "p": SIGNIN_POLICY,
        "client_id": OAUTH_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "nonce": state,
    }
    return f"{AUTHORIZE_ENDPOINT}?{urlencode(params)}"


# --- Paso 1: login interactivo con Playwright -------------------------------

def run_browser_login(timeout_seconds: int = 180):
    """
    Abre una ventana de Chromium real en la pantalla de login de LaLiga
    (con opción de Google) e intercepta la redirección final para capturar
    el authorization code. Devuelve (code, verifier, redirect_uri).
    """
    verifier = generate_verifier()
    challenge = challenge_from_verifier(verifier)
    state = random_state()
    authorize_url = build_authorize_url(REDIRECT_URI, challenge, state)

    captured = {}

    def on_redirect(route):
        qs = parse_qs(urlparse(route.request.url).query)
        captured["code"] = qs.get("code", [None])[0]
        captured["state"] = qs.get("state", [None])[0]
        captured["error"] = qs.get("error", [None])[0]
        captured["error_description"] = qs.get("error_description", [None])[0]
        # El esquema no existe de verdad -- no hay nada que servir, solo
        # devolvemos una página de cortesía para que el usuario sepa que
        # puede cerrar la ventana (igual que oauth-callback.html).
        try:
            route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=(
                    "<html><body style='font-family:sans-serif;text-align:center;"
                    "padding-top:80px;background:#059669;color:white'>"
                    "<h2>Login completado</h2>"
                    "<p>Ya puedes cerrar esta ventana.</p></body></html>"
                ),
            )
        except Exception:
            pass

    print("Abriendo ventana de login de LaLiga (incluye 'Continuar con Google')...")
    print("Inicia sesion con normalidad -- la ventana se cerrara sola al terminar.\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=["--disable-dev-shm-usage"])
        context = browser.new_context(user_agent=UA)
        # Se registra a nivel de contexto para que también cubra cualquier
        # pestaña/popup nueva que se abra durante el login (p.ej. si el flujo
        # de Google usa una ventana intermedia).
        context.route(f"{REDIRECT_URI}**", on_redirect)

        page = context.new_page()
        page.goto(authorize_url, timeout=45000)

        deadline = time.time() + timeout_seconds
        while not captured and time.time() < deadline:
            page.wait_for_timeout(500)

        if captured:
            page.wait_for_timeout(1200)  # deja ver el mensaje de cortesía un instante

        browser.close()

    if not captured:
        raise TimeoutError(
            f"Se agoto el tiempo de espera ({timeout_seconds}s) sin recibir respuesta de LaLiga."
        )

    if captured.get("error"):
        raise RuntimeError(
            f"LaLiga devolvio un error OAuth: {captured['error']} - {captured.get('error_description')}"
        )

    if captured.get("state") != state:
        raise RuntimeError("El 'state' devuelto no coincide con el enviado (posible CSRF).")

    code = captured.get("code")
    if not code:
        raise RuntimeError("No se recibio authorization code en la redireccion.")

    return code, verifier, REDIRECT_URI


# --- Paso 2: canje de code -> tokens, y refresco ----------------------------

def exchange_code_for_tokens(code: str, verifier: str, redirect_uri: str) -> dict:
    token_url = f"{TOKEN_ENDPOINT}?p={SIGNIN_POLICY}"
    data = {
        "grant_type": "authorization_code",
        "client_id": OAUTH_CLIENT_ID,
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
        "scope": SCOPE,
    }
    r = requests.post(
        token_url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA},
        timeout=30,
    )
    try:
        result = r.json()
    except ValueError:
        result = {}

    if not r.ok:
        raise RuntimeError(
            f"Fallo al canjear el code por tokens ({r.status_code}): "
            f"{result.get('error_description') or result.get('error') or r.text[:300]}"
        )
    if not result.get("access_token") and not result.get("id_token"):
        raise RuntimeError("La respuesta de token no contiene access_token ni id_token.")

    result["client_id"] = OAUTH_CLIENT_ID
    return result


def refresh_tokens(refresh_token: str, client_id: str = None) -> dict:
    token_url = f"{TOKEN_ENDPOINT}?p={SIGNIN_POLICY}"
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id or OAUTH_CLIENT_ID,
        "scope": SCOPE,
    }
    r = requests.post(
        token_url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA},
        timeout=30,
    )
    if not r.ok:
        if r.status_code in (400, 401):
            raise RuntimeError(
                "invalid_grant: el refresh_token ha caducado o fue revocado -- "
                "hace falta volver a hacer login interactivo (python laliga_auth.py)."
            )
        raise RuntimeError(f"Fallo al refrescar el token ({r.status_code}): {r.text[:300]}")

    result = r.json()
    if not result.get("id_token"):
        raise RuntimeError("La respuesta de refresh no contiene id_token.")

    result["client_id"] = client_id or OAUTH_CLIENT_ID
    return result


# --- Persistencia local ------------------------------------------------------

def _calculate_expires_on(token_response: dict) -> int:
    if token_response.get("expires_on"):
        return int(token_response["expires_on"])
    if token_response.get("id_token_expires_in"):
        return int(time.time()) + int(token_response["id_token_expires_in"])
    if token_response.get("expires_in"):
        return int(time.time()) + int(token_response["expires_in"])
    return int(time.time()) + 86400


def save_tokens(token_response: dict, path: str = TOKENS_PATH) -> dict:
    payload = {
        # La API de LaLiga acepta el id_token como bearer cuando no hay
        # access_token propio (mismo fallback que buildLoginPayload en la app).
        "access_token": token_response.get("access_token") or token_response.get("id_token"),
        "id_token": token_response.get("id_token"),
        "refresh_token": token_response.get("refresh_token"),
        "token_type": token_response.get("token_type", "Bearer"),
        "client_id": token_response.get("client_id", OAUTH_CLIENT_ID),
        "expires_on": _calculate_expires_on(token_response),
        "saved_at": int(time.time()),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    try:
        os.chmod(path, 0o600)  # no es fiable en Windows, pero no falla si no aplica
    except OSError:
        pass
    return payload


def load_tokens(path: str = TOKENS_PATH):
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _is_expired(tokens: dict, buffer_seconds: int = 300) -> bool:
    if not tokens or not tokens.get("expires_on"):
        return True
    return (tokens["expires_on"] - time.time()) < buffer_seconds


# --- Punto de entrada para el resto del scraper ------------------------------

def get_valid_access_token(auto_login: bool = True) -> str:
    """
    Devuelve un access_token utilizable YA MISMO, refrescando o (si hace
    falta y auto_login=True) abriendo el navegador para un login interactivo.
    """
    tokens = load_tokens()

    if tokens and not _is_expired(tokens):
        return tokens["access_token"]

    if tokens and tokens.get("refresh_token"):
        try:
            print("Token caducado (o a punto de caducar) -- refrescando...")
            fresh = refresh_tokens(tokens["refresh_token"], tokens.get("client_id"))
            saved = save_tokens(fresh)
            return saved["access_token"]
        except Exception as e:
            print(f"No se pudo refrescar automaticamente: {e}")

    if not auto_login:
        raise RuntimeError("No hay un token valido guardado y auto_login=False.")

    print("Hace falta un login interactivo...")
    code, verifier, redirect_uri = run_browser_login()
    tokens = exchange_code_for_tokens(code, verifier, redirect_uri)
    saved = save_tokens(tokens)
    return saved["access_token"]


# --- Metodo manual (fallback cuando "Continuar con Google" no es viable) ---
#
# Google bloquea deliberadamente el login OAuth desde navegadores controlados
# por automatizacion (Playwright/Selenium/Puppeteer) como medida antiphishing
# ("This browser or app may not be secure"). No tiene sentido intentar
# esquivarlo -- para el caso de cuentas que SOLO usan "Continuar con Google"
# (sin email/contraseña propios de LaLiga), la propia LaLigaApp cae a este
# mismo metodo manual: inicias sesion en tu navegador normal de siempre (NO
# automatizado, asi que Google no lo bloquea) y capturas la respuesta de
# tokens desde las DevTools.
#
# Pasos:
#   1. Abre una pestaña nueva en tu navegador habitual (Chrome/Edge/Firefox)
#      y ve a: https://miliga.laliga.com/  -- pero NO inicies sesion todavia.
#   2. Abre las DevTools (F12) -> pestaña "Network"/"Red" -> activa el filtro
#      "Fetch/XHR" y marca "Preserve log"/"Conservar registro".
#   3. Ahora si, inicia sesion con normalidad (incluido "Continuar con
#      Google").
#   4. En el panel Network, filtra por: token?p=B2C_1A_5ULAIP_PARAMETRIZED_SIGNIN
#      Deberia salir una unica peticion a login.laliga.es. Ábrela y ve a la
#      pestaña "Response"/"Respuesta".
#   5. Copia TODO el JSON de la respuesta (desde la primera "{" hasta la
#      ultima "}") y pegalo en un fichero de texto, p.ej. paste_token.txt,
#      en esta misma carpeta.
#   6. Ejecuta:  python laliga_auth.py --paste-file paste_token.txt

def import_manual_tokens(raw_text: str) -> dict:
    """
    Convierte el contenido pegado a mano (JSON completo de la respuesta de
    token, o un access_token suelto) en el mismo formato que produce
    exchange_code_for_tokens(), listo para save_tokens().
    """
    raw_text = raw_text.strip()
    if raw_text.startswith("Bearer "):
        raw_text = raw_text[len("Bearer "):].strip()

    if not raw_text.startswith("{"):
        # Solo un token suelto (sin el JSON alrededor).
        if not raw_text:
            raise ValueError("El fichero esta vacio.")
        return {"access_token": raw_text, "token_type": "Bearer"}

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"El contenido no es JSON valido ({e}). "
            "Asegurate de copiar desde la primera '{' hasta la ultima '}'."
        )

    if not parsed.get("access_token") and not parsed.get("id_token"):
        raise ValueError("El JSON no contiene 'access_token' ni 'id_token'.")

    parsed.setdefault("client_id", OAUTH_CLIENT_ID)
    return parsed


# --- Verificacion contra la API real -----------------------------------

def verify_access_token(access_token: str) -> dict:
    """
    Llama a GET /api/v4/user/me con el token para confirmar que la API
    real de LaLiga Fantasy lo acepta (no solo que el canje de OAuth respondio
    200). Lanza una excepcion si la API lo rechaza.
    """
    r = requests.get(
        f"{FANTASY_API_BASE}/api/v4/user/me?x-lang=es",
        headers={
            "Authorization": f"Bearer {access_token}",
            "x-lang": "es",
            "x-app": "2",
            "User-Agent": UA,
        },
        timeout=15,
    )
    if not r.ok:
        raise RuntimeError(f"La API de LaLiga Fantasy rechazo el token ({r.status_code}): {r.text[:300]}")
    return r.json()


# --- CLI ----------------------------------------------------------------

def _print_token_summary(tokens: dict):
    tok = tokens.get("access_token") or ""
    exp = datetime.fromtimestamp(tokens.get("expires_on", 0))
    estado = "EXPIRADO" if _is_expired(tokens) else "valido"
    print(f"access_token: {tok[:12]}...{tok[-6:]} ({len(tok)} chars)")
    print(f"Caduca: {exp} ({estado})")


def main():
    parser = argparse.ArgumentParser(
        description="Login OAuth (Google/B2C) contra LaLiga Fantasy Oficial y guardado del token."
    )
    parser.add_argument("--refresh", action="store_true", help="Solo refresca el token guardado (sin abrir navegador).")
    parser.add_argument("--show", action="store_true", help="Muestra el token actual (truncado) y su caducidad.")
    parser.add_argument("--verify", action="store_true", help="Solo comprueba contra la API real que el token guardado funciona.")
    parser.add_argument(
        "--paste-file",
        metavar="RUTA",
        help="Importa un token copiado a mano desde las DevTools (ver instrucciones junto a import_manual_tokens en el codigo).",
    )
    args = parser.parse_args()

    if args.paste_file:
        try:
            with open(args.paste_file, "r", encoding="utf-8") as f:
                raw = f.read()
            tokens = import_manual_tokens(raw)
        except (OSError, ValueError) as e:
            print(f"No se pudo importar el token: {e}")
            sys.exit(1)
        saved = save_tokens(tokens)
        print(f"Token importado y guardado en {TOKENS_PATH}")
        _print_token_summary(saved)
        try:
            me = verify_access_token(saved["access_token"])
            print(f"Verificado contra la API real: usuario {me.get('managerName') or me.get('username') or me.get('id')}")
        except Exception as e:
            print(f"AVISO: el token se guardo pero la verificacion contra la API fallo: {e}")
        return

    if args.show:
        tokens = load_tokens()
        if not tokens:
            print(f"No hay tokens guardados todavia en {TOKENS_PATH}.")
            print("Ejecuta 'python laliga_auth.py' para hacer login.")
            return
        _print_token_summary(tokens)
        return

    if args.verify:
        tokens = load_tokens()
        if not tokens:
            print(f"No hay tokens guardados todavia en {TOKENS_PATH}.")
            print("Ejecuta 'python laliga_auth.py' para hacer login.")
            sys.exit(1)
        me = verify_access_token(tokens["access_token"])
        print("El token es valido. Respuesta de /api/v4/user/me:")
        print(json.dumps(me, indent=2, ensure_ascii=False)[:800])
        return

    if args.refresh:
        tokens = load_tokens()
        if not tokens or not tokens.get("refresh_token"):
            print("No hay refresh_token guardado -- ejecuta el login interactivo primero (sin --refresh).")
            sys.exit(1)
        fresh = refresh_tokens(tokens["refresh_token"], tokens.get("client_id"))
        saved = save_tokens(fresh)
        print(f"Token refrescado y guardado en {TOKENS_PATH}")
        _print_token_summary(saved)
        me = verify_access_token(saved["access_token"])
        print(f"Verificado contra la API: usuario {me.get('managerName') or me.get('username') or me.get('id')}")
        return

    token = get_valid_access_token()
    print(f"\nListo. Tokens guardados en: {TOKENS_PATH}")
    print(f"access_token: {token[:12]}...{token[-6:]}")

    try:
        me = verify_access_token(token)
        print(f"Verificado contra la API real: usuario {me.get('managerName') or me.get('username') or me.get('id')}")
    except Exception as e:
        print(f"AVISO: el token se guardo pero la verificacion contra la API fallo: {e}")


if __name__ == "__main__":
    main()
