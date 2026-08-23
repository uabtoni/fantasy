-- Añade la columna `touched`: jugadores marcados por el sitio de origen
-- como "tocado o sale de lesión, pero disponible" (icono disponible_box_min).
-- No es una incidencia (no es Lesionado ni Duda) -- se guarda aparte para
-- poder mostrarla con su propio color en el frontend sin contaminar el
-- campo `status`. Ver scraper/main.py, función fetch_all_status_maps.
--
-- Ejecutar manualmente en el SQL Editor de Supabase ANTES de desplegar la
-- versión de scraper/main.py que empieza a mandar la clave "touched" en
-- el upsert de jugadores -- si la columna no existe todavía, ese upsert
-- fallará y abortará también el guardado del histórico de precios de esa
-- misma ejecución.

ALTER TABLE public.players ADD COLUMN IF NOT EXISTS touched BOOLEAN NOT NULL DEFAULT false;
