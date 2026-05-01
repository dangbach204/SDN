import asyncio
import json
import statistics
from typing import Dict, List, Tuple

from .database import get_pool

# Port capacity
def get_port_capacity(dpid: int, port: int) -> float:
    uplink_ports = {
        1: {1},           # s1: eth1 → s2
        2: {1, 2},        # s2: eth1 → s1, eth2 → s3
        3: {1},           # s3: eth1 → s2
    }
    if port in uplink_ports.get(dpid, set()):
        return 100e6      # uplink 100 Mbps
    return 50e6           # host port 50 Mbps

# Ngưỡng
UTIL_HIGH        = 60.0   # %
UTIL_WARN        = 40.0   # %
SPEED_HIGH_MIN   = 20e6   # bps — 20 Mbps
SPEED_WARN_MIN   = 5e6    # bps — 5 Mbps
ZSCORE_THRESHOLD = 2.5

# Helpers
def _calc_util(speed: float, capacity: float) -> float:
    return (speed / capacity * 100.0) if capacity > 0 else 0.0

def _zscore(speed: float, history: list) -> float:
    if len(history) < 5:
        return 0.0
    std = statistics.stdev(history)
    if std < 1e-6:
        return 0.0
    return (speed - statistics.mean(history)) / std

def _hist_str(history: list, n: int = 3) -> str:
    recent = [round(s / 1e6, 1) for s in history[-n:]]
    return str(recent) if recent else "chưa có"

def _trend(history: list) -> str:
    if len(history) < 2:
        return "CHƯA RÕ"
    return "TĂNG" if history[-1] >= history[-2] else "GIẢM"

# Conditions
def _is_high(speed_avg: float, speed_max: float,
             util_avg: float, util_max: float,
             history: list) -> tuple:
    """
    HIGH nếu:
      (1) util_max >= UTIL_HIGH VÀ speed_max >= SPEED_HIGH_MIN
      (2) HOẶC Z-score >= ZSCORE_THRESHOLD
    """
    # Điều kiện 1: congestion
    if speed_max >= SPEED_HIGH_MIN and util_max >= UTIL_HIGH:
        return True, "congestion"

    # Điều kiện 2: z-score spike
    z = _zscore(speed_avg, history)
    if z >= ZSCORE_THRESHOLD and speed_avg >= SPEED_WARN_MIN:
        return True, f"spike (Z={z:.2f}σ)"

    return False, None

def _is_warn(speed_avg: float, util_avg: float) -> bool:
    """
    WARN nếu:
      40% ≤ util_avg < UTIL_HIGH VÀ speed_avg ≥ 5 Mbps
    """
    return speed_avg >= SPEED_WARN_MIN and UTIL_WARN <= util_avg < UTIL_HIGH


