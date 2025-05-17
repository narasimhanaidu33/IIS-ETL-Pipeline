-- Time Dimension
-- dim_time (with proper constraints)
CREATE TABLE IF NOT EXISTS dim_time (
    time_id SERIAL PRIMARY KEY,
    timestamp TIMESTAMP NOT NULL UNIQUE,  -- Added UNIQUE constraint here
    hour INTEGER NOT NULL,
    time_period VARCHAR(10) NOT NULL,
    date DATE NOT NULL,
    month INTEGER NOT NULL,
    quarter INTEGER NOT NULL,
    year INTEGER NOT NULL
);


-- Enhanced Client Dimension with geolocation
CREATE TABLE IF NOT EXISTS dim_clients (
    client_id SERIAL PRIMARY KEY,
    ip_address TEXT NOT NULL UNIQUE,
    user_agent TEXT,
    browser VARCHAR(100),
    os VARCHAR(100),
    device_type VARCHAR(20),
    -- Geolocation fields
    country VARCHAR(100),
    state VARCHAR(100),
    city VARCHAR(100),
    zip_code VARCHAR(20),
    -- Indexes for geolocation queries
    CONSTRAINT idx_dim_clients_geo_fields UNIQUE (ip_address)
);

-- dim_requests (unchanged)
CREATE TABLE IF NOT EXISTS dim_requests (
    request_id SERIAL PRIMARY KEY,
    method VARCHAR(10) NOT NULL,
    uri_stem TEXT NOT NULL,
    uri_query TEXT,
    username TEXT,
    CONSTRAINT unique_request UNIQUE (method, uri_stem)
);

-- dim_responses (unchanged)
CREATE TABLE IF NOT EXISTS dim_responses (
    response_id SERIAL PRIMARY KEY,
    status_code INTEGER NOT NULL,
    substatus INTEGER,
    win32_status INTEGER,
    CONSTRAINT unique_response UNIQUE (status_code, substatus, win32_status)
);

-- fact_visits (with client reference only)
CREATE TABLE IF NOT EXISTS fact_visits (
    visit_id SERIAL PRIMARY KEY,
    timestamp TIMESTAMP NOT NULL,
    date TEXT,
    time TEXT,
    s_ip TEXT,
    cs_method TEXT,
    cs_uri_stem TEXT,
    cs_uri_query TEXT,
    s_port INTEGER,
    cs_username TEXT,
    c_ip TEXT NOT NULL,
    cs_user_agent TEXT,
    sc_status INTEGER NOT NULL,
    sc_substatus INTEGER,
    sc_win32_status INTEGER,
    time_taken INTEGER,
    hour INTEGER,
    time_period TEXT,
    ip_prefix TEXT,
    user_agent TEXT,
    is_error BOOLEAN,
    is_suspicious BOOLEAN,
    load_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    -- Foreign keys
    time_id INTEGER REFERENCES dim_time(time_id),
    client_id INTEGER REFERENCES dim_clients(client_id),
    request_id INTEGER REFERENCES dim_requests(request_id),
    response_id INTEGER REFERENCES dim_responses(response_id)
);

-- Add indexes for performance
CREATE INDEX IF NOT EXISTS idx_fact_visits_timestamp ON fact_visits(timestamp);
CREATE INDEX IF NOT EXISTS idx_fact_visits_c_ip ON fact_visits(c_ip);
CREATE INDEX IF NOT EXISTS idx_fact_visits_is_error ON fact_visits(is_error);
CREATE INDEX IF NOT EXISTS idx_dim_clients_ip ON dim_clients(ip_address);
CREATE INDEX IF NOT EXISTS idx_dim_requests_composite ON dim_requests(method, uri_stem);
-- Add geolocation indexes
CREATE INDEX IF NOT EXISTS idx_dim_clients_country ON dim_clients(country);
CREATE INDEX IF NOT EXISTS idx_dim_clients_state ON dim_clients(state);
CREATE INDEX IF NOT EXISTS idx_dim_clients_city ON dim_clients(city);

COMMIT;