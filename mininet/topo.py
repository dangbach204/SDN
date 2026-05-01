"""
topo.py — Mininet topology chuỗi thẳng s1—s2—s3
Chạy: sudo -E .venv/bin/python mininet/topo.py
"""
import os

import requests
from dotenv import load_dotenv
from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController
from mininet.topo import Topo

load_dotenv()


class SDNTopo(Topo):
    def build(self):
        s1 = self.addSwitch('s1', protocols='OpenFlow13')
        s2 = self.addSwitch('s2', protocols='OpenFlow13')
        s3 = self.addSwitch('s3', protocols='OpenFlow13')

        # Uplink — port cố định
        self.addLink(s1, s2, port1=1, port2=1, bw=100, delay='2ms')
        self.addLink(s2, s3, port1=2, port2=1, bw=100, delay='2ms')

        # 4 host mỗi switch — 50 Mbps
        host_switch_map = [
            ('h1', s1), ('h2', s1), ('h3', s1),  ('h4', s1),
            ('h5', s2), ('h6', s2), ('h7', s2),  ('h8', s2),
            ('h9', s3), ('h10', s3),('h11', s3), ('h12', s3),
        ]
        for i, (host, sw) in enumerate(host_switch_map, 1):
            h = self.addHost(
                host,
                ip=f'10.0.0.{i}/24',
                mac=f'00:00:00:00:00:{i:02x}')
            self.addLink(h, sw, bw=50, delay='1ms')


def _reset_backend():
    """Báo FastAPI reset trạng thái mạng cũ sau mỗi lần Mininet khởi động."""
    fastapi_url = os.getenv("FASTAPI_URL", "http://127.0.0.1:8000")
    try:
        r = requests.post(f"{fastapi_url}/api/reset", timeout=5)
        data = r.json()
        dismissed = data.get("dismissed_recommendations", 0)
        print(f"[RESET] {data.get('msg', 'ok')} — dismissed {dismissed} recommendations")
    except requests.exceptions.ConnectionError:
        print("[RESET] FastAPI chưa sẵn sàng — bỏ qua reset (không ảnh hưởng Mininet)")
    except Exception as e:
        print(f"[RESET] Lỗi: {e}")


def _start_traffic_generators(net):
    """Start iperf servers and clients to generate background traffic."""
    hosts = [net.get(f'h{i}') for i in range(1, 13)]
    
    # Start iperf servers on all hosts (background)
    print("\n[TRAFFIC] Starting iperf servers on all hosts...")
    for h in hosts:
        h.cmd('iperf3 -s -D 2>/dev/null')
    
    # Start iperf clients generating traffic (random pairs)
    print("[TRAFFIC] Starting iperf clients to generate background traffic...")
    import random, threading, time
    
    def traffic_loop():
        while True:
            try:
                src = random.choice(hosts)
                dst = random.choice([h for h in hosts if h != src])
                # 30s at ~5 Mbps per client (non-blocking)
                src.cmd(f'timeout 30 iperf3 -c {dst.IP()} -b 5M -Z 2>/dev/null &')
                time.sleep(5)
            except:
                pass
    
    traffic_thread = threading.Thread(target=traffic_loop, daemon=True)
    traffic_thread.start()


def run():
    setLogLevel('info')
    topo = SDNTopo()
    net  = Mininet(
        topo=topo,
        controller=RemoteController('c0', ip='127.0.0.1', port=6653),
        switch=OVSSwitch,
        link=TCLink,
        autoSetMacs=False,
    )
    net.start()

    # Tự động reset trạng thái DB sau khi mạng được tạo lại
    _reset_backend()

    print("\n=== Ping test (h1 → h2) ===")
    h1, h2 = net.get('h1'), net.get('h2')
    print(h1.cmd(f'ping -c 3 -W 1 {h2.IP()}'))

    # Start background traffic generators
    ENABLE_BG_TRAFFIC = False
    if ENABLE_BG_TRAFFIC:
        _start_traffic_generators(net)
    print("[TRAFFIC] Background traffic started — monitor should see bytes increase\n")

    CLI(net)
    net.stop()


if __name__ == '__main__':
    run()