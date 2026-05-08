CREATE TABLE IF NOT EXISTS push_notification (
    endpoint TEXT PRIMARY KEY,
    userid UUID,
    expiration_time TIMESTAMP,
    p256dh TEXT,
    auth TEXT,
    app TEXT NOT NULL,                     -- identifies which app registered this token
    platform TEXT,                         -- 'fcm' | 'apns' | 'webpush'
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    CONSTRAINT fk_user
        FOREIGN KEY(userid) 
        REFERENCES users(uuid)
        ON DELETE CASCADE
);

-- Trigger to update 'updated_at' timestamp on row modification
CREATE OR REPLACE FUNCTION update_push_notification_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER set_push_notification_updated_at
BEFORE UPDATE ON push_notification
FOR EACH ROW
EXECUTE FUNCTION update_push_notification_updated_at();
