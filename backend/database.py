"""
database.py — Kết nối Neon PostgreSQL qua asyncpg

Thay đổi so với bản cũ:
  1. Bỏ partition port_stats — không cần thiết ở quy mô lab/demo,
     gây lỗi nếu không tạo partition mỗi ngày
  2. Bỏ bảng switches, ports — không dùng ở đâu
  3. Bỏ bảng action_verifications — kết quả verify lưu trong control_actions
  4. Sửa tên cột flow_stats: match_str→match (jsonb), duration→duration_seconds
  5. Sửa enum alert_level: thêm 'high','warn','zscore' để khớp với
     những gì monitor.py và decision_engine.py thực sự gửi lên
  6. Bỏ cột details trong anomalies — không bao giờ được populate
"""

import os
from datetime import date, timedelta
from typing import Optional

import asyncpg
from dotenv import load_dotenv

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
    """Tạo schema trên Neon PostgreSQL nếu chưa tồn tại."""
    pool = await get_pool()
    async with pool.acquire() as conn:

        # ── Enum types ────────────────────────────────────────────────────────
        # alert_level: khớp với giá trị thực tế từ monitor.py và decision_engine.py
        await conn.execute("""
            DO $$ BEGIN
                CREATE TYPE alert_level AS ENUM ('low', 'medium', 'high', 'warn', 'zscore');
            EXCEPTION WHEN duplicate_object THEN null; END $$;
        """)
        # Thêm giá trị mới vào enum nếu đang upgrade từ schema cũ
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

        # ── port_stats ────────────────────────────────────────────────────────
        # Bỏ PARTITION — đơn giản hóa, đủ cho quy mô lab/demo.
        # Nếu cần partition sau này: thêm PARTITION BY RANGE(timestamp)
        # và job tạo partition hàng ngày.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS port_stats (
                id         BIGSERIAL PRIMARY KEY,
                timestamp  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                dpid       BIGINT NOT NULL,
                port_no    INTEGER NOT NULL,
                rx_bytes   BIGINT NOT NULL DEFAULT 0,
                tx_bytes   BIGINT NOT NULL DEFAULT 0,
                speed_rx   DOUBLE PRECISION NOT NULL DEFAULT 0,
                speed_tx   DOUBLE PRECISION NOT NULL DEFAULT 0
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

        # ── flow_stats ────────────────────────────────────────────────────────
        # Cột match kiểu JSONB + GIN index để closed_loop.py query ->> hoạt động.
        # duration_seconds (không phải duration hay match_str như bản cũ).
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

        # ── anomalies ─────────────────────────────────────────────────────────
        # Bỏ cột details (không bao giờ được populate).
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

        # ── recommendations ───────────────────────────────────────────────────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS recommendations (
                id            BIGSERIAL PRIMARY KEY,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                dpid          BIGINT NOT NULL,
                port_no       INTEGER NOT NULL,
                level         alert_level NOT NULL,
                action_type   action_type_enum NOT NULL,
                message       TEXT NOT NULL,
                root_cause    TEXT NOT NULL DEFAULT '',
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

        # ── closed-loop tables ────────────────────────────────────────────────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS control_cycles (
                id              BIGSERIAL PRIMARY KEY,
                started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                ended_at        TIMESTAMPTZ,
                status          TEXT NOT NULL DEFAULT 'running',
                congested_links INTEGER NOT NULL DEFAULT 0,
                anomalies       INTEGER NOT NULL DEFAULT 0,
                actions_planned INTEGER NOT NULL DEFAULT 0,
                actions_applied INTEGER NOT NULL DEFAULT 0,
                metadata        JSONB NOT NULL DEFAULT '{}'::jsonb
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_control_cycles_started
                ON control_cycles(started_at DESC)
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS control_actions (
                id                   BIGSERIAL PRIMARY KEY,
                cycle_id             BIGINT REFERENCES control_cycles(id) ON DELETE SET NULL,
                dpid                 BIGINT NOT NULL,
                port_no              INTEGER NOT NULL,
                strategy             TEXT NOT NULL DEFAULT '',
                action_type          TEXT NOT NULL,
                action_param         DOUBLE PRECISION NOT NULL DEFAULT 0,
                confidence           DOUBLE PRECISION NOT NULL DEFAULT 0,
                score                DOUBLE PRECISION,
                decision             TEXT NOT NULL DEFAULT 'pending',
                execution_ok         BOOLEAN NOT NULL DEFAULT FALSE,
                verification_ok      BOOLEAN NOT NULL DEFAULT FALSE,
                rollback_performed   BOOLEAN NOT NULL DEFAULT FALSE,
                execution_message    TEXT NOT NULL DEFAULT '',
                verification_message TEXT NOT NULL DEFAULT '',
                rollback_message     TEXT,
                before_kpi           JSONB NOT NULL DEFAULT '{}'::jsonb,
                after_kpi            JSONB,
                metadata             JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_control_actions_created
                ON control_actions(created_at DESC)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_control_actions_port
                ON control_actions(dpid, port_no, created_at DESC)
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS active_control_actions (
                id                 BIGSERIAL PRIMARY KEY,
                dpid               BIGINT NOT NULL,
                port_no            INTEGER NOT NULL,
                strategy           TEXT NOT NULL DEFAULT '',
                action_type        TEXT NOT NULL,
                action_param       DOUBLE PRECISION NOT NULL DEFAULT 0,
                confidence         DOUBLE PRECISION NOT NULL DEFAULT 0,
                state              TEXT NOT NULL DEFAULT 'active',
                cooldown_until     TIMESTAMPTZ,
                evaluate_after     TIMESTAMPTZ,
                stable_cycles      INTEGER NOT NULL DEFAULT 0,
                baseline_kpi       JSONB NOT NULL DEFAULT '{}'::jsonb,
                latest_kpi         JSONB,
                metadata           JSONB NOT NULL DEFAULT '{}'::jsonb,
                control_action_id  BIGINT REFERENCES control_actions(id) ON DELETE SET NULL,
                created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (dpid, port_no)
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_active_control_state
                ON active_control_actions(state, evaluate_after)
        """)

        # ── Cleanup function ──────────────────────────────────────────────────
        await conn.execute("""
            CREATE OR REPLACE FUNCTION cleanup_old_data(days INT)
            RETURNS VOID AS $$
            BEGIN
                DELETE FROM port_stats  WHERE timestamp < NOW() - (days || ' days')::INTERVAL;
                DELETE FROM flow_stats  WHERE timestamp < NOW() - (days || ' days')::INTERVAL;
                DELETE FROM anomalies   WHERE timestamp < NOW() - (days || ' days')::INTERVAL;
                DELETE FROM control_cycles WHERE started_at < NOW() - (days || ' days')::INTERVAL;
            END;
            $$ LANGUAGE plpgsql;
        """)

    print("[DB] Schema Neon PostgreSQL sẵn sàng.")