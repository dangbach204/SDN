"""
routers/control.py
API endpoints for autonomous closed-loop controller state and policy.
"""

from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from database import get_pool

router = APIRouter(tags=["control"])


class ControlEnabledBody(BaseModel):
    enabled: bool


class ControlPolicyBody(BaseModel):
    congestion_on_pct: Optional[float] = None
    congestion_off_pct: Optional[float] = None
    evaluation_window_seconds: Optional[int] = None
    cooldown_seconds: Optional[int] = None
    retry_cooldown_seconds: Optional[int] = None
    min_confidence: Optional[float] = None
    keep_score: Optional[float] = None
    rollback_score: Optional[float] = None
    stable_cycles_to_release: Optional[int] = None
    max_reoptimize: Optional[int] = None


def _controller(request: Request):
    ctl = getattr(request.app.state, "closed_loop_controller", None)
    if ctl is None:
        raise HTTPException(503, "Closed-loop controller is not available")
    return ctl


@router.get("/control/state")
async def control_state(request: Request):
    ctl = _controller(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        active_count = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM active_control_actions
            WHERE state IN ('pending_eval', 'active', 'reoptimizing')
            """
        )
        cooldown_count = await conn.fetchval(
            "SELECT COUNT(*) FROM active_control_actions WHERE state='cooldown'"
        )
        recent_success = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM control_actions
            WHERE created_at >= NOW() - INTERVAL '24 hours'
              AND execution_ok=TRUE
              AND verification_ok=TRUE
            """
        )
        recent_total = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM control_actions
            WHERE created_at >= NOW() - INTERVAL '24 hours'
            """
        )

    snap = ctl.snapshot()
    return {
        **snap,
        "runtime": {
            "active_actions": int(active_count or 0),
            "cooldown_ports": int(cooldown_count or 0),
            "success_24h": int(recent_success or 0),
            "total_24h": int(recent_total or 0),
        },
    }


@router.get("/control/actions")
async def control_actions(limit: int = 20):
    safe_limit = max(1, min(limit, 200))
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                id,
                to_char(created_at AT TIME ZONE 'Asia/Ho_Chi_Minh',
                        'HH24:MI:SS DD/MM') AS time,
                dpid,
                port_no,
                strategy,
                action_type,
                action_param,
                confidence,
                score,
                decision,
                execution_ok,
                verification_ok,
                rollback_performed,
                execution_message,
                verification_message,
                rollback_message,
                before_kpi::text AS before_kpi,
                after_kpi::text  AS after_kpi
            FROM control_actions
            ORDER BY created_at DESC
            LIMIT $1
            """,
            safe_limit,
        )
    return [dict(r) for r in rows]


@router.get("/control/active")
async def control_active():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                dpid,
                port_no,
                strategy,
                action_type,
                action_param,
                confidence,
                state,
                stable_cycles,
                to_char(cooldown_until AT TIME ZONE 'Asia/Ho_Chi_Minh',
                        'HH24:MI:SS DD/MM') AS cooldown_until,
                to_char(evaluate_after AT TIME ZONE 'Asia/Ho_Chi_Minh',
                        'HH24:MI:SS DD/MM') AS evaluate_after,
                baseline_kpi::text AS baseline_kpi,
                latest_kpi::text AS latest_kpi,
                metadata::text AS metadata,
                control_action_id
            FROM active_control_actions
            ORDER BY dpid, port_no
            """
        )
    return [dict(r) for r in rows]


@router.post("/control/enabled")
async def control_enabled(request: Request, body: ControlEnabledBody):
    ctl = _controller(request)
    ctl.set_enabled(body.enabled)
    return {"ok": True, "enabled": ctl.enabled}


@router.post("/control/policy")
async def control_policy(request: Request, body: ControlPolicyBody):
    ctl = _controller(request)
    updates = body.model_dump(exclude_none=True)
    policy = ctl.update_policy(updates)
    return {"ok": True, "policy": policy}
