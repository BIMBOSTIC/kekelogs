CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT UNIQUE NOT NULL,
    username TEXT,
    currency TEXT NOT NULL DEFAULT '₺',
    timezone TEXT NOT NULL DEFAULT 'Europe/Istanbul',
    tier TEXT NOT NULL DEFAULT 'free',
    onboarded INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS vehicles (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    plate_or_nickname TEXT NOT NULL,
    ownership TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS remittance_rules (
    id BIGSERIAL PRIMARY KEY,
    vehicle_id BIGINT NOT NULL REFERENCES vehicles(id),
    amount DOUBLE PRECISION NOT NULL,
    frequency TEXT NOT NULL DEFAULT 'DAILY',
    effective_from DATE NOT NULL,
    effective_to DATE,
    accrues_on_zero_days INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS passengers (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    display_name TEXT NOT NULL,
    note TEXT,
    first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lifetime_revenue DOUBLE PRECISION NOT NULL DEFAULT 0,
    trip_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS trips (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    vehicle_id BIGINT NOT NULL REFERENCES vehicles(id),
    amount DOUBLE PRECISION NOT NULL,
    destination TEXT,
    passenger_id BIGINT REFERENCES passengers(id),
    paid INTEGER NOT NULL DEFAULT 1,
    payment_method TEXT NOT NULL DEFAULT 'CASH',
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS expenses (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    vehicle_id BIGINT NOT NULL REFERENCES vehicles(id),
    type TEXT NOT NULL,
    amount DOUBLE PRECISION NOT NULL,
    note TEXT,
    litres DOUBLE PRECISION,
    odometer DOUBLE PRECISION,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS remittance_log (
    id BIGSERIAL PRIMARY KEY,
    vehicle_id BIGINT NOT NULL REFERENCES vehicles(id),
    amount DOUBLE PRECISION NOT NULL,
    paid_on DATE NOT NULL,
    status TEXT NOT NULL DEFAULT 'PAID',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS action_log (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    action_type TEXT NOT NULL,
    table_name TEXT NOT NULL,
    record_id BIGINT NOT NULL,
    snapshot TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_trips_user_date ON trips(user_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_expenses_user_date ON expenses(user_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_action_log_user ON action_log(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_passengers_user ON passengers(user_id);
