"""
topo.py — Mininet topology
Chạy: sudo python mininet/topo.py

Thay đổi so với bản cũ:
  - Bỏ lệnh 'tc qdisc add dev {sw.name} root htb r2q 100' sai
    (sw.name là 's1','s2','s3' — không phải tên interface)
  - QoS được áp dụng từ FastAPI qua lệnh tc trên interface cụ thể (s1-eth1, ...)
  - r2q warning được xử lý bằng cách set burst lớn hơn khi cần
"""
from mininet.topo import Topo
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.cli import CLI
from mininet.log import setLogLevel
from mininet.link import TCLink


class SDNTopo(Topo):
    def build(self):
        # 3 switches kết nối thành chuỗi: s1 — s2 — s3
        s1 = self.addSwitch('s1', protocols='OpenFlow13')
        s2 = self.addSwitch('s2', protocols='OpenFlow13')
        s3 = self.addSwitch('s3', protocols='OpenFlow13')

        # Uplink giữa các switch: 100 Mbps
        self.addLink(s1, s2, bw=100, delay='2ms')
        self.addLink(s2, s3, bw=100, delay='2ms')

        # 4 host mỗi switch, băng thông 50 Mbps mỗi link
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

    # Kiểm tra kết nối cơ bản
    print("\n=== Ping test (h1 → h2) ===")
    h1, h2 = net.get('h1'), net.get('h2')
    result = h1.cmd(f'ping -c 2 -W 1 {h2.IP()}')
    print(result)

    CLI(net)
    net.stop()


if __name__ == '__main__':
    run()