# Decision Engine
class DecisionEngine:
    def __init__(self):
        self.speed_history: Dict[Tuple[int, int], List[float]] = {}

    async def run_once(self):
        pool = await get_pool()

        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT
                    dpid,
                    port_no,
                    AVG(GREATEST(speed_rx, speed_tx)) AS avg_speed,
                    MAX(GREATEST(speed_rx, speed_tx)) AS max_speed
                FROM port_stats
                WHERE timestamp >= NOW() - INTERVAL '60 seconds'
                  AND port_no != 4294967294
                GROUP BY dpid, port_no
            """)

        new_recs = 0

        for row in rows:
            dpid      = row["dpid"]
            port      = row["port_no"]
            avg_speed = float(row["avg_speed"] or 0)
            max_speed = float(row["max_speed"] or 0)

            key     = (dpid, port)
            history = self.speed_history.setdefault(key, [])

            # Anti-noise: bỏ qua nếu 3 chu kỳ liên tiếp đều = 0
            if len(history) >= 3 and all(h == 0 for h in history[-3:]) and avg_speed == 0:
                history.append(avg_speed)
                if len(history) > 20:
                    history.pop(0)
                continue

            capacity = get_port_capacity(dpid, port)
            util_avg = _calc_util(avg_speed, capacity)
            util_max = _calc_util(max_speed, capacity)

            level       = None
            action_type = None
            message     = None
            root_cause  = None
            actions     = []

            # Phân loại
            high, reason = _is_high(avg_speed, max_speed,
                                    util_avg, util_max, history)

            if high:
                level       = "high"
                action_type = "limit_bandwidth"
                message = (
                    f"s{dpid}/eth{port}: avg={avg_speed/1e6:.1f} Mbps "
                    f"max={max_speed/1e6:.1f} Mbps "
                    f"({util_max:.1f}%) — HIGH ({reason})"
                )
                root_cause = (
                    f"avg {avg_speed/1e6:.1f} Mbps, max {max_speed/1e6:.1f} Mbps "
                    f"({util_max:.1f}% capacity). "
                    f"Lịch sử: {_hist_str(history)} Mbps, xu hướng: {_trend(history)}."
                )
                actions = [
                    {"id": "qos_10", "label": "Giới hạn 10 Mbps",
                     "type": "QoS", "param": 10,
                     "desc": f"TC rate limiting 10 Mbps trên s{dpid}-eth{port}"},
                    {"id": "qos_20", "label": "Giới hạn 20 Mbps",
                     "type": "QoS", "param": 20,
                     "desc": "Mức vừa phải, vẫn cho traffic hợp lệ đi qua"},
                    {"id": "block",  "label": "Chặn traffic",
                     "type": "BLOCK", "param": 0,
                     "desc": "DROP flow — dùng khi nghi ngờ tấn công"},
                ]

            elif _is_warn(avg_speed, util_avg):
                level       = "medium"
                action_type = "monitor"
                message = (
                    f"s{dpid}/eth{port}: {avg_speed/1e6:.1f} Mbps "
                    f"({util_avg:.1f}%) — WARN"
                )
                root_cause = (
                    f"Băng thông {avg_speed/1e6:.1f} Mbps ({util_avg:.1f}% capacity) "
                    f"trong vùng cảnh báo ({UTIL_WARN}–{UTIL_HIGH}%). "
                    f"Lịch sử: {_hist_str(history)} Mbps, xu hướng: {_trend(history)}."
                )
                actions = [
                    {"id": "monitor", "label": "Theo dõi thêm",
                     "type": "MONITOR", "param": 0,
                     "desc": "Chờ thêm dữ liệu"},
                    {"id": "qos_5",  "label": "Giới hạn 5 Mbps",
                     "type": "QoS", "param": 5,
                     "desc": "Giới hạn nhẹ"},
                    {"id": "qos_10", "label": "Giới hạn 10 Mbps",
                     "type": "QoS", "param": 10,
                     "desc": "Giới hạn vừa"},
                    {"id": "qos_15", "label": "Giới hạn 15 Mbps",
                     "type": "QoS", "param": 15,
                     "desc": "Giới hạn nhẹ để tránh vượt ngưỡng HIGH"},
                ]

            # Insert nếu có level
            if level:
                async with pool.acquire() as conn:
                    existing = await conn.fetchval("""
                        SELECT id FROM recommendations
                        WHERE dpid=$1 AND port_no=$2 AND action_type=$3
                          AND status='pending'
                          AND created_at >= NOW() - INTERVAL '120 seconds'
                    """, dpid, port, action_type)

                if not existing:
                    async with pool.acquire() as conn:
                        await conn.execute("""
                            INSERT INTO recommendations
                                (created_at, dpid, port_no, level, action_type,
                                 message, root_cause, actions_json, status)
                            VALUES
                                (NOW(), $1, $2, $3, $4, $5, $6, $7::jsonb, 'pending')
                        """, dpid, port,
                             level, action_type,
                             message, root_cause or "",
                             json.dumps(actions, ensure_ascii=False))

                    print(f"  [{level.upper()}] {message}")
                    new_recs += 1

            # Cập nhật history
            history.append(avg_speed)
            if len(history) > 20:
                history.pop(0)

        return new_recs

    async def loop(self, interval: int = 60):
        print(f"Decision Engine running (interval={interval}s)")
        while True:
            try:
                n = await self.run_once()
                if n:
                    print(f"  → {n} new recommendations")
            except Exception as e:
                print(f"  [ERR engine] {e}")
            await asyncio.sleep(interval)