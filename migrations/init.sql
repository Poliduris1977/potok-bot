-- migrations/init.sql
-- Выполняется автоматически при первом старте PostgreSQL-контейнера.

CREATE TABLE IF NOT EXISTS leads (
    id              SERIAL PRIMARY KEY,
    platform        VARCHAR(20)  NOT NULL,          -- 'telegram' | 'max'
    user_id         VARCHAR(100) NOT NULL,
    username        VARCHAR(100),
    name            VARCHAR(200),
    phone           VARCHAR(50),
    phone_channel   VARCHAR(50),                    -- Zadarma номер для карточки
    address         TEXT,
    budget          VARCHAR(100),
    deadline        VARCHAR(100),
    product         VARCHAR(200),
    status          VARCHAR(50)  DEFAULT 'new',     -- 'new' | 'hot' | 'booked' | 'transferred'
    appointment_dt  TIMESTAMP,
    notes           TEXT,
    created_at      TIMESTAMP    DEFAULT NOW(),
    updated_at      TIMESTAMP    DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS dialogs (
    id               SERIAL PRIMARY KEY,
    lead_id          INTEGER REFERENCES leads(id),
    platform         VARCHAR(20)  NOT NULL,
    user_id          VARCHAR(100) NOT NULL,
    started_at       TIMESTAMP    DEFAULT NOW(),
    last_message_at  TIMESTAMP,
    message_count    INTEGER      DEFAULT 0,
    transferred      BOOLEAN      DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS messages (
    id         SERIAL PRIMARY KEY,
    dialog_id  INTEGER REFERENCES dialogs(id),
    role       VARCHAR(20) NOT NULL,                -- 'user' | 'assistant'
    content    TEXT        NOT NULL,
    created_at TIMESTAMP   DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS appointments (
    id           SERIAL PRIMARY KEY,
    lead_id      INTEGER REFERENCES leads(id),
    scheduled_at TIMESTAMP,
    address      TEXT,
    confirmed    BOOLEAN   DEFAULT FALSE,
    created_at   TIMESTAMP DEFAULT NOW()
);

-- Уникальный индекс для атомарного upsert по (user_id, platform)
CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_user_platform ON leads(user_id, platform);

-- Индексы для быстрого поиска диалогов и сообщений
CREATE INDEX IF NOT EXISTS idx_dialogs_user_platform ON dialogs(user_id, platform);
CREATE INDEX IF NOT EXISTS idx_messages_dialog       ON messages(dialog_id);