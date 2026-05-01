"""
routers/internal.py
Các endpoint nội bộ chỉ dành cho Ryu gọi vào.
Không expose ra ngoài (có thể thêm API key sau).
"""
import ast
import json
from fastapi import APIRouter
from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional

from ..database import get_pool

router = APIRouter(prefix="/internal", tags=["internal"])


# ── Port stats ───────────────────────────────────────────────────────
class PortStatRow(BaseModel):
    timestamp: float
    dpid:      int
    port_no:   int
    rx_bytes:  int = 0
    tx_bytes:  int = 0
    speed_rx:  float = 0.0
    speed_tx:  float = 0.0
    loss:      float = 0.0  # packet loss %

class PortStatsBatch(BaseModel):
    rows: List[PortStatRow]


def _normalize_alert_level(level: str) -> str:
    mapping = {
        "high": "high",
        "warn": "medium",
        "warning": "medium",
        "medium": "medium",
        "zscore": "medium",
        "low": "low",
    }
    return mapping.get((level or "").strip().lower(), "medium")


def _parse_match_payload(match_raw: Optional[Dict[str, Any]], match_str: Optional[str]) -> Dict[str, Any]:
    if isinstance(match_raw, dict):
        return match_raw
    if not match_str:
        return {}

    try:
        parsed = json.loads(match_str)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass

    try:
        parsed = ast.literal_eval(match_str)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}

@router.post("/port_stats")
async def ingest_port_stats(batch: PortStatsBatch):
    pool = await get_pool()
    async with pool.acquire() as conn:
        dpids = sorted({r.dpid for r in batch.rows})
        ports = sorted({(r.dpid, r.port_no) for r in batch.rows})

        await conn.executemany(
            """INSERT INTO switches (dpid)
               VALUES ($1)
               ON CONFLICT (dpid) DO NOTHING""",
            [(dpid,) for dpid in dpids]
        )
        await conn.executemany(
            """INSERT INTO ports (dpid, port_no)
               VALUES ($1, $2)
               ON CONFLICT (dpid, port_no) DO NOTHING""",
            [(dpid, port_no) for dpid, port_no in ports]
        )

        await conn.executemany(
            """INSERT INTO port_stats
               (timestamp,dpid,port_no,rx_bytes,tx_bytes,speed_rx,speed_tx,loss)
               VALUES (to_timestamp($1),$2,$3,$4,$5,$6,$7,$8)""",
            [(r.timestamp, r.dpid, r.port_no,
              r.rx_bytes, r.tx_bytes, r.speed_rx, r.speed_tx, r.loss)
             for r in batch.rows]
        )
    return {"inserted": len(batch.rows)}


# ── Flow stats ───────────────────────────────────────────────────────
class FlowStatRow(BaseModel):
    timestamp: float
    dpid:      int
    priority:  int
    packets:   int
    bytes:     int
    duration: Optional[int] = None
    duration_seconds: Optional[int] = None
    match_str: Optional[str] = None
    match: Optional[Dict[str, Any]] = None

class FlowStatsBatch(BaseModel):
    rows: List[FlowStatRow]

@router.post("/flow_stats")
async def ingest_flow_stats(batch: FlowStatsBatch):
    pool = await get_pool()
    async with pool.acquire() as conn:
        payload = []
        for r in batch.rows:
            duration_seconds = r.duration_seconds if r.duration_seconds is not None else (r.duration or 0)
            match_obj = _parse_match_payload(r.match, r.match_str)
            payload.append(
                (r.timestamp, r.dpid, r.priority, r.packets, r.bytes,
                 duration_seconds, json.dumps(match_obj, ensure_ascii=False))
            )

        await conn.executemany(
            """INSERT INTO flow_stats
               (timestamp,dpid,priority,packets,bytes,duration_seconds,match)
               VALUES (to_timestamp($1),$2,$3,$4,$5,$6,$7::jsonb)""",
            payload
        )
    return {"inserted": len(batch.rows)}


# ── Anomalies ────────────────────────────────────────────────────────
class AnomalyIn(BaseModel):
    timestamp: float
    dpid:      int
    port_no:   int
    metric:    str
    value:     float
    threshold: Optional[float] = None
    level:     str
    message:   str
    details:   Dict[str, Any] = Field(default_factory=dict)

@router.post("/anomalies")
async def ingest_anomaly(a: AnomalyIn):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO switches (dpid)
               VALUES ($1)
               ON CONFLICT (dpid) DO NOTHING""",
            a.dpid,
        )
        await conn.execute(
            """INSERT INTO ports (dpid, port_no)
               VALUES ($1, $2)
               ON CONFLICT (dpid, port_no) DO NOTHING""",
            a.dpid,
            a.port_no,
        )
        await conn.execute(
            """INSERT INTO anomalies
               (timestamp,dpid,port_no,metric,value,threshold,level,message,details)
               VALUES (to_timestamp($1),$2,$3,$4,$5,$6,$7,$8,$9::jsonb)""",
            a.timestamp,
            a.dpid,
            a.port_no,
            a.metric,
            a.value,
            a.threshold,
            _normalize_alert_level(a.level),
            a.message,
            json.dumps(a.details, ensure_ascii=False),
        )
    return {"ok": True}
