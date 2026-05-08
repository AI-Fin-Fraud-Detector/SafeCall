CREATE TABLE IF NOT EXISTS users (
    uuid            UUID PRIMARY KEY,
    name            VARCHAR(100) NOT NULL,
    email           VARCHAR(255) NOT NULL UNIQUE,
    phone_number    VARCHAR(20) NOT NULL UNIQUE,
    hashed_password TEXT NOT NULL,
    scam_detection  BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW()
);

CREATE OR REPLACE FUNCTION trim_name_field()
RETURNS TRIGGER AS $$
BEGIN
    NEW.name = TRIM(NEW.name);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trim_name_before_write
BEFORE INSERT OR UPDATE ON users
FOR EACH ROW
EXECUTE FUNCTION trim_name_field();

-- Insert dummy users
INSERT INTO users (uuid, name, email, phone_number, hashed_password, scam_detection)
VALUES
  ('11111111-1111-1111-1111-111111111111', 'Alice Cheng', 'user1@example.com', '0911000001', '$argon2id$v=19$m=65536,t=3,p=4$7J2TMuacU4rRGoPQmnNOiQ$Uli+5lyquOywXeKDNJjRy0WeYE8LsU9+EuMuO4acNLw', TRUE),
  ('22222222-2222-2222-2222-222222222222', 'Bob Smith', 'user2@example.com', '0911000002', '$argon2id$v=19$m=65536,t=3,p=4$QCgFoHQuxbi3ds55T6n1fg$BbrFvJN7S4X1g4/ZywXJOOsj/gVM4eYtBoZ/itrm0lo', TRUE),
  ('4ac10511-9f94-4bda-be08-cadc46018b8d', 'Test', 'test@example.com', '0911000003', '$argon2id$v=19$m=65536,t=3,p=4$k7J27v3/PyfkHKOUEkJobQ$9/GOcxoTiJ3+qFhcysRgyFOmz980CIoMXQ8hnFM/fdQ', FALSE)
ON CONFLICT (email) DO NOTHING;
