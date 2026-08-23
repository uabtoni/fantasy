-- Quién tiene fichado a cada jugador en TU liga privada (las plantillas
-- completas de los managers), para poder mostrar "Ferran87_ lo tiene" al
-- lado de cada jugador en la web -- tanto en el mercado público (`players`)
-- como en el mercado de tu liga (`league_market`).
--
-- Poblado por scraper/sync_league_roster.py (misma fuente que
-- scraper/find_player_owner.py -- ver scraper/league_data.py):
--   GET /api/v1/competition/1/leagues/{leagueId}/standing
--   GET /api/v1/competition/1/leagues/{leagueId}/teams/{teamId}
--
-- Ejecutar manualmente en el SQL Editor de Supabase antes de correr
-- sync_league_roster.py por primera vez.

CREATE TABLE IF NOT EXISTS public.league_roster (
    id TEXT PRIMARY KEY,              -- playerTeamId (ficha concreta de ese jugador en ese equipo)
    league_id TEXT NOT NULL,
    player_id TEXT NOT NULL,          -- id oficial del jugador (cruza exacto con league_market.player_id)
    player_name TEXT NOT NULL,
    nickname TEXT,
    position TEXT,
    real_team TEXT,                   -- equipo real de LaLiga del jugador
    market_value BIGINT,
    team_id TEXT NOT NULL,            -- id del equipo Fantasy que lo tiene
    manager_id TEXT,
    manager_name TEXT NOT NULL,       -- quién lo tiene, en texto legible
    buyout_clause BIGINT,
    clause_locked_until TIMESTAMP WITH TIME ZONE,
    shielded BOOLEAN DEFAULT false,
    last_synced TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now())
);

CREATE INDEX IF NOT EXISTS idx_league_roster_league_id ON public.league_roster(league_id);
CREATE INDEX IF NOT EXISTS idx_league_roster_player_id ON public.league_roster(player_id);

ALTER TABLE public.league_roster ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Permitir lectura publica de la plantilla de la liga"
ON public.league_roster FOR SELECT USING (true);

-- Inserción/actualización/borrado los hace sync_league_roster.py con la
-- Service Role Key (bypassa RLS), igual que el resto del scraper.
