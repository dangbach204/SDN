"""
decision_engine.py — Chạy trong FastAPI lifespan (asyncio task)
Đọc dữ liệu từ Neon PostgreSQL, sinh recommendations khi phát hiện bất thường.
"""
import asyncio
import json
import statistics
from typing import Dict, List, Tuple

from database import get_pool

# Rules
RULES = [
    {
        "name":        "high_bandwidth",
        "condition":   lambda speed, history: speed >= 20e6,
        "level":       "high",
        "action_type": "limit_bandwidth",
        "message":     lambda dpid, port, speed, **kw:
                       f"Port s{dpid}/{port} đang dùng {speed/1e6:.1f} Mbps — vượt ngưỡng cao (20 Mbps)",
        "root_cause":  lambda dpid, port, speed, **kw:
                       f"Băng thông {speed/1e6:.1f} Mbps vượt 20 Mbps. Nguyên nhân có thể: "
                       f"truyền file lớn, stream video, ứng dụng nền, hoặc DDoS nếu nhiều port cùng tăng.",
        "actions":     lambda dpid, port, **kw: [
            {"id":"qos_10",  "label":"Giới hạn 10 Mbps","type":"QoS","param":10,
             "desc":f"TC rate limiting 10 Mbps trên s{dpid}-eth{port}"},
            {"id":"qos_20",  "label":"Giới hạn 20 Mbps","type":"QoS","param":20,
             "desc":"Mức vừa phải, vẫn cho traffic hợp lệ đi qua"},
            {"id":"monitor", "label":"Theo dõi thêm","type":"MONITOR","param":0,
             "desc":"Chờ 2 chu kỳ xem có tiếp tục tăng"},
            {"id":"block",   "label":"Chặn toàn bộ traffic","type":"BLOCK","param":0,
             "desc":"DROP flow — dùng khi nghi ngờ tấn công"},
        ],
    },
    {
        "name":        "warn_bandwidth",
        "condition":   lambda speed, history: 10e6 <= speed < 20e6,
        "level":       "medium",
        "action_type": "reroute",
        "message":     lambda dpid, port, speed, **kw:
                       f"Port s{dpid}/{port} đang dùng {speed/1e6:.1f} Mbps — cần theo dõi",
        "root_cause":  lambda dpid, port, speed, **kw:
                       f"Băng thông {speed/1e6:.1f} Mbps trong vùng cảnh báo (10–20 Mbps). "
                       f"Nếu tiếp tục tăng trong 2–3 chu kỳ, cần can thiệp.",
        "actions":     lambda dpid, port, **kw: [
            {"id":"monitor", "label":"Tiếp tục theo dõi","type":"MONITOR","param":0,
             "desc":"Chờ thêm dữ liệu"},
            {"id":"qos_15",  "label":"Giới hạn phòng ngừa 15 Mbps","type":"QoS","param":15,
             "desc":"Giới hạn nhẹ để tránh vượt ngưỡng HIGH"},
        ],
    },
    {
        "name":        "zscore_anomaly",
        "condition":   lambda speed, history: (
            len(history) >= 5
            and statistics.stdev(history) > 0
            and abs(speed - statistics.mean(history)) / statistics.stdev(history) >= 2.5
        ),
        "level":       "medium",
        "action_type": "reroute",
        "message":     lambda dpid, port, speed, history, **kw:
                       f"Port s{dpid}/{port} bất thường thống kê "
                       f"(Z={abs(speed-statistics.mean(history))/statistics.stdev(history):.1f})",
        "root_cause":  lambda dpid, port, speed, history, **kw:
                       f"Tốc độ {speed/1e6:.1f} Mbps lệch "
                       f"{abs(speed-statistics.mean(history))/statistics.stdev(history):.1f} "
                       f"độ lệch chuẩn (TB={statistics.mean(history)/1e6:.1f} Mbps). "
                       f"Nguyên nhân: luồng mới, truyền file đột ngột, hoặc dấu hiệu tấn công sớm.",
        "actions":     lambda dpid, port, **kw: [
            {"id":"investigate","label":"Xem flow table","type":"INVESTIGATE","param":0,
             "desc":f"dump-flows s{dpid} để tìm luồng bất thường"},
            {"id":"qos_10",     "label":"Giới hạn phòng ngừa 10 Mbps","type":"QoS","param":10,
             "desc":"Ngăn leo thang nếu là tấn công"},
            {"id":"monitor",    "label":"Theo dõi thêm","type":"MONITOR","param":0,
             "desc":"Chờ thêm dữ liệu để xác nhận"},
        ],
    },
    {
        "name":        "sustained_high",
        "condition":   lambda speed, history: (
            len(history) >= 3 and all(s >= 15e6 for s in history[-3:])
        ),
        "level":       "high",
        "action_type": "block",
        "message":     lambda dpid, port, speed, **kw:
                       f"Port s{dpid}/{port} duy trì băng thông cao liên tục 3 chu kỳ",
        "root_cause":  lambda dpid, port, speed, history, **kw:
                       f"Băng thông trên 15 Mbps trong 3 chu kỳ liên tiếp "
                       f"({[round(s/1e6,1) for s in history[-3:]]} Mbps). "
                       f"Nguy cơ cao: DDoS, worm, hoặc ứng dụng bị lỗi gây flood.",
        "actions":     lambda dpid, port, **kw: [
            {"id":"block",  "label":"Chặn ngay","type":"BLOCK","param":0,
             "desc":f"DROP toàn bộ traffic port s{dpid}/{port}"},
            {"id":"qos_5",  "label":"Giới hạn khẩn cấp 5 Mbps","type":"QoS","param":5,
             "desc":"Hạn chế tối đa, giảm thiệt hại trong khi điều tra"},
            {"id":"qos_10", "label":"Giới hạn 10 Mbps","type":"QoS","param":10,
             "desc":"Cho traffic hợp lệ qua, chặn lưu lượng thừa"},
        ],
    },
]


