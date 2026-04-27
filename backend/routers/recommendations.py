"""
routers/recommendations.py
Giao tiếp với Ryu qua ofctl_rest (port 8080) khi apply action.
"""
import os
import subprocess
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv

from database import get_pool

router = APIRouter(tags=["recommendations"])

load_dotenv()

DEFAULT_RYU_URL = "http://127.0.0.1:8080"
BLOCK_PRIORITY = 20
REROUTE_PRIORITY = 25

# Topology tam giác 3 switch: mỗi uplink có một cổng backup đối xứng.
REROUTE_BACKUP_PORT = {
    (1, 1): 2,
    (1, 2): 1,
    (2, 1): 2,
    (2, 2): 1,
    (3, 1): 2,
    (3, 2): 1,
}


def _ryu_candidates() -> list[str]:
    primary = (os.getenv("RYU_URL") or DEFAULT_RYU_URL).strip()
    fallback_env = os.getenv("RYU_URL_FALLBACKS", "")
    fallback_urls = [u.strip() for u in fallback_env.split(",") if u.strip()]

    candidates = [
        primary,
        *fallback_urls,
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "http://host.docker.internal:8080",
    ]
    # Giữ thứ tự nhưng loại bỏ URL trùng nhau.
    deduped = []
    seen = set()
    for url in candidates:
        normalized = url.rstrip("/")
        if normalized and normalized not in seen:
            deduped.append(normalized)
            seen.add(normalized)
    return deduped


async def _ryu_post(path: str, payload: dict) -> tuple[bool, str]:
    errors = []
    for base_url in _ryu_candidates():
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.post(f"{base_url}{path}", json=payload)
            if resp.status_code == 200:
                return True, f"Ryu {base_url}"
            errors.append(f"{base_url} => HTTP {resp.status_code}")
        except Exception as exc:
            errors.append(f"{base_url} => {exc}")

    return False, "; ".join(errors)


