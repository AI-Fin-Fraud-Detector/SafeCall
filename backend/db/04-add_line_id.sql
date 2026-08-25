-- Migration: Add line_id column to users table
-- Date: 2026-08-25
-- Description: Add optional LINE ID field for user social integration

-- Add line_id column (nullable, safe for existing rows)
ALTER TABLE users
ADD COLUMN IF NOT EXISTS line_id VARCHAR(100);

-- Verify migration
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'users' AND column_name = 'line_id'
    ) THEN
        RAISE NOTICE 'Migration successful: line_id column added to users table';
    END IF;
END $$;
