-- Historial de actividad de la liga (fichajes, ventas, clausulas, blindajes,
-- ganancias por jornada...) y el dinero ESTIMADO de cada manager calculado
-- a partir de ese historial.
--
-- Fuente: scraper/sync_league_activity.py, via scraper/league_data.py
-- (get_league_activity / walk_all_activity / compute_manager_finances):
--   GET /api/v1/competition/1/leagues/{leagueId}/activity/{index}
--
-- IMPORTANTE sobre league_manager_finances.estimated_money: NO es un dato
-- oficial. La API solo expone el saldo real (GET /teams/{id}/money) para tu
-- propio equipo -- para el resto da 403 Forbidden. Es una reconstruccion a
-- partir del historial de transacciones asumiendo el mismo presupuesto
-- inicial para todos (ver compute_manager_finances en league_data.py).
--
-- Un estimated_money negativo NO es, por si solo, un error: LaLiga Fantasy
-- permite endeudarte pujando en el mercado (fichajes a la IA) hasta el 20%
-- del valor de tu plantilla (debt_limit = -20% * team_value) -- lo que no
-- se puede es pagar a OTRO MANAGER (oferta directa, cláusula) sin tener el
-- dinero. Si is_plausible = false, estimated_money quedo POR DEBAJO de ese
-- limite -- esa es la señal real de que el presupuesto inicial asumido no
-- encaja con ese manager en concreto.

CREATE TABLE IF NOT EXISTS public.league_activity (
    id TEXT PRIMARY KEY,              -- id del evento en la API oficial
    league_id TEXT NOT NULL,
    activity_type_id INTEGER,
    amount BIGINT,
    user1_id TEXT,
    user2_id TEXT,
    player_master_id TEXT,
    week_number INTEGER,
    created_at TIMESTAMP WITH TIME ZONE,
    raw JSONB DEFAULT '{}'::jsonb,    -- item completo tal cual lo devuelve la API
    synced_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now())
);

CREATE INDEX IF NOT EXISTS idx_league_activity_league_id ON public.league_activity(league_id);
CREATE INDEX IF NOT EXISTS idx_league_activity_created_at ON public.league_activity(created_at);

ALTER TABLE public.league_activity ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Permitir lectura publica del historial de la liga"
ON public.league_activity FOR SELECT USING (true);


CREATE TABLE IF NOT EXISTS public.league_manager_finances (
    league_id TEXT NOT NULL,
    manager_id TEXT NOT NULL,
    manager_name TEXT NOT NULL,
    team_id TEXT,
    team_value BIGINT,                -- valor de mercado de la plantilla (oficial, get_standing)
    total_spent BIGINT DEFAULT 0,     -- suma de gastos detectados en el historial
    total_income BIGINT DEFAULT 0,    -- suma de ingresos detectados en el historial
    estimated_money BIGINT,           -- ESTIMADO, no oficial -- ver aviso arriba
    debt_limit BIGINT,                -- suelo permitido (<=0): -20% * team_value
    starting_budget_assumed BIGINT,   -- presupuesto inicial asumido para el calculo
    is_plausible BOOLEAN DEFAULT true,       -- false si estimated_money quedo por debajo de debt_limit
    has_unverified_events BOOLEAN DEFAULT false, -- participo en algun tipo de evento (cláusula) cuya regla de dinero no está confirmada
    last_synced TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()),
    PRIMARY KEY (league_id, manager_id)
);

ALTER TABLE public.league_manager_finances ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Permitir lectura publica de las finanzas de la liga"
ON public.league_manager_finances FOR SELECT USING (true);

-- Inserción/actualización las hace sync_league_activity.py con la Service
-- Role Key (bypassa RLS), igual que el resto del scraper.
