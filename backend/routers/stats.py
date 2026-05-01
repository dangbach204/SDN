"""
routers/stats.py — Các endpoint thống kê cho React frontend
"""

from fastapi import APIRouter

import subprocess
import re

from ..database import get_pool

router = APIRouter(tags=["stats"])


# Topology chuỗi thẳng s1—s2—s3
# s1-eth1 ↔ s2-eth1 (100 Mbps)
# s2-eth2 ↔ s3-eth1 (100 Mbps)
UPLINK_PORTS = {
    (1, 1),
    (2, 1), (2, 2),
    (3, 1),
}

def get_path(src, dst):
    def get_switch(h):
        num = int(h.replace("h", ""))
        if num <= 4:
            return "s1"
        elif num <= 8:
            return "s2"
        else:
            return "s3"

    s_src = get_switch(src)
    s_dst = get_switch(dst)

    if s_src == s_dst:
        return [src, s_src, dst]

    if {s_src, s_dst} == {"s1", "s3"}:
        return [src, "s1", "s2", "s3", dst]

    return [src, s_src, s_dst, dst]


def _capacity_mbps(dpid: int, port_no: int) -> float:
    return 100.0 if (dpid, port_no) in UPLINK_PORTS else 50.0


@router.get("/summary")
async def summary():
    pool = await get_pool()
    async with pool.acquire() as conn:
        total      = await conn.fetchval("SELECT COUNT(*) FROM port_stats")
        total_anom = await conn.fetchval("SELECT COUNT(*) FROM anomalies")
        high       = await conn.fetchval("SELECT COUNT(*) FROM anomalies WHERE level='high'")
        warn       = await conn.fetchval("SELECT COUNT(*) FROM anomalies WHERE level='medium'")
        zscore     = await conn.fetchval("SELECT COUNT(*) FROM anomalies WHERE message ILIKE '%Z-score%'")
    return {
        "total_records":   total,
        "total_anomalies": total_anom,
        "high":   high,
        "warn":   warn,
        "zscore": zscore,
    }


@router.get("/port_stats")
async def port_stats():
    """Top port theo băng thông trung bình trong 60s gần nhất."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT dpid, port_no,
                   AVG(speed_rx) AS avg_rx,
                   AVG(speed_tx) AS avg_tx,
                   AVG(loss) AS avg_loss,
                   MAX(speed_rx + speed_tx) AS peak
            FROM port_stats
            WHERE timestamp >= NOW() - INTERVAL '60 seconds'
              AND port_no != 4294967294
            GROUP BY dpid, port_no
            ORDER BY AVG(speed_rx) + AVG(speed_tx) DESC
        """)

    result = []
    for r in rows:
        d = dict(r)
        avg_total = (d.get("avg_rx") or 0) + (d.get("avg_tx") or 0)
        cap_mbps  = _capacity_mbps(d["dpid"], d["port_no"])
        d["avg_total"]      = avg_total
        d["capacity_mbps"]  = cap_mbps
        d["is_uplink"]      = (d["dpid"], d["port_no"]) in UPLINK_PORTS
        d["utilization_pct"] = round(avg_total / (cap_mbps * 1e6) * 100, 1) if cap_mbps > 0 else 0.0
        d["avg_loss"]       = round(d.get("avg_loss") or 0, 1)  # packet loss %
        result.append(d)
    return result


@router.get("/history/{dpid}/{port_no}")
async def history(dpid: int, port_no: int):
    """20 điểm dữ liệu gần nhất của một port cụ thể (bao gồm loss)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT EXTRACT(EPOCH FROM timestamp) AS timestamp,
                   speed_rx, speed_tx, loss
            FROM port_stats
            WHERE dpid=$1 AND port_no=$2 AND port_no != 4294967294
            ORDER BY timestamp DESC LIMIT 20
        """, dpid, port_no)
    return list(reversed([dict(r) for r in rows]))


@router.get("/utilization")
async def utilization():
    """Link utilization % theo capacity thực tế của từng port."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT dpid, port_no,
                   AVG(speed_rx + speed_tx) AS avg_total
            FROM port_stats
            WHERE timestamp >= NOW() - INTERVAL '60 seconds'
              AND port_no != 4294967294
            GROUP BY dpid, port_no
            ORDER BY dpid, port_no
        """)
    result = []
    for r in rows:
        d = dict(r)
        cap_mbps = _capacity_mbps(d["dpid"], d["port_no"])
        d["capacity_mbps"]   = cap_mbps
        d["is_uplink"]       = (d["dpid"], d["port_no"]) in UPLINK_PORTS
        d["utilization_pct"] = round((d["avg_total"] or 0) / (cap_mbps * 1e6) * 100, 1) if cap_mbps > 0 else 0.0
        result.append(d)
    return result


@router.get("/flow_stats")
async def flow_stats():
    """Top flow theo bytes trong 2 phút gần nhất."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT match::text AS match_str,
                   SUM(bytes)  AS total_bytes,
                   COUNT(*)    AS flow_count
            FROM flow_stats
            WHERE timestamp >= NOW() - INTERVAL '120 seconds'
            GROUP BY match
            ORDER BY total_bytes DESC
            LIMIT 10
        """)
    return [dict(r) for r in rows]


@router.post("/reset")
async def reset_network_state():
    pool = await get_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM recommendations WHERE status = 'pending'"
        )
        await conn.execute(
            "UPDATE recommendations SET status = 'dismissed' WHERE status = 'pending'"
        )
    return {"ok": True, "dismissed": int(count or 0)}

@router.get("/flow-metrics")
async def flow_metrics(src: str, dst: str):
    """
    Đo realtime latency, jitter, loss giữa 2 host
    """

    # map host -> IP
    def host_to_ip(h):
        num = int(h.replace("h", ""))
        return f"10.0.0.{num}"
        # return f"h{num}"

    src_ip = host_to_ip(src)
    dst_ip = host_to_ip(dst)

    try:
        # chạy ping từ host src
        cmd = f"echo '{src} ping -c 5 {dst}' | sudo python3 ~/mininet/util/m"
        result = subprocess.check_output(cmd, shell=True, text=True)

        # parse latency
        match = re.search(r"rtt min/avg/max/mdev = ([\d\.]+)/([\d\.]+)/([\d\.]+)/([\d\.]+)", result)
        if match:
            latency = float(match.group(2))
            jitter  = float(match.group(4))
        else:
            latency = jitter = 0

        # parse loss
        loss_match = re.search(r"(\d+)% packet loss", result)
        loss = float(loss_match.group(1)) if loss_match else 0

        return {
            "src": src,
            "dst": dst,
            "latency_ms": latency,
            "jitter_ms": jitter,
            "loss_pct": loss
        }

    except Exception as e:
        return {"error": str(e)}