class DecisionEngine:
    def __init__(self):
        self.speed_history: Dict[Tuple[int, int], List[float]] = {}

    async def run_once(self):
        pool   = await get_pool()

        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT dpid, port_no, AVG(speed_rx + speed_tx) AS avg_speed
                FROM port_stats
                WHERE timestamp >= NOW() - INTERVAL '60 seconds' AND port_no != 4294967294
                GROUP BY dpid, port_no
            """)

        new_recs = 0
        for row in rows:
            dpid    = row["dpid"]
            port_no = row["port_no"]
            speed   = float(row["avg_speed"] or 0)
            key     = (dpid, port_no)
            history = self.speed_history.setdefault(key, [])

            for rule in RULES:
                try:
                    if not rule["condition"](speed=speed, history=history):
                        continue

                    # Kiểm tra duplicate trong 120s
                    async with pool.acquire() as conn:
                        existing = await conn.fetchval("""
                            SELECT id FROM recommendations
                            WHERE dpid=$1 AND port_no=$2 AND action_type=$3
                              AND status='pending' AND created_at >= NOW() - INTERVAL '120 seconds'
                        """, dpid, port_no, rule["action_type"])
                    if existing:
                        continue

                    msg        = rule["message"](dpid=dpid, port=port_no, speed=speed, history=history)
                    root_cause = rule["root_cause"](dpid=dpid, port=port_no, speed=speed, history=history)
                    actions    = rule["actions"](dpid=dpid, port=port_no)

                    async with pool.acquire() as conn:
                        await conn.execute("""
                            INSERT INTO recommendations
                            (created_at,dpid,port_no,level,action_type,
                             message,root_cause,actions_json,status)
                            VALUES (NOW(),$1,$2,$3,$4,$5,$6,$7::jsonb,'pending')
                        """, dpid, port_no,
                             rule["level"], rule["action_type"],
                             msg, root_cause, json.dumps(actions, ensure_ascii=False))

                    print(f"  [GỢI Ý {rule['level']}] {msg}")
                    new_recs += 1

                except Exception as e:
                    print(f"  [ERR decision] {e}")

            history.append(speed)
            if len(history) > 20:
                history.pop(0)

        return new_recs

    async def loop(self, interval: int = 60):
        """Chạy vòng lặp nền — được spawn bởi FastAPI lifespan."""
        print("Decision Engine đang chạy (interval={}s)...".format(interval))
        while True:
            try:
                n = await self.run_once()
                if n:
                    print(f"  → Sinh {n} gợi ý mới")
            except Exception as e:
                print(f"  [ERR engine loop] {e}")
            await asyncio.sleep(interval)
