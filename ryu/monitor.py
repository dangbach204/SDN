"""
monitor.py — Ryu Traffic Monitor
Chạy:          ryu-manager ryu/monitor.py ryu.app.ofctl_rest --ofp-tcp-listen-port 6653 --wsapi-port 8080
hoặc đơn giản: ryu-manager ryu/monitor.py

"""

import json
import os
import statistics
import time

import requests
from dotenv import load_dotenv
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.lib import hub
from ryu.lib.packet import ethernet, packet
from ryu.ofproto import ofproto_v1_3

load_dotenv()

# Cấu hình
FASTAPI_URL      = os.getenv("FASTAPI_URL", "http://127.0.0.1:8000")
POLL_INTERVAL    = int(os.getenv("POLL_INTERVAL", "10"))  # đổi 10 khi debug
THRESHOLD_HIGH   = 20e6    # bps — 20 Mbps
THRESHOLD_WARN   = 10e6    # bps — 10 Mbps
ZSCORE_THRESHOLD = 2.5
HISTORY_MAX_LEN  = 20
LOCAL_PORT       = 0xFFFFFFFE


# HTTP helper
def _post(path: str, payload: dict) -> None:
    """POST lên FastAPI, bỏ qua lỗi kết nối."""
    try:
        requests.post(f"{FASTAPI_URL}{path}", json=payload, timeout=5)
    except Exception as e:
        print(f"  [WARN] POST {path} thất bại: {e}")


