"""
routers/anomalies.py
"""
from fastapi import APIRouter
from database import get_pool

router = APIRouter(tags=["anomalies"])


@router.get("/anomalies")
async def anomalies():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                to_char(timestamp AT TIME ZONE 'Asia/Ho_Chi_Minh',
                        'HH24:MI:SS DD/MM') AS time,
                dpid,
                port_no,
                CASE
                    WHEN level = 'high' THEN 'HIGH'
                    WHEN level = 'medium' THEN 'WARN'
                    ELSE 'LOW'
                END AS level,
                message,
                value,
                threshold
            FROM anomalies
            ORDER BY timestamp DESC
            LIMIT 30
        """)
    return [dict(r) for r in rows]
