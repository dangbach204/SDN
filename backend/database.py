import os
import asyncpg
from typing import Optional
from dotenv import load_dotenv
from datetime import date, timedelta

load_dotenv()

DATABASE_URL: str = os.getenv("DATABASE_URL", "")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL chưa được đặt.\n"
        "Tạo file .env từ .env.example và điền connection string từ Neon."
    )

_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    return _pool


async def init_db():
    """Tạo schema local đồng nhất với Neon schema."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        await conn.execute("""
            DO $$ BEGIN
                CREATE TYPE alert_level AS ENUM ('low', 'medium', 'high');
            EXCEPTION WHEN duplicate_object THEN null; END $$;
        """)
        await conn.execute("""
            DO $$ BEGIN
                CREATE TYPE action_type_enum AS ENUM ('limit_bandwidth', 'block', 'reroute');
            EXCEPTION WHEN duplicate_object THEN null; END $$;
        """)
        await conn.execute("""
            DO $$ BEGIN
                CREATE TYPE status_enum AS ENUM ('pending', 'applied', 'dismissed');
            EXCEPTION WHEN duplicate_object THEN null; END $$;
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS switches (
                dpid BIGINT PRIMARY KEY,
                name TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ports (
                dpid BIGINT,
                port_no INTEGER,
                name TEXT,
                PRIMARY KEY (dpid, port_no),
                FOREIGN KEY (dpid) REFERENCES switches(dpid) ON DELETE CASCADE
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS port_stats (
                id BIGSERIAL,
                timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                dpid BIGINT NOT NULL,
                port_no INTEGER NOT NULL,
                rx_bytes BIGINT NOT NULL DEFAULT 0,
                tx_bytes BIGINT NOT NULL DEFAULT 0,
                speed_rx DOUBLE PRECISION NOT NULL DEFAULT 0,
                speed_tx DOUBLE PRECISION NOT NULL DEFAULT 0,
                PRIMARY KEY (id, timestamp)
            )
            PARTITION BY RANGE (timestamp)
        """)
        today = date.today()
        tomorrow = today + timedelta(days=1)
        partition_name = f"port_stats_{today:%Y_%m_%d}"
        await conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {partition_name}
            PARTITION OF port_stats
            FOR VALUES FROM ('{today.isoformat()}') TO ('{tomorrow.isoformat()}')
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS port_stats_default
            PARTITION OF port_stats DEFAULT
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_port_stats_time
                ON port_stats(timestamp DESC)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_port_stats_dpid_port_time
                ON port_stats(dpid, port_no, timestamp DESC)
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS flow_stats (
                id BIGSERIAL PRIMARY KEY,
                timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                dpid BIGINT NOT NULL,
                priority INTEGER NOT NULL,
                packets BIGINT NOT NULL,
                bytes BIGINT NOT NULL,
                duration_seconds BIGINT NOT NULL,
                match JSONB NOT NULL DEFAULT '{}'::jsonb
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_flow_stats_time
                ON flow_stats(timestamp DESC)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_flow_stats_dpid_time
                ON flow_stats(dpid, timestamp DESC)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_flow_match
                ON flow_stats USING GIN (match)
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS anomalies (
                id BIGSERIAL PRIMARY KEY,
                timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                dpid BIGINT NOT NULL,
                port_no INTEGER NOT NULL,
                metric TEXT NOT NULL,
                value DOUBLE PRECISION NOT NULL,
                threshold DOUBLE PRECISION,
                level alert_level NOT NULL,
                message TEXT NOT NULL,
                details JSONB NOT NULL DEFAULT '{}'::jsonb
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_anomalies_time
                ON anomalies(timestamp DESC)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_anomalies_dpid_port_time
                ON anomalies(dpid, port_no, timestamp DESC)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_anomalies_details
                ON anomalies USING GIN (details)
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS recommendations (
                id BIGSERIAL PRIMARY KEY,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                dpid BIGINT NOT NULL,
                port_no INTEGER NOT NULL,
                level alert_level NOT NULL,
                action_type action_type_enum NOT NULL,
                message TEXT NOT NULL,
                root_cause TEXT NOT NULL,
                actions_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                status status_enum NOT NULL DEFAULT 'pending',
                chosen_action TEXT,
                applied_at TIMESTAMPTZ
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_recommendations_created
                ON recommendations(created_at DESC)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_recommendations_status
                ON recommendations(status, created_at DESC)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_recommendations_dpid_port
                ON recommendations(dpid, port_no)
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_recommendations_actions
                ON recommendations USING GIN (actions_json)
        """)

        await conn.execute("""
            CREATE OR REPLACE FUNCTION cleanup_old_port_stats(days INT)
            RETURNS VOID AS $$
            BEGIN
                DELETE FROM port_stats
                WHERE timestamp < NOW() - (days || ' days')::INTERVAL;
            END;
            $$ LANGUAGE plpgsql;
        """)
    print("[DB] Schema Neon PostgreSQL sẵn sàng.")
