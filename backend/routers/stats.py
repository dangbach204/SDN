"""
routers/stats.py — Các endpoint thống kê cho React frontend
"""
from fastapi import APIRouter

from database import get_pool

router = APIRouter(tags=["stats"])


@router.get("/summary")
async def summary():
    pool = await get_pool()
    async with pool.acquire() as conn:
        total      = await conn.fetchval("SELECT COUNT(*) FROM port_stats")
        total_anom = await conn.fetchval("SELECT COUNT(*) FROM anomalies")
        high       = await conn.fetchval("SELECT COUNT(*) FROM anomalies WHERE level='high'")
        warn       = await conn.fetchval("SELECT COUNT(*) FROM anomalies WHERE level='medium'")
    return {
        "total_records":    total,
        "total_anomalies":  total_anom,
        "high":   high,
        "warn":   warn,
        "zscore": 0,
    }


@router.get("/port_stats")
async def port_stats():
    """Top port theo băng thông trung bình trong 60s gần nhất."""
    pool   = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT dpid, port_no,
                   AVG(speed_rx) AS avg_rx,
                   AVG(speed_tx) AS avg_tx,
                   MAX(speed_rx + speed_tx) AS peak
            FROM port_stats
            WHERE timestamp >= NOW() - INTERVAL '60 seconds' AND port_no != 4294967294
            GROUP BY dpid, port_no
            ORDER BY AVG(speed_rx) + AVG(speed_tx) DESC
        """)
    return [dict(r) for r in rows]


@router.get("/history/{dpid}/{port_no}")
async def history(dpid: int, port_no: int):
    """20 điểm dữ liệu gần nhất của một port cụ thể."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT EXTRACT(EPOCH FROM timestamp) AS timestamp, speed_rx, speed_tx
            FROM port_stats
            WHERE dpid=$1 AND port_no=$2 AND port_no != 4294967294
            ORDER BY timestamp DESC LIMIT 20
        """, dpid, port_no)
    return list(reversed([dict(r) for r in rows]))


@router.get("/utilization")
async def utilization():
    """Link utilization % theo capacity 50 Mbps."""
    pool   = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT dpid, port_no,
                   AVG(speed_rx + speed_tx) AS avg_total
            FROM port_stats
            WHERE timestamp >= NOW() - INTERVAL '60 seconds' AND port_no != 4294967294
            GROUP BY dpid, port_no
            ORDER BY dpid, port_no
        """)
    result = []
    for r in rows:
        d = dict(r)
        d["utilization_pct"] = round((d["avg_total"] or 0) / (50 * 1e6) * 100, 1)
        result.append(d)
    return result


@router.get("/flow_stats")
async def flow_stats():
    """Top flow theo bytes trong 2 phút gần nhất."""
    pool   = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT match::text AS match_str,
                   SUM(bytes)   AS total_bytes,
                   COUNT(*)     AS flow_count
            FROM flow_stats
            WHERE timestamp >= NOW() - INTERVAL '120 seconds'
            GROUP BY match
            ORDER BY total_bytes DESC
            LIMIT 10
        """)
    return [dict(r) for r in rows]