# Main Ryu App
class TrafficMonitor(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mac_to_port   = {}
        self.datapaths     = {}
        self.prev_stats    = {}  # {(dpid, port): {"rx_bytes": ..., "tx_bytes": ..., "tx_packets": ..., "rx_packets": ..., "timestamp": ...}}
        self.speed_history = {}  # {(dpid, port): [speed, speed, ...]}
        self.prev_speed    = {}  # {(dpid, port): speed} — for detecting restart after idle
        self.monitor_thread = hub.spawn(self._monitor_loop)

    # Switch connect
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        self.datapaths[datapath.id] = datapath
        self._install_table_miss(datapath)
        self.logger.info("Switch s%s connected", datapath.id)

    def _install_table_miss(self, datapath):
        """Flow rule priority=0: gửi unknown packet lên controller."""
        ofproto = datapath.ofproto
        parser  = datapath.ofproto_parser
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod  = parser.OFPFlowMod(datapath=datapath, priority=0,
                                 match=parser.OFPMatch(), instructions=inst)
        datapath.send_msg(mod)

    def _add_flow(self, datapath, priority, match, actions,
                  idle_timeout=0, hard_timeout=0):
        ofproto = datapath.ofproto
        parser  = datapath.ofproto_parser
        inst    = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod     = parser.OFPFlowMod(
            datapath=datapath, priority=priority,
            match=match, instructions=inst,
            idle_timeout=idle_timeout, hard_timeout=hard_timeout,
        )
        datapath.send_msg(mod)

    # Packet-in — learning switch
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg      = ev.msg
        datapath = msg.datapath
        ofproto  = datapath.ofproto
        parser   = datapath.ofproto_parser
        in_port  = msg.match['in_port']
        dpid     = datapath.id

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None:
            return

        dst, src = eth.dst, eth.src
        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][src] = in_port
        out_port = self.mac_to_port[dpid].get(dst, ofproto.OFPP_FLOOD)
        actions  = [parser.OFPActionOutput(out_port)]

        if out_port != ofproto.OFPP_FLOOD:
            match = parser.OFPMatch(in_port=in_port, eth_dst=dst, eth_src=src)
            # idle_timeout=30: xóa flow sau 30s idle, tránh flow table đầy
            self._add_flow(datapath, priority=1, match=match,
                           actions=actions, idle_timeout=30)

        data = msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
        out  = parser.OFPPacketOut(
            datapath=datapath, buffer_id=msg.buffer_id,
            in_port=in_port, actions=actions, data=data)
        datapath.send_msg(out)

    # Monitor loop
    def _monitor_loop(self):
        while True:
            for dp in list(self.datapaths.values()):
                self._request_port_stats(dp)
                self._request_flow_stats(dp)
            hub.sleep(POLL_INTERVAL)

    def _request_port_stats(self, datapath):
        ofproto = datapath.ofproto
        parser  = datapath.ofproto_parser
        req = parser.OFPPortStatsRequest(datapath, 0, ofproto.OFPP_ANY)
        datapath.send_msg(req)

    def _request_flow_stats(self, datapath):
        parser = datapath.ofproto_parser
        req    = parser.OFPFlowStatsRequest(datapath)
        datapath.send_msg(req)

    def _get_port_capacity(self, dpid: int, port: int) -> float:
        """Port capacity (bps) — tương tự decision_engine.py"""
        # SW1
        if dpid == 1:
            if port == 1:
                return 100e6  # uplink
            elif 2 <= port <= 5:
                return 50e6   # host

        # SW2
        elif dpid == 2:
            if port in [1, 2]:
                return 100e6  # uplink
            elif 3 <= port <= 6:
                return 50e6   # host

        # SW3
        elif dpid == 3:
            if port == 1:
                return 100e6  # uplink
            elif 2 <= port <= 5:
                return 50e6   # host

        # fallback
        return 100e6

    # Port stats reply
    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply_handler(self, ev):
        dpid = ev.msg.datapath.id
        now  = time.time()
        self.prev_stats.setdefault(dpid, {})

        rows = []
        for stat in ev.msg.body:
            port_no = stat.port_no
            if port_no == LOCAL_PORT:
                continue

            rx, tx = stat.rx_bytes, stat.tx_bytes
            rx_packets, tx_packets = stat.rx_packets, stat.tx_packets
            
            speed_rx = speed_tx = loss = 0.0
            key = (dpid, port_no)
            
            # ── Tính delta từ previous state ─────────────────────────────
            if key in self.prev_stats[dpid]:
                prev = self.prev_stats[dpid][key]
                prev_rx = prev["rx_bytes"]
                prev_tx = prev["tx_bytes"]
                prev_rx_packets = prev["rx_packets"]
                prev_tx_packets = prev["tx_packets"]
                prev_t = prev["timestamp"]
                
                dt = now - prev_t
                if dt > 0:
                    # ── Xử lý counter reset / overflow ──────────────────
                    delta_rx = rx - prev_rx
                    delta_tx = tx - prev_tx
                    
                    # Nếu counter bị reset (âm) → bỏ sample này
                    if delta_rx < 0 or delta_tx < 0:
                        self.prev_stats[dpid][key] = {
                            "rx_bytes": rx, "tx_bytes": tx,
                            "rx_packets": rx_packets, "tx_packets": tx_packets,
                            "timestamp": now
                        }
                        continue
                    
                    # ── Tính speed (bps) ──────────────────────────────
                    speed_rx = delta_rx * 8 / dt
                    speed_tx = delta_tx * 8 / dt
                    
                    # ── Clamp giá trị bất thường (vượt 120% capacity) ──
                    capacity = self._get_port_capacity(dpid, port_no)
                    if speed_rx > capacity * 1.2:
                        speed_rx = capacity
                    if speed_tx > capacity * 1.2:
                        speed_tx = capacity
                    
                    # ── Ignore đúng 1 sample đầu sau idle (tránh spike) ──
                    prev_spd = self.prev_speed.get(key, None)
                    if prev_spd == 0 and (speed_rx + speed_tx) > 0:
                        self.prev_speed[key] = speed_rx + speed_tx
                        continue

                    # ── Smoothing: moving average 3 samples ─────────────
                    history = self.speed_history.setdefault(key, [])
                    speed = max(speed_rx, speed_tx)

                    history.append(speed)
                    if len(history) > 3:
                        history.pop(0)

                    # Lấy average của 3 sample gần nhất
                    if len(history) >= 2:
                        speed = sum(history) / len(history)

                    # ── Tính packet loss ─────────────────────────────────
                    delta_rx_packets = rx_packets - prev_rx_packets
                    delta_tx_packets = tx_packets - prev_tx_packets

                    if delta_tx_packets > 0:
                        packet_loss = (delta_tx_packets - delta_rx_packets) / delta_tx_packets
                        loss = max(0, min(packet_loss * 100, 100))  # Clamp 0-100%

                    rows.append({
                        "timestamp": now,
                        "dpid":      dpid,
                        "port_no":   port_no,
                        "rx_bytes":  rx,
                        "tx_bytes":  tx,
                        "speed_rx":  speed_rx,
                        "speed_tx":  speed_tx,
                        "loss":      loss,
                    })

                    self._check_anomaly(dpid, port_no, speed, now)
            
            # Lưu current state
            self.prev_stats[dpid][key] = {
                "rx_bytes": rx, "tx_bytes": tx,
                "rx_packets": rx_packets, "tx_packets": tx_packets,
                "timestamp": now
            }
            
            # Cập nhật prev_speed để detect idle
            self.prev_speed[key] = speed_rx + speed_tx

        print(f"[DEBUG] rows={len(rows)} dpid={dpid}")
        if not rows:
            print(f"[WARN] No rows generated for s{dpid}")
        else:
            print(f"[POST] sending {len(rows)} rows")
            _post("/internal/port_stats", {"rows": rows})

        print(f"\n[Port Stats] s{dpid} — {time.strftime('%H:%M:%S')} — {len(rows)} ports")

    # Flow stats reply
    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def flow_stats_reply_handler(self, ev):
        dpid = ev.msg.datapath.id
        now  = time.time()

        rows = []
        for stat in ev.msg.body:
            if stat.priority == 0:
                continue

            # FIX: json.dumps() thay vì str() — để PostgreSQL ->> query được
            match_dict = {}
            for key, value in stat.match._fields2:
                match_dict[key] = value.hex() if isinstance(value, (bytes, bytearray)) else value

            rows.append({
                "timestamp": now,
                "dpid":      dpid,
                "priority":  stat.priority,
                "packets":   stat.packet_count,
                "bytes":     stat.byte_count,
                "duration":  stat.duration_sec,
                "match_str": json.dumps(match_dict),
            })

        if rows:
            _post("/internal/flow_stats", {"rows": rows})
        print(f"  [Flow Stats] s{dpid}: {len(rows)} flows")

    # Anomaly detection
    def _check_anomaly(self, dpid, port_no, speed, now):
        """Detect anomaly từ smoothed speed"""
        key = (dpid, port_no)
        history = self.speed_history.setdefault(key, [])

        anomaly = None
        if speed >= THRESHOLD_HIGH:
            anomaly = ("HIGH", speed, THRESHOLD_HIGH,
                       f"s{dpid} port {port_no}: {speed/1e6:.1f} Mbps vượt ngưỡng cao")
        elif speed >= THRESHOLD_WARN:
            anomaly = ("WARN", speed, THRESHOLD_WARN,
                       f"s{dpid} port {port_no}: {speed/1e6:.1f} Mbps vượt ngưỡng cảnh báo")
        elif len(history) >= 5:
            mean = statistics.mean(history)
            std  = statistics.stdev(history)
            if std > 0:
                zscore = abs(speed - mean) / std
                if zscore >= ZSCORE_THRESHOLD:
                    anomaly = ("ZSCORE", speed, mean,
                               f"s{dpid} port {port_no}: Z-score={zscore:.2f} bất thường")

        if anomaly:
            level, value, threshold, msg = anomaly
            print(f"  *** [{level}] {msg} ***")
            _post("/internal/anomalies", {
                "timestamp": now,
                "dpid":      dpid,
                "port_no":   port_no,
                "metric":    "bandwidth",
                "value":     value,
                "threshold": threshold,
                "level":     level,
                "message":   msg,
            })

        # history đã được update trong port_stats_reply_handler