def _ovs_block(dpid: int, port_no: int) -> tuple[bool, str]:
    switch_name = f"s{dpid}"
    cmd = [
        "sudo", "ovs-ofctl", "-O", "OpenFlow13",
        "add-flow", switch_name,
        f"priority={BLOCK_PRIORITY},in_port={port_no},actions=drop",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode == 0:
        return True, "ovs-ofctl"
    return False, (res.stderr or res.stdout or "unknown error").strip()


def _ovs_unblock(dpid: int, port_no: int) -> tuple[bool, str]:
    switch_name = f"s{dpid}"
    cmd = [
        "sudo", "ovs-ofctl", "-O", "OpenFlow13", "--strict",
        "del-flows", switch_name,
        f"priority={BLOCK_PRIORITY},in_port={port_no}",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode == 0:
        return True, "ovs-ofctl"
    return False, (res.stderr or res.stdout or "unknown error").strip()


def _ovs_add_reroute(dpid: int, in_port: int, out_port: int) -> tuple[bool, str]:
    switch_name = f"s{dpid}"
    cmd = [
        "sudo", "ovs-ofctl", "-O", "OpenFlow13",
        "add-flow", switch_name,
        f"priority={REROUTE_PRIORITY},in_port={in_port},actions=output:{out_port}",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode == 0:
        return True, "ovs-ofctl"
    return False, (res.stderr or res.stdout or "unknown error").strip()


def _ovs_del_reroute(dpid: int, in_port: int) -> tuple[bool, str]:
    switch_name = f"s{dpid}"
    cmd = [
        "sudo", "ovs-ofctl", "-O", "OpenFlow13", "--strict",
        "del-flows", switch_name,
        f"priority={REROUTE_PRIORITY},in_port={in_port}",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode == 0:
        return True, "ovs-ofctl"
    return False, (res.stderr or res.stdout or "unknown error").strip()


def _dump_flows(dpid: int) -> tuple[bool, str]:
    cmd = ["sudo", "ovs-ofctl", "-O", "OpenFlow13", "dump-flows", f"s{dpid}"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode == 0:
        return True, res.stdout
    return False, (res.stderr or res.stdout or "unknown error").strip()


def _has_drop_rule(flow_dump: str, port_no: int) -> bool:
    for line in flow_dump.splitlines():
        if (
            f"priority={BLOCK_PRIORITY}" in line
            and f"in_port={port_no}" in line
            and "actions=drop" in line
        ):
            return True
    return False


def _has_reroute_rule(flow_dump: str, in_port: int, out_port: int) -> bool:
    for line in flow_dump.splitlines():
        if (
            f"priority={REROUTE_PRIORITY}" in line
            and f"in_port={in_port}" in line
            and f"actions=output:{out_port}" in line
        ):
            return True
    return False


def _has_any_reroute_for_in_port(flow_dump: str, in_port: int) -> bool:
    for line in flow_dump.splitlines():
        if (
            f"priority={REROUTE_PRIORITY}" in line
            and f"in_port={in_port}" in line
            and "actions=output:" in line
        ):
            return True
    return False


def _read_tc_qdisc(dpid: int, port_no: int) -> tuple[bool, str]:
    port_name = f"s{dpid}-eth{port_no}"
    cmd = ["sudo", "tc", "qdisc", "show", "dev", port_name]
    res = subprocess.run(cmd, capture_output=True, text=True)
    output = (res.stdout or "") + ("\n" + res.stderr if res.stderr else "")
    if res.returncode != 0:
        return False, output.strip() or f"Không đọc được qdisc trên {port_name}"
    return True, output.strip()


def _has_tc_limit(dpid: int, port_no: int, param_mbps: float) -> tuple[bool, str]:
    port_name = f"s{dpid}-eth{port_no}"
    ok, output = _read_tc_qdisc(dpid, port_no)
    if not ok:
        return False, output

    expected = f"rate {int(param_mbps)}mbit"
    normalized = output.lower()
    if "tbf" in normalized and expected in normalized:
        return True, f"qdisc {port_name} có {expected}"
    return False, f"qdisc {port_name} không chứa {expected}. Raw: {output.strip()}"


def _reroute_target_port(
    dpid: int,
    port_no: int,
    requested_port: Optional[int] = None,
) -> Optional[int]:
    if requested_port is not None and requested_port > 0:
        return int(requested_port)
    return REROUTE_BACKUP_PORT.get((dpid, port_no))


async def _read_recent_speed_mbps(dpid: int, port_no: int) -> Optional[float]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        speed_bps = await conn.fetchval(
            """
            SELECT AVG(speed_rx + speed_tx)
            FROM port_stats
            WHERE dpid=$1 AND port_no=$2
              AND timestamp >= NOW() - INTERVAL '120 seconds'
            """,
            dpid,
            port_no,
        )
    if speed_bps is None:
        return None
    return round(float(speed_bps) / 1e6, 3)


def _verify_action_effect(
    dpid: int,
    port_no: int,
    action_type: str,
    param_mbps: float,
) -> tuple[bool, str]:
    normalized = (action_type or "").strip().lower()

    if normalized in {"qos", "limit_bandwidth"} and param_mbps > 0:
        return _has_tc_limit(dpid, port_no, param_mbps)

    if normalized == "block":
        ok, detail = _dump_flows(dpid)
        if not ok:
            return False, f"Không đọc được flow table: {detail}"
        if _has_drop_rule(detail, port_no):
            return True, f"Đã xác minh DROP rule priority {BLOCK_PRIORITY} cho in_port={port_no}"
        return False, f"Không thấy DROP rule priority {BLOCK_PRIORITY} cho in_port={port_no}"

    if normalized == "unblock":
        ok, detail = _dump_flows(dpid)
        if not ok:
            return False, f"Không đọc được flow table: {detail}"
        if _has_drop_rule(detail, port_no):
            return False, f"DROP rule priority {BLOCK_PRIORITY} vẫn còn trên in_port={port_no}"
        return True, f"DROP rule priority {BLOCK_PRIORITY} đã được gỡ trên in_port={port_no}"

    if normalized == "reroute":
        requested_port = int(param_mbps) if param_mbps >= 1 else None
        backup_port = _reroute_target_port(dpid, port_no, requested_port)
        if backup_port is None:
            return False, f"Port s{dpid}/{port_no} không có uplink backup để reroute"

        ok, detail = _dump_flows(dpid)
        if not ok:
            return False, f"Không đọc được flow table: {detail}"
        if _has_reroute_rule(detail, port_no, backup_port):
            return True, (
                f"Đã xác minh reroute rule priority {REROUTE_PRIORITY}: "
                f"in_port={port_no} -> output={backup_port}"
            )
        return False, (
            f"Không thấy reroute rule priority {REROUTE_PRIORITY} "
            f"in_port={port_no} -> output={backup_port}"
        )

    if normalized == "unreroute":
        requested_port = int(param_mbps) if param_mbps >= 1 else None
        backup_port = _reroute_target_port(dpid, port_no, requested_port)
        ok, detail = _dump_flows(dpid)
        if not ok:
            return False, f"Không đọc được flow table: {detail}"

        if backup_port is None:
            if _has_any_reroute_for_in_port(detail, port_no):
                return False, f"Reroute rule priority {REROUTE_PRIORITY} vẫn còn trên in_port={port_no}"
            return True, f"Không còn reroute rule priority {REROUTE_PRIORITY} trên in_port={port_no}"

        if _has_reroute_rule(detail, port_no, backup_port):
            return False, (
                f"Reroute rule priority {REROUTE_PRIORITY} vẫn còn: "
                f"in_port={port_no} -> output={backup_port}"
            )
        return True, (
            f"Reroute rule priority {REROUTE_PRIORITY} đã được gỡ: "
            f"in_port={port_no}"
        )

    if normalized == "reset_qos":
        ok, detail = _read_tc_qdisc(dpid, port_no)
        if not ok:
            return False, detail
        if "tbf" in detail.lower():
            return False, f"TBF qdisc vẫn còn trên s{dpid}-eth{port_no}: {detail}"
        return True, f"Không còn TBF qdisc trên s{dpid}-eth{port_no}"

    if normalized in {"monitor", "investigate"}:
        return True, "Action dạng quan sát, không cần xác minh enforce trên switch"

    return False, "Không có logic xác minh cho action này"


# ── GET list ────────────────────────────────────────────────────────
@router.get("/recommendations")
async def list_recommendations():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id,
                   to_char(created_at AT TIME ZONE 'Asia/Ho_Chi_Minh',
                           'HH24:MI:SS DD/MM') AS time,
                   dpid,
                   port_no,
                   CASE
                       WHEN level = 'high' THEN 'HIGH'
                       WHEN level = 'medium' THEN 'WARN'
                       ELSE 'LOW'
                   END AS level,
                   CASE
                       WHEN action_type = 'limit_bandwidth' THEN 'QoS'
                       WHEN action_type = 'block' THEN 'BLOCK'
                       WHEN action_type = 'reroute' THEN 'REROUTE'
                       ELSE 'MONITOR'
                   END AS action_type,
                   message,
                   root_cause,
                   actions_json::text AS actions_json,
                   status, chosen_action
            FROM recommendations
            ORDER BY created_at DESC
            LIMIT 50
        """)
    return [dict(r) for r in rows]


# ── POST dismiss ─────────────────────────────────────────────────────
@router.post("/recommendations/{rec_id}/dismiss")
async def dismiss(rec_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE recommendations SET status='dismissed' WHERE id=$1", rec_id)
    return {"result": "ok", "id": rec_id}


# ── POST choose & apply ──────────────────────────────────────────────
class ChooseBody(BaseModel):
    action_id:   str
    action_type: str
    param:       float = 0

@router.post("/recommendations/{rec_id}/choose")
async def choose_action(rec_id: int, body: ChooseBody):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT dpid, port_no FROM recommendations WHERE id=$1", rec_id)
        if not row:
            raise HTTPException(404, "Không tìm thấy recommendation")

    normalized_type = (body.action_type or "").strip().lower()
    before_mbps = await _read_recent_speed_mbps(row["dpid"], row["port_no"])

    ok, result = await _execute_action(
        row["dpid"], row["port_no"], body.action_type, body.param
    )

    verified_ok = False
    verification_msg = "Bỏ qua xác minh vì action thực thi thất bại"
    if ok:
        verified_ok, verification_msg = _verify_action_effect(
            row["dpid"], row["port_no"], body.action_type, body.param
        )

    after_mbps = await _read_recent_speed_mbps(row["dpid"], row["port_no"])

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO action_verifications
            (recommendation_id, dpid, port_no,
             action_id, action_type, param_mbps,
             executed_ok, verified_ok,
             execution_message, verification_message,
             before_mbps, after_mbps)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            """,
            rec_id,
            row["dpid"],
            row["port_no"],
            body.action_id,
            normalized_type,
            body.param,
            ok,
            verified_ok,
            result,
            verification_msg,
            before_mbps,
            after_mbps,
        )

    if ok and verified_ok:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE recommendations SET status='applied', chosen_action=$1, applied_at=NOW() WHERE id=$2",
                body.action_id, rec_id
            )

    delta_mbps = None
    if before_mbps is not None and after_mbps is not None:
        delta_mbps = round(after_mbps - before_mbps, 3)

    return {
        "result": "ok" if (ok and verified_ok) else "error",
        "action": result,
        "verification": verification_msg,
        "execution_ok": ok,
        "verification_ok": verified_ok,
        "before_mbps": before_mbps,
        "after_mbps": after_mbps,
        "delta_mbps": delta_mbps,
    }


async def _execute_action(dpid: int, port_no: int,
                           action_type: str, param_mbps: float) -> tuple[bool, str]:
    """
    Thực thi action bằng cách gọi Ryu ofctl_rest hoặc chạy tc trực tiếp.
    """
    normalized = (action_type or "").strip().lower()

    if normalized in {"qos", "limit_bandwidth"} and param_mbps > 0:
        # Giới hạn băng thông bằng Linux tc (chạy trên Mininet host)
        port_name = f"s{dpid}-eth{port_no}"
        subprocess.run(
            f"sudo tc qdisc del dev {port_name} root 2>/dev/null",
            shell=True)
        r = subprocess.run(
            f"sudo tc qdisc add dev {port_name} root tbf "
            f"rate {int(param_mbps)}mbit burst 10kb latency 50ms",
            shell=True, capture_output=True, text=True)
        if r.returncode == 0:
            return True, f"Đã giới hạn {port_name} xuống {int(param_mbps)} Mbps"
        return False, f"Lỗi TC: {r.stderr.strip()}"

    elif normalized == "block":
        # Ưu tiên gọi Ryu REST, nếu thất bại thì fallback sang ovs-ofctl cục bộ.
        ok, detail = await _ryu_post(
            "/stats/flowentry/add",
            {
                "dpid": dpid,
                "priority": BLOCK_PRIORITY,
                "match": {"in_port": port_no},
                "actions": [],  # DROP
            },
        )
        if ok:
            return True, f"Đã block port s{dpid}/{port_no} qua {detail}"

        ovs_ok, ovs_detail = _ovs_block(dpid, port_no)
        if ovs_ok:
            return True, f"Đã block port s{dpid}/{port_no} bằng fallback {ovs_detail}"

        return False, (
            "Ryu connection failed: "
            f"{detail}. Fallback ovs-ofctl failed: {ovs_detail}"
        )

    elif normalized == "unblock":
        ok, detail = await _ryu_post(
            "/stats/flowentry/delete",
            {
                "dpid": dpid,
                "priority": BLOCK_PRIORITY,
                "match": {"in_port": port_no},
            },
        )
        if ok:
            return True, f"Đã unblock port s{dpid}/{port_no} qua {detail}"

        ovs_ok, ovs_detail = _ovs_unblock(dpid, port_no)
        if ovs_ok:
            return True, f"Đã unblock port s{dpid}/{port_no} bằng fallback {ovs_detail}"

        return False, (
            "Ryu unblock failed: "
            f"{detail}. Fallback ovs-ofctl failed: {ovs_detail}"
        )

    elif normalized == "reroute":
        requested_port = int(param_mbps) if param_mbps >= 1 else None
        backup_port = _reroute_target_port(dpid, port_no, requested_port)
        if backup_port is None:
            return False, f"Port s{dpid}/{port_no} không có uplink backup để reroute"

        ok, detail = await _ryu_post(
            "/stats/flowentry/add",
            {
                "dpid": dpid,
                "priority": REROUTE_PRIORITY,
                "match": {"in_port": port_no},
                "actions": [{"type": "OUTPUT", "port": backup_port}],
            },
        )
        if ok:
            return True, (
                f"Đã reroute port s{dpid}/{port_no} sang uplink {backup_port} "
                f"qua {detail}"
            )

        ovs_ok, ovs_detail = _ovs_add_reroute(dpid, port_no, backup_port)
        if ovs_ok:
            return True, (
                f"Đã reroute port s{dpid}/{port_no} sang uplink {backup_port} "
                f"bằng fallback {ovs_detail}"
            )

        return False, (
            "Reroute failed: "
            f"{detail}. Fallback ovs-ofctl failed: {ovs_detail}"
        )

    elif normalized == "unreroute":
        delete_payload = {
            "dpid": dpid,
            "priority": REROUTE_PRIORITY,
            "match": {"in_port": port_no},
        }
        ok, detail = await _ryu_post("/stats/flowentry/delete_strict", delete_payload)
        if not ok:
            ok, detail = await _ryu_post("/stats/flowentry/delete", delete_payload)

        if ok:
            return True, f"Đã gỡ reroute trên s{dpid}/{port_no} qua {detail}"

        ovs_ok, ovs_detail = _ovs_del_reroute(dpid, port_no)
        if ovs_ok:
            return True, f"Đã gỡ reroute trên s{dpid}/{port_no} bằng fallback {ovs_detail}"

        return False, (
            "Unreroute failed: "
            f"{detail}. Fallback ovs-ofctl failed: {ovs_detail}"
        )

    elif normalized == "reset_qos":
        port_name = f"s{dpid}-eth{port_no}"
        cmd = ["sudo", "tc", "qdisc", "del", "dev", port_name, "root"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        stderr = (res.stderr or "").strip().lower()
        if res.returncode == 0:
            return True, f"Đã gỡ giới hạn QoS trên {port_name}"

        # tc trả lỗi nếu qdisc chưa tồn tại, coi như đã ở trạng thái mong muốn.
        if "no such file" in stderr or "cannot find qdisc" in stderr:
            return True, f"{port_name} không có qdisc giới hạn để gỡ"

        return False, f"Lỗi reset QoS: {(res.stderr or res.stdout or '').strip()}"

    elif normalized == "monitor":
        return True, "Ghi nhận — tiếp tục theo dõi"

    elif normalized == "investigate":
        return True, f"Chạy: sudo ovs-ofctl -O OpenFlow13 dump-flows s{dpid}"

    return False, "Hành động không xác định"
