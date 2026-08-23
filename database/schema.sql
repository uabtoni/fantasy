-- Habilitar extensión para UUIDs si fuera necesario, aunque usaremos el ID del origen
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Tabla principal de jugadores
CREATE TABLE public.players (
    id TEXT PRIMARY KEY, -- Usaremos el ID de la web de origen
    name TEXT NOT NULL,
    team TEXT NOT NULL,
    position TEXT NOT NULL,
    price INTEGER DEFAULT 0,
    points INTEGER DEFAULT 0,
    status TEXT,
    touched BOOLEAN DEFAULT false, -- "tocado, pero disponible" (no es incidencia) -- ver database/migrations/001_add_touched_column.sql
    url TEXT,
    stats JSONB DEFAULT '{}'::jsonb, -- Para estadísticas extra flexibles
    last_updated TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now())
);

-- Tabla de histórico para ver evolución de precio y puntos
CREATE TABLE public.player_history (
    id SERIAL PRIMARY KEY,
    player_id TEXT REFERENCES public.players(id) ON DELETE CASCADE,
    price INTEGER NOT NULL,
    points INTEGER NOT NULL,
    recorded_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now())
);

-- Habilitar Row Level Security (RLS)
ALTER TABLE public.players ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.player_history ENABLE ROW LEVEL SECURITY;

-- Crear políticas para permitir lectura pública a cualquier usuario (incluido anónimo)
CREATE POLICY "Permitir lectura publica de jugadores" 
ON public.players FOR SELECT USING (true);

CREATE POLICY "Permitir lectura publica de historial" 
ON public.player_history FOR SELECT USING (true);

-- Nota: La inserción y actualización la hará el scraper usando la Service Role Key, 
-- que bypassa el RLS, por lo que no necesitamos crear políticas de INSERT/UPDATE públicas.