#!/usr/bin/env bash
# start.sh — Khởi động toàn bộ hệ thống SDN Monitor
# Dùng trong môi trường dev/lab với Mininet
#
# Yêu cầu:
#   - File .env đã được tạo từ .env.example
#   - Mininet, Ryu, Python 3.9+, Node.js 18+ đã được cài
#   - Đang chạy với quyền sudo (cho Mininet)
#
# Cách dùng: chmod +x start.sh && sudo ./start.sh

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Load .env
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
    echo "[ENV] Đã load .env"
else
    echo "[WARN] Không tìm thấy .env — copy từ .env.example trước"
    exit 1
fi

# Kiểm tra DATABASE_URL
if [ -z "$DATABASE_URL" ]; then
    echo "[ERR] DATABASE_URL chưa được đặt trong .env"
    exit 1
fi

echo ""
echo "╔══════════════════════════════════════╗"
echo "║      SDN Traffic Monitor — Start     ║"
echo "╚══════════════════════════════════════╝"
echo ""

# ── 1. Dọn dẹp tiến trình cũ ──────────────────────────────────────
echo "[1/5] Dọn dẹp tiến trình cũ..."
sudo mn -c 2>/dev/null || true
pkill -f "ryu-manager"  2>/dev/null || true
pkill -f "uvicorn"      2>/dev/null || true
sleep 1

# ── 2. Cài dependencies nếu chưa có ──────────────────────────────
echo "[2/5] Kiểm tra Python dependencies..."
pip install -q -r backend/requirements.txt
pip install -q -r ryu/requirements.txt

echo "[2/5] Kiểm tra Node.js dependencies..."
(cd frontend && npm install --silent)

# ── 3. Ryu Controller ─────────────────────────────────────────────
echo "[3/5] Khởi động Ryu Controller (port 6653 + ofctl_rest 8080)..."
ryu-manager ryu/monitor.py ryu.app.ofctl_rest \
    --ofp-tcp-listen-port 6653 \
    --wsapi-port 8080 \
    > /tmp/ryu.log 2>&1 &
RYU_PID=$!
echo "      PID=$RYU_PID, log: /tmp/ryu.log"
sleep 3

# ── 4. FastAPI Backend ────────────────────────────────────────────
echo "[4/5] Khởi động FastAPI backend (port 8000)..."
uvicorn backend.main:app --host 0.0.0.0 --port 8000 \
    > /tmp/fastapi.log 2>&1 &
FASTAPI_PID=$!
echo "      PID=$FASTAPI_PID, log: /tmp/fastapi.log"
sleep 2

# Kiểm tra FastAPI đã sẵn sàng
for i in {1..10}; do
    if curl -sf http://127.0.0.1:8000/api/health > /dev/null 2>&1; then
        echo "      ✓ FastAPI sẵn sàng"
        break
    fi
    sleep 1
done

# ── 5. React Frontend ─────────────────────────────────────────────
echo "[5/5] Khởi động React frontend (port 5173)..."
(cd frontend && npm run dev > /tmp/react.log 2>&1) &
REACT_PID=$!
echo "      PID=$REACT_PID, log: /tmp/react.log"
sleep 2

# ── 6. Mininet (chạy cuối để có Ryu sẵn) ─────────────────────────
echo ""
echo "[6/5] Khởi động Mininet topology..."
echo "      (Nhấn Ctrl+D hoặc 'exit' trong CLI để thoát)"
echo ""
sudo python mininet/topo.py

# ── Cleanup khi thoát ─────────────────────────────────────────────
echo ""
echo "[STOP] Dừng tất cả tiến trình..."
kill $RYU_PID    2>/dev/null || true
kill $FASTAPI_PID 2>/dev/null || true
kill $REACT_PID  2>/dev/null || true
sudo mn -c 2>/dev/null || true
echo "Đã dừng tất cả."
