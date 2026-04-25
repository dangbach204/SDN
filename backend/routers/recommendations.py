"""
routers/recommendations.py
Giao tiếp với Ryu qua ofctl_rest (port 8080) khi apply action.
"""
import subprocess
import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from database import get_pool

router = APIRouter(tags=["recommendations"])

RYU_URL = "http://127.0.0.1:8080"   # Ryu ofctl_rest


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
        await conn.execute(
            "UPDATE recommendations SET status='applied', chosen_action=$1, applied_at=NOW() WHERE id=$2",
            body.action_id, rec_id)

    result = await _execute_action(row["dpid"], row["port_no"],
                                   body.action_type, body.param)
    return {"result": "ok", "action": result}


async def _execute_action(dpid: int, port_no: int,
                           action_type: str, param_mbps: float) -> str:
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
            return f"Đã giới hạn {port_name} xuống {int(param_mbps)} Mbps"
        return f"Lỗi TC: {r.stderr.strip()}"

    elif normalized == "block":
        # Gửi flow rule DROP qua Ryu ofctl_rest
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.post(
                    f"{RYU_URL}/stats/flowentry/add",
                    json={
                        "dpid":     dpid,
                        "priority": 20,
                        "match":    {"in_port": port_no},
                        "actions":  []   # DROP
                    })
            if resp.status_code == 200:
                return f"Đã block port s{dpid}/{port_no}"
            return f"Ryu trả về {resp.status_code}"
        except Exception as e:
            return f"Không kết nối được Ryu: {e}"

    elif normalized == "unblock":
        # Xóa flow rule DROP
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(
                    f"{RYU_URL}/stats/flowentry/delete",
                    json={
                        "dpid":     dpid,
                        "priority": 20,
                        "match":    {"in_port": port_no},
                    })
            return f"Đã unblock port s{dpid}/{port_no}"
        except Exception as e:
            return f"Lỗi unblock: {e}"

    elif normalized in {"monitor", "reroute"}:
        return "Ghi nhận — tiếp tục theo dõi"

    elif normalized == "investigate":
        return f"Chạy: sudo ovs-ofctl -O OpenFlow13 dump-flows s{dpid}"

    return "Hành động không xác định"
