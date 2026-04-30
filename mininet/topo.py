"""
topo.py — Mininet topology chuỗi thẳng s1—s2—s3
Chạy: sudo python mininet/topo.py

Topology hiện tại: chuỗi thẳng (không có đường s1-s3)
  s1 — s2 — s3

Port mapping cố định:
  s1-eth1 ↔ s2-eth1  (uplink s1—s2, 100 Mbps)
  s2-eth2 ↔ s3-eth1  (uplink s2—s3, 100 Mbps)
  s1-eth3..eth6  → h1..h4  (host ports, 50 Mbps)
  s2-eth3..eth6  → h5..h8
  s3-eth2..eth5  → h9..h12

Thay đổi:
  - Bỏ stp=True: xung đột với Ryu OpenFlow controller
  - Link s1-s3 đã comment out (không có loop → không cần loop prevention)
"""
from mininet.topo import Topo
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.cli import CLI
from mininet.log import setLogLevel
from mininet.link import TCLink


class SDNTopo(Topo):
    def build(self):
        # Topology chuỗi thẳng s1-s2-s3
        s1 = self.addSwitch('s1', protocols='OpenFlow13')
        s2 = self.addSwitch('s2', protocols='OpenFlow13')
        s3 = self.addSwitch('s3', protocols='OpenFlow13')

        # Uplink giữa các switch — port cố định
        # s1-eth1 ↔ s2-eth1
        self.addLink(s1, s2, port1=1, port2=1, bw=100, delay='2ms')
        # s2-eth2 ↔ s3-eth1
        self.addLink(s2, s3, port1=2, port2=1, bw=100, delay='2ms')

        # 4 host mỗi switch — 50 Mbps mỗi link
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

    print("\n=== Ping test (h1 → h2) ===")
    h1, h2 = net.get('h1'), net.get('h2')
    print(h1.cmd(f'ping -c 3 -W 1 {h2.IP()}'))

    CLI(net)
    net.stop()


if __name__ == '__main__':
    run()