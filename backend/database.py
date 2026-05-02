"""
database.py — Kết nối Neon PostgreSQL qua asyncpg

Chỉ giữ các bảng phục vụ chức năng mô tả trong description.md:
  - port_stats    : lưu stats băng thông từ Ryu
  - flow_stats    : lưu flow entries từ Ryu
  - anomalies     : lưu cảnh báo phát hiện bất thường
  - recommendations: lưu khuyến nghị từ DecisionEngine
  - latency_stats : lưu kết quả đo độ trễ
"""

import os
from typing import Optional

import asyncpg
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL: str = os.getenv("DATABASE_URL", "")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL chưa được đặt.\n"
        "Tạo file .env và điền connection string từ Neon."
    )

_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    return _pool


async def init_db():
    pool = await get_pool()
    async with pool.acquire() as conn:

        # Enum types
        await conn.execute("""
            DO $$ BEGIN
                CREATE TYPE alert_level AS ENUM
                    ('low', 'medium', 'high', 'warn', 'zscore');
            EXCEPTION WHEN duplicate_object THEN null; END $$;
        """)
        for val in ('warn', 'zscore'):
            await conn.execute(f"""
                DO $$ BEGIN
                    ALTER TYPE alert_level ADD VALUE IF NOT EXISTS '{val}';
                EXCEPTION WHEN others THEN null; END $$;
            """)

        await conn.execute("""
            DO $$ BEGIN
                CREATE TYPE action_type_enum AS ENUM
                    ('limit_bandwidth', 'block', 'monitor', 'investigate');
            EXCEPTION WHEN duplicate_object THEN null; END $$;
        """)
        for val in ('monitor', 'investigate'):
            await conn.execute(f"""
                DO $$ BEGIN
                    ALTER TYPE action_type_enum ADD VALUE IF NOT EXISTS '{val}';
                EXCEPTION WHEN others THEN null; END $$;
            """)

        await conn.execute("""
            DO $$ BEGIN
                CREATE TYPE status_enum AS ENUM ('pending', 'applied', 'dismissed');
            EXCEPTION WHEN duplicate_object THEN null; END $$;
        """)

        # port_stats
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS port_stats (
                id        BIGSERIAL PRIMARY KEY,
                timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                dpid      BIGINT NOT NULL,
                port_no   INTEGER NOT NULL,
                rx_bytes  BIGINT NOT NULL DEFAULT 0,
                tx_bytes  BIGINT NOT NULL DEFAULT 0,
                speed_rx  DOUBLE PRECISION NOT NULL DEFAULT 0,
                speed_tx  DOUBLE PRECISION NOT NULL DEFAULT 0
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_port_stats_time
                ON port_stats(timestamp DESC)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_port_stats_dpid_port_time
                ON port_stats(dpid, port_no, timestamp DESC)
        """)

        # flow_stats
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS flow_stats (
                id               BIGSERIAL PRIMARY KEY,
                timestamp        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                dpid             BIGINT NOT NULL,
                priority         INTEGER NOT NULL DEFAULT 0,
                packets          BIGINT NOT NULL DEFAULT 0,
                bytes            BIGINT NOT NULL DEFAULT 0,
                duration_seconds BIGINT NOT NULL DEFAULT 0,
                match            JSONB NOT NULL DEFAULT '{}'::jsonb
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

        # anomalies
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS anomalies (
                id        BIGSERIAL PRIMARY KEY,
                timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                dpid      BIGINT NOT NULL,
                port_no   INTEGER NOT NULL,
                metric    TEXT NOT NULL DEFAULT 'bandwidth',
                value     DOUBLE PRECISION NOT NULL,
                threshold DOUBLE PRECISION,
                level     alert_level NOT NULL,
                message   TEXT NOT NULL
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

        # recommendations
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS recommendations (
                id            BIGSERIAL PRIMARY KEY,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                dpid          BIGINT NOT NULL,
                port_no       INTEGER NOT NULL,
                level         alert_level NOT NULL,
                action_type   action_type_enum NOT NULL,
                message       TEXT NOT NULL,
                actions_json  JSONB NOT NULL DEFAULT '[]'::jsonb,
                status        status_enum NOT NULL DEFAULT 'pending',
                chosen_action TEXT,
                applied_at    TIMESTAMPTZ
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

        # latency_stats
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS latency_stats (
                id               BIGSERIAL PRIMARY KEY,
                timestamp        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                src_ip           TEXT NOT NULL,
                dst_ip           TEXT NOT NULL,
                rtt_min_ms       DOUBLE PRECISION,
                rtt_avg_ms       DOUBLE PRECISION,
                rtt_max_ms       DOUBLE PRECISION,
                packet_loss_pct  DOUBLE PRECISION NOT NULL DEFAULT 0,
                probe_count      INTEGER NOT NULL DEFAULT 3
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_latency_time
                ON latency_stats(timestamp DESC)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_latency_src_dst
                ON latency_stats(src_ip, dst_ip, timestamp DESC)
        """)

        # Cleanup function
        await conn.execute("""
            CREATE OR REPLACE FUNCTION cleanup_old_data(days INT)
            RETURNS VOID AS $$
            BEGIN
                DELETE FROM port_stats    WHERE timestamp < NOW() - (days || ' days')::INTERVAL;
                DELETE FROM flow_stats    WHERE timestamp < NOW() - (days || ' days')::INTERVAL;
                DELETE FROM anomalies     WHERE timestamp < NOW() - (days || ' days')::INTERVAL;
                DELETE FROM latency_stats WHERE timestamp < NOW() - (days || ' days')::INTERVAL;
            END;
            $$ LANGUAGE plpgsql;
        """)

    print("[DB] Schema Neon PostgreSQL sẵn sàng.")