-- migrations/002_add_lead_fields.sql
-- Добавление полей размера и стиля, а также уникального индекса для upsert

-- Добавить колонку size, если не существует
ALTER TABLE leads ADD COLUMN IF NOT EXISTS size TEXT;

-- Добавить колонку style, если не существует
ALTER TABLE leads ADD COLUMN IF NOT EXISTS style TEXT;

-- Пересоздать индекс как UNIQUE — DROP нужен если существует неуникальный индекс с тем же именем
DROP INDEX IF EXISTS idx_leads_user_platform;
CREATE UNIQUE INDEX idx_leads_user_platform ON leads (user_id, platform);