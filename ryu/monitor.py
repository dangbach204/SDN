"""
monitor.py — Ryu app
Chạy: ryu-manager ryu/monitor.py ryu.app.ofctl_rest --ofp-tcp-listen-port 6653

Thay đổi so với bản cũ:
  1. Bỏ Ryu WSGI / MonitorAPI — FastAPI là backend riêng
  2. Sửa duplicate hub.spawn(_monitor_loop)
  3. Ghi dữ liệu lên FastAPI (POST /internal/...) thay vì SQLite
  4. POLL_INTERVAL = 60s theo yêu cầu (có thể đổi lại 10s khi test)
"""

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet
from ryu.lib import hub

import time
import statistics
import os
import requests
from dotenv import load_dotenv

load_dotenv()

# ── Cấu hình ────────────────────────────────────────────────────────
FASTAPI_URL      = os.getenv("FASTAPI_URL", "http://127.0.0.1:8000")
POLL_INTERVAL    = 60          # giây — đổi thành 10 khi debug
THRESHOLD_HIGH   = 20 * 1e6   # bps
THRESHOLD_WARN   = 10 * 1e6
ZSCORE_THRESHOLD = 2.5


def _post(path: str, payload: dict):
    """Gửi dữ liệu lên FastAPI backend, bỏ qua lỗi kết nối."""
    try:
        requests.post(f"{FASTAPI_URL}{path}", json=payload, timeout=5)
    except Exception as e:
        print(f"  [WARN] POST {path} thất bại: {e}")


class TrafficMonitor(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mac_to_port   = {}
        self.datapaths     = {}
        self.prev_bytes    = {}
        self.speed_history = {}
        # ── FIX: chỉ spawn một lần ──────────────────────────────────
        self.monitor_thread = hub.spawn(self._monitor_loop)

    # ── Switch connect ───────────────────────────────────────────────
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        self.datapaths[datapath.id] = datapath
        self._install_table_miss(datapath)
        self.logger.info("Switch s%s connected", datapath.id)

    def _install_table_miss(self, datapath):
        ofproto = datapath.ofproto
        parser  = datapath.ofproto_parser
        match   = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod  = parser.OFPFlowMod(datapath=datapath, priority=0,
                                 match=match, instructions=inst)
        datapath.send_msg(mod)

    def _add_flow(self, datapath, priority, match, actions):
        ofproto = datapath.ofproto
        parser  = datapath.ofproto_parser
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod  = parser.OFPFlowMod(datapath=datapath, priority=priority,
                                 match=match, instructions=inst)
        datapath.send_msg(mod)

    # ── Packet-in (learning switch) ──────────────────────────────────
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
            self._add_flow(datapath, 1, match, actions)
        data = msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
        out  = parser.OFPPacketOut(
            datapath=datapath, buffer_id=msg.buffer_id,
            in_port=in_port, actions=actions, data=data)
        datapath.send_msg(out)

    # ── Monitor loop ─────────────────────────────────────────────────
    def _monitor_loop(self):
        while True:
            for dp in list(self.datapaths.values()):
                self._request_port_stats(dp)
                self._request_flow_stats(dp)
            hub.sleep(POLL_INTERVAL)

    def _request_port_stats(self, datapath):
        parser  = datapath.ofproto_parser
        ofproto = datapath.ofproto
        req = parser.OFPPortStatsRequest(datapath, 0, ofproto.OFPP_ANY)
        datapath.send_msg(req)

    def _request_flow_stats(self, datapath):
        parser = datapath.ofproto_parser
        req = parser.OFPFlowStatsRequest(datapath)
        datapath.send_msg(req)

    # ── Port stats reply ─────────────────────────────────────────────
    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply_handler(self, ev):
        dpid = ev.msg.datapath.id
        now  = time.time()
        self.prev_bytes.setdefault(dpid, {})

        rows = []
        for stat in ev.msg.body:
            port_no = stat.port_no
            if port_no == 0xFFFFFFFE:   # LOCAL port — bỏ qua
                continue
            rx, tx = stat.rx_bytes, stat.tx_bytes
            speed_rx = speed_tx = 0.0
            if port_no in self.prev_bytes[dpid]:
                prev_rx, prev_tx, prev_t = self.prev_bytes[dpid][port_no]
                dt = now - prev_t
                if dt > 0:
                    speed_rx = (rx - prev_rx) * 8 / dt
                    speed_tx = (tx - prev_tx) * 8 / dt
            self.prev_bytes[dpid][port_no] = (rx, tx, now)

            rows.append({
                "timestamp": now,
                "dpid":     dpid,
                "port_no":  port_no,
                "rx_bytes": rx,
                "tx_bytes": tx,
                "speed_rx": speed_rx,
                "speed_tx": speed_tx,
            })

            self._check_anomaly(dpid, port_no, speed_rx, speed_tx, now)

        if rows:
            _post("/internal/port_stats", {"rows": rows})

        print(f"\n[Port Stats] s{dpid} — {time.strftime('%H:%M:%S')} — {len(rows)} ports")

    # ── Flow stats reply ─────────────────────────────────────────────
    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def flow_stats_reply_handler(self, ev):
        dpid = ev.msg.datapath.id
        now  = time.time()
        rows = []
        for stat in ev.msg.body:
            if stat.priority == 0:
                continue
            match_fields = {k: v for k, v in stat.match._fields2}
            rows.append({
                "timestamp": now,
                "dpid":      dpid,
                "priority":  stat.priority,
                "packets":   stat.packet_count,
                "bytes":     stat.byte_count,
                "duration":  stat.duration_sec,
                "match_str": str(match_fields),
            })
        if rows:
            _post("/internal/flow_stats", {"rows": rows})
        print(f"  [Flow Stats] s{dpid}: {len(rows)} flows")

    # ── Anomaly detection ────────────────────────────────────────────
    def _check_anomaly(self, dpid, port_no, speed_rx, speed_tx, now):
        key     = (dpid, port_no)
        history = self.speed_history.setdefault(key, [])
        speed   = max(speed_rx, speed_tx)

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

        history.append(speed)
        if len(history) > 20:
            history.pop(0)
