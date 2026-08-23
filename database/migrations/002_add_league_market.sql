-- Mercado de TU liga privada de LaLiga Fantasy Oficial (distinto de `players`,
-- que es el mercado PÚBLICO de toda LaLiga scrapeado de futbolfantasy.com).
-- Cada fila es un jugador puesto a la venta ahora mismo en tu liga, con el
-- precio de venta concreto de esa oferta, pujas y fecha de expiración.
--
-- Poblado por scraper/sync_league_market.py, que usa el token de
-- scraper/laliga_auth.py contra la API oficial:
--   GET /api/v1/competition/1/league/{leagueId}/market
--
-- Ejecutar manualmente en el SQL Editor de Supabase antes de correr
-- sync_league_market.py por primera vez.

CREATE TABLE IF NOT EXISTS public.league_market (
    id TEXT PRIMARY KEY,              -- id de la oferta de mercado (no del jugador -- un mismo jugador puede volver a salir con otro id)
    league_id TEXT NOT NULL,          -- por si en el futuro se sigue más de una liga
    player_id TEXT NOT NULL,
    player_name TEXT NOT NULL,
    position TEXT,
    team_id INTEGER,                  -- id del equipo real de LaLiga del jugador
    image_url TEXT,
    market_value BIGINT,              -- valor de mercado oficial del jugador
    sale_price BIGINT NOT NULL,       -- precio concreto de esta oferta
    status TEXT,                      -- on_sale, etc.
    number_of_bids INTEGER DEFAULT 0,
    expiration_date TIMESTAMP WITH TIME ZONE,
    points INTEGER,
    average_points NUMERIC,
    raw JSONB DEFAULT '{}'::jsonb,    -- respuesta completa de la API por si hace falta algo mas adelante
    last_synced TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now())
);

CREATE INDEX IF NOT EXISTS idx_league_market_league_id ON public.league_market(league_id);

ALTER TABLE public.league_market ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Permitir lectura publica del mercado de liga"
ON public.league_market FOR SELECT USING (true);

-- Inserción/actualización/borrado los hace sync_league_market.py con la
-- Service Role Key (bypassa RLS), igual que el resto del scraper.
