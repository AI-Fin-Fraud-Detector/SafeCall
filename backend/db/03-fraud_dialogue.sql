CREATE TABLE IF NOT EXISTS fraud_conversations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_uuid UUID REFERENCES users(uuid) ON DELETE CASCADE,
    target_user_uuid UUID REFERENCES users(uuid) ON DELETE CASCADE,
    title TEXT,  -- optional, can auto-generate from first message
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS fraud_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID REFERENCES fraud_conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK(role IN ('system','user','assistant')), 
    content TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT NOW(),
    parent_message_id UUID REFERENCES fraud_messages(id) ON DELETE SET NULL, -- optional for regeneration
    version INT DEFAULT 1
);
