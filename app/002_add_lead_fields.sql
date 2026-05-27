-- migrations/002_add_lead_fields.sql
-- Добавление полей размера и стиля, а также уникального индекса для upsert

-- Добавить колонку size, если не существует
ALTER TABLE leads ADD COLUMN IF NOT EXISTS size TEXT;

-- Добавить колонку style, если не существует
ALTER TABLE leads ADD COLUMN IF NOT EXISTS style TEXT;

-- Создать уникальный индекс на (user_id, platform) для атомарного upsert
CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_user_platform ON leads (user_id, platform);