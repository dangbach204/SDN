# SDN Traffic Monitor

Hệ thống giám sát mạng SDN sử dụng **Mininet + Ryu + FastAPI + React + Neon PostgreSQL**.

## Kiến trúc

```
React (port 5173)
    │  GET /api/...  POST /api/recommendations/{id}/choose
    ▼
FastAPI (port 8000)
    │  asyncpg                    │  HTTP POST /stats/flowentry/add
    ▼                             ▼
Neon PostgreSQL          Ryu ofctl_rest (port 8080)
    ▲                             │  OpenFlow 1.3
    │  POST /internal/...         ▼
Ryu monitor.py ◄────── Mininet (3 switch, 12 host)
    (port 6653)
```

## Cấu trúc thư mục

```
project/
├── start.sh                 ← Script khởi động toàn bộ
│
├── backend/
|   ├── .env                 ← Database URL
│   ├── main.py              ← FastAPI app entry point
│   ├── database.py          ← asyncpg pool + Neon init schema
│   ├── decision_engine.py   ← Phát hiện bất thường, sinh recommendations
│   └── routers/
│       ├── stats.py         ← GET /api/summary, /port_stats, /history, /utilization
│       ├── anomalies.py     ← GET /api/anomalies
│       ├── recommendations.py ← GET/POST /api/recommendations
│       └── internal.py      ← POST /internal/port_stats, /flow_stats, /anomalies
│
├── ryu/
│   └── monitor.py           ← Ryu app: thu thập stats, POST lên FastAPI
│
├── mininet/
│   └── topo.py              ← 3 switch (s1,s2,s3), 12 host (h1–h12)
│
└── frontend/
    ├── package.json
    ├── vite.config.js
    ├── index.html
    └── src/
        ├── main.jsx
        ├── App.jsx           ← Root component, fetch tất cả API
        ├── index.css         ← Design system
        └── components/
            ├── Summary.jsx
            ├── BandwidthChart.jsx
            ├── UtilizationBars.jsx
            ├── PortTable.jsx
            ├── AnomalyTable.jsx
            ├── Topology.jsx
            └── Recommendations.jsx
```

## Cài đặt

### 1. Yêu cầu hệ thống

```bash
# Ubuntu 20.04/22.04
sudo apt install python3-pip nodejs npm mininet openvswitch-switch

# Python packages ()
pip install requests python-dotenv       # cho Ryu
pip install fastapi uvicorn asyncpg httpx python-dotenv  # cho FastAPI
pip install ryu # (yêu cầu phải cài đặt trong môi trường ảo python 3.9)
```

### 2. Cấu hình Neon PostgreSQL

1. Tạo tài khoản tại https://neon.tech (free tier đủ dùng)
2. Tạo project mới → copy **Connection string**
3. Dán vào file .env

### 3. Chạy hệ thống (khoan thử)

**Cách 1 — Script tự động:**

```bash
chmod +x start.sh
sudo ./start.sh
```

**Cách 2 — Từng terminal:**

```bash
# Đối với Ryu và backend server, cần kích hoạt môi trường ảo rồi mới chạy
# Nếu chưa tạo folder môi trường ảo thì làm theo hướng dẫn sau:
cd backend
python3.9 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install ryu
pip uninstall eventlet
pip install eventlet==0.30.2

# Terminal 1 — Ryu
# Đảm bảo đã kích hoạt môi trường ảo
# Nếu đang ở trong thư mục backend thì cd ra ngoài bằng:
cd ..
ryu-manager ryu/monitor.py ryu.app.ofctl_rest --ofp-tcp-listen-port 6653 --wsapi-port 8080

# Terminal 2 — Mininet
sudo python3 mininet/topo.py

# Terminal 3 — Backend server và FastAPI
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
# hoặc
uvicorn main:app --reload

# Terminal 4 — Frontend
cd frontend && npm install && npm run dev

```

Mở trình duyệt: **http://localhost:5173**

## Test tạo traffic

Trong Mininet CLI:

```
mininet> h1 iperf -s &
mininet> h2 iperf -c 10.0.0.1 -b 30M -t 60
mininet> h3 ping 10.0.0.4 -i 0.1
```

## API Endpoints

| Method | Path                                | Mô tả                         |
| ------ | ----------------------------------- | ----------------------------- |
| GET    | `/api/health`                       | Kiểm tra backend              |
| GET    | `/api/summary`                      | Tổng quan hệ thống            |
| GET    | `/api/port_stats`                   | Stats các port (60s gần nhất) |
| GET    | `/api/history/{dpid}/{port_no}`     | Lịch sử băng thông            |
| GET    | `/api/utilization`                  | Link utilization %            |
| GET    | `/api/anomalies`                    | Danh sách cảnh báo            |
| GET    | `/api/recommendations`              | Danh sách khuyến nghị         |
| POST   | `/api/recommendations/{id}/choose`  | Áp dụng action đã chọn        |
| POST   | `/api/recommendations/{id}/dismiss` | Bỏ qua khuyến nghị            |
| POST   | `/internal/port_stats`              | Ryu → FastAPI: đẩy port stats |
| POST   | `/internal/flow_stats`              | Ryu → FastAPI: đẩy flow stats |
| POST   | `/internal/anomalies`               | Ryu → FastAPI: đẩy anomaly    |

## Ngưỡng cảnh báo (decision_engine.py)

| Rule           | Điều kiện                          | Level  | Action mặc định |
| -------------- | ---------------------------------- | ------ | --------------- |
| high_bandwidth | speed ≥ 20 Mbps                    | HIGH   | QoS             |
| warn_bandwidth | 10 ≤ speed < 20 Mbps               | WARN   | MONITOR         |
| zscore_anomaly | \|Z-score\| ≥ 2.5                  | ZSCORE | INVESTIGATE     |
| sustained_high | ≥ 15 Mbps trong 3 chu kỳ liên tiếp | HIGH   | BLOCK           |

## Troubleshooting

**Ryu không kết nối được Mininet:**

```bash
sudo mn -c  # dọn dẹp topology cũ
sudo service openvswitch-switch restart
```

**FastAPI không kết nối Neon:**

- Kiểm tra `DATABASE_URL` trong `.env`
- Đảm bảo `?sslmode=require` ở cuối connection string

**React không gọi được API:**

- Kiểm tra FastAPI đang chạy: `curl http://localhost:8000/api/health`
- CORS đã được enable cho tất cả origins trong development
