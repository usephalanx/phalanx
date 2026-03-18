-- Runs once on first Postgres container start
-- Enables required extensions

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "vector";       -- pgvector for semantic search
CREATE EXTENSION IF NOT EXISTS "pg_trgm";      -- trigram for text search fallback
CREATE EXTENSION IF NOT EXISTS "btree_gin";    -- GIN index support

-- Verify extensions loaded
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
        RAISE EXCEPTION 'pgvector extension not available. Use pgvector/pgvector:pg16 image.';
    END IF;
END $$;
