CREATE TABLE IF NOT EXISTS contacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_uuid UUID NOT NULL
        REFERENCES users(uuid)
        ON DELETE CASCADE,
    name VARCHAR(100) NOT NULL,
    phone_number VARCHAR(32) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT contacts_user_phone_unique UNIQUE (user_uuid, phone_number)
);

CREATE INDEX IF NOT EXISTS idx_contacts_user_uuid
ON contacts(user_uuid);

CREATE INDEX IF NOT EXISTS idx_contacts_user_phone
ON contacts(user_uuid, phone_number);

CREATE OR REPLACE FUNCTION update_contacts_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER set_contacts_updated_at
BEFORE UPDATE ON contacts
FOR EACH ROW
EXECUTE FUNCTION update_contacts_updated_at();
