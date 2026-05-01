"""
closed_loop.py
Autonomous closed-loop SDN controller logic.

Control loop:
Monitor -> Detect -> Decide -> Enforce -> Verify -> Keep/Rollback/Re-optimize
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from adaptive_routing import capacity_mbps, choose_reroute_port, is_uplink_port, mac_to_switch
from .database import get_pool
from routers.recommendations import _execute_action, _verify_action_effect


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


class ClosedLoopController:
    def __init__(self, interval: int = 30):
        self.interval = interval
        self.enabled = True
        self.policy = {
            "congestion_on_pct": 80.0,
            "congestion_off_pct": 65.0,
            "evaluation_window_seconds": 180,
            "cooldown_seconds": 180,
            "retry_cooldown_seconds": 60,
            "min_confidence": 0.55,
            "keep_score": 0.10,
            "rollback_score": -0.10,
            "stable_cycles_to_release": 2,
            "max_reoptimize": 1,
        }
        self._cycle_lock = asyncio.Lock()
        self.last_cycle_at: Optional[str] = None
        self.last_cycle_summary: Dict[str, Any] = {
            "cycle_id": None,
            "status": "idle",
            "congested": 0,
            "actions_planned": 0,
            "actions_applied": 0,
            "evaluated": 0,
            "rolled_back": 0,
            "reoptimized": 0,
        }

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)

    def update_policy(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        for key, value in updates.items():
            if key in self.policy and value is not None:
                self.policy[key] = value
        return self.policy.copy()

    def snapshot(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "interval_seconds": self.interval,
            "policy": self.policy.copy(),
            "last_cycle_at": self.last_cycle_at,
            "last_cycle": self.last_cycle_summary.copy(),
        }

    async def loop(self):
        while True:
            try:
                if self.enabled:
                    await self.run_once()
            except Exception as exc:
                print(f"[ERR closed-loop] {exc}")
            await asyncio.sleep(self.interval)

    async def run_once(self):
        if self._cycle_lock.locked():
            return

        async with self._cycle_lock:
            now = datetime.now(timezone.utc)
            pool = await get_pool()
            summary = {
                "cycle_id": None,
                "status": "running",
                "congested": 0,
                "actions_planned": 0,
                "actions_applied": 0,
                "evaluated": 0,
                "rolled_back": 0,
                "reoptimized": 0,
            }

            async with pool.acquire() as conn:
                cycle_id = await conn.fetchval(
                    """
                    INSERT INTO control_cycles (started_at, status)
                    VALUES (NOW(), 'running')
                    RETURNING id
                    """
                )
                summary["cycle_id"] = cycle_id

                util_rows = await self._fetch_port_utilization(conn, 60)
                util_by_port = {(r["dpid"], r["port_no"]): r["util_pct"] for r in util_rows}

                total_anomalies = await conn.fetchval(
                    "SELECT COUNT(*) FROM anomalies WHERE timestamp >= NOW() - INTERVAL '120 seconds'"
                )

                congested = [
                    r
                    for r in util_rows
                    if r["util_pct"] >= float(self.policy["congestion_on_pct"])
                ]
                summary["congested"] = len(congested)

                active_rows = await conn.fetch("SELECT * FROM active_control_actions")
                active_map = {(r["dpid"], r["port_no"]): r for r in active_rows}

                for row in congested:
                    dpid = int(row["dpid"])
                    port_no = int(row["port_no"])
                    active = active_map.get((dpid, port_no))

                    if self._is_action_blocked_by_state(active, now):
                        continue

                    local_anomalies = await self._count_anomalies(conn, dpid, port_no, 300)
                    trend_pct = await self._port_trend_pct(conn, dpid, port_no)
                    confidence = self._confidence(row["util_pct"], local_anomalies, trend_pct)
                    if confidence < float(self.policy["min_confidence"]):
                        continue

                    summary["actions_planned"] += 1
                    before_kpi = await self._collect_kpi(conn, dpid, port_no)

                    strategy, action_type, action_param, meta = await self._select_action_plan(
                        conn=conn,
                        dpid=dpid,
                        port_no=port_no,
                        util_by_port=util_by_port,
                        is_uplink=bool(row["is_uplink"]),
                        congestion_pct=float(row["util_pct"]),
                    )

                    execution_ok, execution_msg = await _execute_action(
                        dpid=dpid,
                        port_no=port_no,
                        action_type=action_type,
                        param_mbps=action_param,
                    )
                    verify_ok, verify_msg = await _verify_action_effect(
                        dpid=dpid,
                        port_no=port_no,
                        action_type=action_type,
                        param_mbps=action_param,
                    )

                    control_action_id = await conn.fetchval(
                        """
                        INSERT INTO control_actions
                        (cycle_id, dpid, port_no,
                         strategy, action_type, action_param,
                         confidence, decision,
                         execution_ok, verification_ok,
                         execution_message, verification_message,
                         before_kpi, metadata,
                         created_at, updated_at)
                        VALUES
                        ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb,$14::jsonb,NOW(),NOW())
                        RETURNING id
                        """,
                        cycle_id,
                        dpid,
                        port_no,
                        strategy,
                        action_type,
                        action_param,
                        confidence,
                        "pending_eval" if (execution_ok and verify_ok) else "failed",
                        execution_ok,
                        verify_ok,
                        execution_msg,
                        verify_msg,
                        json.dumps(before_kpi, ensure_ascii=False),
                        json.dumps(meta, ensure_ascii=False),
                    )

                    if execution_ok and verify_ok:
                        summary["actions_applied"] += 1
                        await self._upsert_active_action(
                            conn=conn,
                            dpid=dpid,
                            port_no=port_no,
                            strategy=strategy,
                            action_type=action_type,
                            action_param=action_param,
                            confidence=confidence,
                            state="pending_eval",
                            baseline_kpi=before_kpi,
                            latest_kpi=before_kpi,
                            metadata={"reopt_count": 0, **meta},
                            control_action_id=control_action_id,
                            evaluate_after=now + timedelta(seconds=int(self.policy["evaluation_window_seconds"])),
                            cooldown_until=now + timedelta(seconds=int(self.policy["cooldown_seconds"])),
                            stable_cycles=0,
                        )
                    else:
                        await self._upsert_active_action(
                            conn=conn,
                            dpid=dpid,
                            port_no=port_no,
                            strategy=strategy,
                            action_type=action_type,
                            action_param=action_param,
                            confidence=confidence,
                            state="cooldown",
                            baseline_kpi=before_kpi,
                            latest_kpi=before_kpi,
                            metadata={"reopt_count": 0, **meta},
                            control_action_id=control_action_id,
                            evaluate_after=None,
                            cooldown_until=now + timedelta(seconds=int(self.policy["retry_cooldown_seconds"])),
                            stable_cycles=0,
                        )

                evaluated, rolled_back, reoptimized = await self._evaluate_active_actions(
                    conn=conn,
                    now=now,
                    util_by_port=util_by_port,
                )
                summary["evaluated"] = evaluated
                summary["rolled_back"] = rolled_back
                summary["reoptimized"] = reoptimized

                await conn.execute(
                    """
                    UPDATE control_cycles
                    SET ended_at=NOW(),
                        status='completed',
                        congested_links=$1,
                        anomalies=$2,
                        actions_planned=$3,
                        actions_applied=$4,
                        metadata=$5::jsonb
                    WHERE id=$6
                    """,
                    summary["congested"],
                    int(total_anomalies or 0),
                    summary["actions_planned"],
                    summary["actions_applied"],
                    json.dumps(
                        {
                            "evaluated": summary["evaluated"],
                            "rolled_back": summary["rolled_back"],
                            "reoptimized": summary["reoptimized"],
                        },
                        ensure_ascii=False,
                    ),
                    cycle_id,
                )

            self.last_cycle_at = now.isoformat()
            self.last_cycle_summary = summary

    async def _fetch_port_utilization(self, conn, window_seconds: int) -> List[Dict[str, Any]]:
        rows = await conn.fetch(
            """
            SELECT dpid, port_no, AVG(speed_rx + speed_tx) AS avg_bps
            FROM port_stats
            WHERE timestamp >= NOW() - make_interval(secs => $1)
              AND port_no != 4294967294
            GROUP BY dpid, port_no
            """,
            window_seconds,
        )

        result: List[Dict[str, Any]] = []
        for r in rows:
            dpid = int(r["dpid"])
            port_no = int(r["port_no"])
            avg_bps = float(r["avg_bps"] or 0.0)
            cap = capacity_mbps(dpid, port_no)
            util = (avg_bps / (cap * 1e6) * 100.0) if cap > 0 else 0.0
            result.append(
                {
                    "dpid": dpid,
                    "port_no": port_no,
                    "avg_bps": avg_bps,
                    "capacity_mbps": cap,
                    "util_pct": util,
                    "is_uplink": is_uplink_port(dpid, port_no),
                }
            )
        return result

    async def _count_anomalies(
        self,
        conn,
        dpid: Optional[int],
        port_no: Optional[int],
        seconds: int,
    ) -> int:
        if dpid is None or port_no is None:
            v = await conn.fetchval(
                "SELECT COUNT(*) FROM anomalies WHERE timestamp >= NOW() - make_interval(secs => $1)",
                seconds,
            )
            return int(v or 0)

        v = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM anomalies
            WHERE dpid=$1 AND port_no=$2
              AND timestamp >= NOW() - make_interval(secs => $3)
            """,
            dpid,
            port_no,
            seconds,
        )
        return int(v or 0)

    async def _port_trend_pct(self, conn, dpid: int, port_no: int) -> float:
        row = await conn.fetchrow(
            """
            SELECT  
                AVG(CASE WHEN timestamp >= NOW() - INTERVAL '60 seconds'
                         THEN speed_rx + speed_tx END) AS recent_avg,
                AVG(CASE WHEN timestamp < NOW() - INTERVAL '60 seconds'
                           AND timestamp >= NOW() - INTERVAL '120 seconds'
                         THEN speed_rx + speed_tx END) AS prev_avg
            FROM port_stats
            WHERE dpid=$1 AND port_no=$2
            """,
            dpid,
            port_no,
        )
        if not row:
            return 0.0
        recent = float(row["recent_avg"] or 0.0)
        prev = float(row["prev_avg"] or 0.0)
        if prev <= 0:
            return 0.0
        return (recent - prev) / prev * 100.0

    def _confidence(self, util_pct: float, local_anomalies: int, trend_pct: float) -> float:
        util_term = max(0.0, min((util_pct - 70.0) / 30.0, 1.0))
        anomaly_term = min(local_anomalies / 5.0, 1.0)
        trend_term = max(0.0, min(trend_pct / 30.0, 1.0))
        conf = 0.35 + util_term * 0.45 + anomaly_term * 0.15 + trend_term * 0.05
        return max(0.0, min(conf, 1.0))

    async def _collect_kpi(self, conn, dpid: int, port_no: int) -> Dict[str, Any]:
        row = await conn.fetchrow(
            """
            SELECT
                AVG(speed_rx + speed_tx) AS avg_bps,
                MAX(speed_rx + speed_tx) AS peak_bps,
                STDDEV_POP(speed_rx + speed_tx) AS volatility_bps,
                COUNT(*) AS sample_count
            FROM port_stats
            WHERE dpid=$1 AND port_no=$2
              AND timestamp >= NOW() - INTERVAL '120 seconds'
            """,
            dpid,
            port_no,
        )
        avg_bps = float((row and row["avg_bps"]) or 0.0)
        peak_bps = float((row and row["peak_bps"]) or 0.0)
        vol_bps = float((row and row["volatility_bps"]) or 0.0)
        samples = int((row and row["sample_count"]) or 0)

        cap = capacity_mbps(dpid, port_no)
        util_pct = (avg_bps / (cap * 1e6) * 100.0) if cap > 0 else 0.0
        anomalies = await self._count_anomalies(conn, dpid, port_no, 120)

        # Latency/jitter/loss are not directly available from current telemetry.
        return {
            "avg_mbps": round(avg_bps / 1e6, 3),
            "peak_mbps": round(peak_bps / 1e6, 3),
            "utilization_pct": round(util_pct, 3),
            "anomaly_count": anomalies,
            "volatility_mbps": round(vol_bps / 1e6, 3),
            "latency_ms": None,
            "jitter_ms": None,
            "packet_loss_pct": None,
            "sample_count": samples,
        }

    async def _select_action_plan(
        self,
        conn,
        dpid: int,
        port_no: int,
        util_by_port: Dict[Tuple[int, int], float],
        is_uplink: bool,
        congestion_pct: float,
    ) -> Tuple[str, str, float, Dict[str, Any]]:
        if not is_uplink:
            qos_limit = 10.0 if congestion_pct >= 90 else 15.0
            return (
                "qos_host",
                "limit_bandwidth",
                qos_limit,
                {"reason": "host_port_congestion", "target_mbps": qos_limit},
            )

        top_flow = await conn.fetchrow(
            """
            SELECT
                match->>'eth_dst' AS eth_dst,
                SUM(bytes) AS total_bytes
            FROM flow_stats
            WHERE dpid=$1
              AND timestamp >= NOW() - INTERVAL '120 seconds'
              AND (
                  CASE
                      WHEN (match->>'in_port') ~ '^[0-9]+$'
                      THEN (match->>'in_port')::int
                      ELSE NULL
                  END
              ) = $2
            GROUP BY eth_dst
            ORDER BY total_bytes DESC
            LIMIT 1
            """,
            dpid,
            port_no,
        )

        dst_mac = top_flow["eth_dst"] if top_flow else None
        dst_switch = mac_to_switch(dst_mac)
        out_port, path, reason = choose_reroute_port(
            dpid=dpid,
            congested_port=port_no,
            util_by_port=util_by_port,
            dst_switch=dst_switch,
        )
        if out_port is not None:
            return (
                "adaptive_reroute",
                "reroute",
                float(out_port),
                {
                    "reason": reason,
                    "path": path,
                    "dst_switch": dst_switch,
                    "dst_mac": dst_mac,
                },
            )

        qos_limit = 15.0
        return (
            "qos_fallback",
            "limit_bandwidth",
            qos_limit,
            {
                "reason": "reroute_not_available",
                "target_mbps": qos_limit,
                "dst_switch": dst_switch,
            },
        )

    async def _upsert_active_action(
        self,
        conn,
        dpid: int,
        port_no: int,
        strategy: str,
        action_type: str,
        action_param: float,
        confidence: float,
        state: str,
        baseline_kpi: Dict[str, Any],
        latest_kpi: Dict[str, Any],
        metadata: Dict[str, Any],
        control_action_id: Optional[int],
        evaluate_after: Optional[datetime],
        cooldown_until: Optional[datetime],
        stable_cycles: int,
    ):
        await conn.execute(
            """
            INSERT INTO active_control_actions
            (dpid, port_no,
             strategy, action_type, action_param,
             confidence, state,
             cooldown_until, evaluate_after,
             stable_cycles,
             baseline_kpi, latest_kpi, metadata,
             control_action_id,
             created_at, updated_at)
            VALUES
            ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb,$12::jsonb,$13::jsonb,$14,NOW(),NOW())
            ON CONFLICT (dpid, port_no)
            DO UPDATE SET
                strategy=EXCLUDED.strategy,
                action_type=EXCLUDED.action_type,
                action_param=EXCLUDED.action_param,
                confidence=EXCLUDED.confidence,
                state=EXCLUDED.state,
                cooldown_until=EXCLUDED.cooldown_until,
                evaluate_after=EXCLUDED.evaluate_after,
                stable_cycles=EXCLUDED.stable_cycles,
                baseline_kpi=EXCLUDED.baseline_kpi,
                latest_kpi=EXCLUDED.latest_kpi,
                metadata=EXCLUDED.metadata,
                control_action_id=EXCLUDED.control_action_id,
                updated_at=NOW()
            """,
            dpid,
            port_no,
            strategy,
            action_type,
            action_param,
            confidence,
            state,
            cooldown_until,
            evaluate_after,
            stable_cycles,
            json.dumps(baseline_kpi, ensure_ascii=False),
            json.dumps(latest_kpi, ensure_ascii=False),
            json.dumps(metadata, ensure_ascii=False),
            control_action_id,
        )

    def _is_action_blocked_by_state(self, active_row, now: datetime) -> bool:
        if not active_row:
            return False

        state = str(active_row["state"] or "")
        cooldown_until = active_row["cooldown_until"]

        if state in {"pending_eval", "active", "reoptimizing"}:
            return True
        if cooldown_until and cooldown_until > now:
            return True
        return False

    def _action_score(self, before: Dict[str, Any], after: Dict[str, Any]) -> float:
        before_util = float(before.get("utilization_pct") or 0.0)
        after_util = float(after.get("utilization_pct") or 0.0)
        before_anom = float(before.get("anomaly_count") or 0.0)
        after_anom = float(after.get("anomaly_count") or 0.0)
        before_bw = float(before.get("avg_mbps") or 0.0)
        after_bw = float(after.get("avg_mbps") or 0.0)

        util_improve = (before_util - after_util) / max(before_util, 1.0)
        anom_improve = (before_anom - after_anom) / max(before_anom, 1.0)
        bw_preserve = (after_bw / max(before_bw, 0.1)) - 1.0

        # Weighted score: congestion relief and anomaly reduction are primary.
        score = util_improve * 0.55 + anom_improve * 0.35 + bw_preserve * 0.10
        return round(score, 4)

    def _inverse_action(self, action_type: str, action_param: float) -> Tuple[str, float]:
        normalized = (action_type or "").strip().lower()
        if normalized in {"qos", "limit_bandwidth"}:
            return "reset_qos", 0.0
        if normalized == "block":
            return "unblock", 0.0
        if normalized == "reroute":
            return "unreroute", action_param
        return "monitor", 0.0

    async def _evaluate_active_actions(
        self,
        conn,
        now: datetime,
        util_by_port: Dict[Tuple[int, int], float],
    ) -> Tuple[int, int, int]:
        rows = await conn.fetch(
            """
            SELECT *
            FROM active_control_actions
            WHERE state IN ('pending_eval', 'active', 'reoptimizing')
              AND evaluate_after IS NOT NULL
              AND evaluate_after <= NOW()
            """
        )

        evaluated = 0
        rolled_back = 0
        reoptimized = 0

        for row in rows:
            evaluated += 1
            dpid = int(row["dpid"])
            port_no = int(row["port_no"])
            action_type = str(row["action_type"])
            action_param = float(row["action_param"] or 0.0)
            control_action_id = row["control_action_id"]
            metadata = _as_dict(row["metadata"])
            reopt_count = int(metadata.get("reopt_count", 0))
            stable_cycles = int(row["stable_cycles"] or 0)

            before_kpi = _as_dict(row["baseline_kpi"])
            after_kpi = await self._collect_kpi(conn, dpid, port_no)
            score = self._action_score(before_kpi, after_kpi)

            after_util = float(after_kpi.get("utilization_pct") or 0.0)
            if after_util <= float(self.policy["congestion_off_pct"]):
                stable_cycles += 1
            else:
                stable_cycles = 0

            decision = "keep"
            rollback_performed = False
            rollback_message = None
            next_state = "active"
            next_action_type = action_type
            next_action_param = action_param
            next_strategy = str(row["strategy"])
            evaluate_after = now + timedelta(seconds=int(self.policy["evaluation_window_seconds"]))
            cooldown_until = row["cooldown_until"]

            if stable_cycles >= int(self.policy["stable_cycles_to_release"]):
                decision = "release"
                inverse_action, inverse_param = self._inverse_action(action_type, action_param)
                rb_ok, rb_msg = await _execute_action(dpid, port_no, inverse_action, inverse_param)
                rb_verify, rb_verify_msg = await _verify_action_effect(dpid, port_no, inverse_action, inverse_param)
                rollback_performed = True
                rollback_message = f"{rb_msg}; {rb_verify_msg}"
                rolled_back += 1
                if rb_ok and rb_verify:
                    next_state = "cooldown"
                    evaluate_after = None
                    cooldown_until = now + timedelta(seconds=int(self.policy["cooldown_seconds"]))
                else:
                    next_state = "rollback_failed"
                    evaluate_after = None

            elif score <= float(self.policy["rollback_score"]):
                decision = "rollback"
                inverse_action, inverse_param = self._inverse_action(action_type, action_param)
                rb_ok, rb_msg = await _execute_action(dpid, port_no, inverse_action, inverse_param)
                rb_verify, rb_verify_msg = await _verify_action_effect(dpid, port_no, inverse_action, inverse_param)
                rollback_performed = True
                rollback_message = f"{rb_msg}; {rb_verify_msg}"
                rolled_back += 1
                if rb_ok and rb_verify:
                    next_state = "cooldown"
                    evaluate_after = None
                    cooldown_until = now + timedelta(seconds=int(self.policy["cooldown_seconds"]))
                else:
                    next_state = "rollback_failed"
                    evaluate_after = None

            elif score < float(self.policy["keep_score"]) and reopt_count < int(self.policy["max_reoptimize"]):
                decision = "reoptimize"
                alt_strategy, alt_action, alt_param, alt_meta = await self._reoptimize_plan(
                    conn=conn,
                    dpid=dpid,
                    port_no=port_no,
                    current_action=action_type,
                    current_param=action_param,
                    util_by_port=util_by_port,
                )
                if alt_action:
                    ex_ok, ex_msg = await _execute_action(dpid, port_no, alt_action, alt_param)
                    vf_ok, vf_msg = await _verify_action_effect(dpid, port_no, alt_action, alt_param)
                    if ex_ok and vf_ok:
                        reoptimized += 1
                        reopt_count += 1
                        next_state = "reoptimizing"
                        next_action_type = alt_action
                        next_action_param = alt_param
                        next_strategy = alt_strategy
                        stable_cycles = 0
                        metadata.update(alt_meta)
                        metadata["reopt_count"] = reopt_count

                        new_control_action_id = await conn.fetchval(
                            """
                            INSERT INTO control_actions
                            (cycle_id, dpid, port_no,
                             strategy, action_type, action_param,
                             confidence, decision,
                             execution_ok, verification_ok,
                             execution_message, verification_message,
                             before_kpi, metadata,
                             created_at, updated_at)
                            VALUES
                            (NULL,$1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb,$13::jsonb,NOW(),NOW())
                            RETURNING id
                            """,
                            dpid,
                            port_no,
                            alt_strategy,
                            alt_action,
                            alt_param,
                            float(row["confidence"] or 0.0),
                            "pending_eval",
                            ex_ok,
                            vf_ok,
                            ex_msg,
                            vf_msg,
                            json.dumps(after_kpi, ensure_ascii=False),
                            json.dumps(metadata, ensure_ascii=False),
                        )
                        control_action_id = new_control_action_id
                    else:
                        decision = "rollback"
                        inverse_action, inverse_param = self._inverse_action(action_type, action_param)
                        rb_ok, rb_msg = await _execute_action(dpid, port_no, inverse_action, inverse_param)
                        rb_verify, rb_verify_msg = await _verify_action_effect(dpid, port_no, inverse_action, inverse_param)
                        rollback_performed = True
                        rollback_message = f"{rb_msg}; {rb_verify_msg}"
                        rolled_back += 1
                        next_state = "cooldown" if (rb_ok and rb_verify) else "rollback_failed"
                        evaluate_after = None
                        cooldown_until = now + timedelta(seconds=int(self.policy["cooldown_seconds"]))
                else:
                    decision = "keep"

            metadata["reopt_count"] = reopt_count

            if control_action_id:
                await conn.execute(
                    """
                    UPDATE control_actions
                    SET score=$1,
                        decision=$2,
                        rollback_performed=$3,
                        rollback_message=$4,
                        after_kpi=$5::jsonb,
                        updated_at=NOW()
                    WHERE id=$6
                    """,
                    score,
                    decision,
                    rollback_performed,
                    rollback_message,
                    json.dumps(after_kpi, ensure_ascii=False),
                    control_action_id,
                )

            await self._upsert_active_action(
                conn=conn,
                dpid=dpid,
                port_no=port_no,
                strategy=next_strategy,
                action_type=next_action_type,
                action_param=next_action_param,
                confidence=float(row["confidence"] or 0.0),
                state=next_state,
                baseline_kpi=after_kpi,
                latest_kpi=after_kpi,
                metadata=metadata,
                control_action_id=control_action_id,
                evaluate_after=evaluate_after,
                cooldown_until=cooldown_until,
                stable_cycles=stable_cycles,
            )

        return evaluated, rolled_back, reoptimized

    async def _reoptimize_plan(
        self,
        conn,
        dpid: int,
        port_no: int,
        current_action: str,
        current_param: float,
        util_by_port: Dict[Tuple[int, int], float],
    ) -> Tuple[str, Optional[str], float, Dict[str, Any]]:
        normalized = (current_action or "").strip().lower()

        if normalized == "reroute":
            # If reroute underperforms, fall back to QoS clamp.
            return "qos_reopt", "limit_bandwidth", 10.0, {"reason": "reroute_underperformed"}

        if normalized in {"qos", "limit_bandwidth"} and is_uplink_port(dpid, port_no):
            flow = await conn.fetchrow(
                """
                SELECT
                    match->>'eth_dst' AS eth_dst,
                    SUM(bytes) AS total_bytes
                FROM flow_stats
                WHERE dpid=$1
                  AND timestamp >= NOW() - INTERVAL '120 seconds'
                  AND (
                      CASE
                          WHEN (match->>'in_port') ~ '^[0-9]+$'
                          THEN (match->>'in_port')::int
                          ELSE NULL
                      END
                  ) = $2
                GROUP BY eth_dst
                ORDER BY total_bytes DESC
                LIMIT 1
                """,
                dpid,
                port_no,
            )
            dst_mac = flow["eth_dst"] if flow else None
            dst_switch = mac_to_switch(dst_mac)
            out_port, path, reason = choose_reroute_port(
                dpid=dpid,
                congested_port=port_no,
                util_by_port=util_by_port,
                dst_switch=dst_switch,
            )
            if out_port is not None and int(out_port) != int(current_param):
                return (
                    "reroute_reopt",
                    "reroute",
                    float(out_port),
                    {
                        "reason": reason,
                        "path": path,
                        "dst_switch": dst_switch,
                        "dst_mac": dst_mac,
                    },
                )

        return "none", None, 0.0, {"reason": "no_better_action"}
