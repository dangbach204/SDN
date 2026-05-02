import { useEffect, useRef, useState } from "react"
import { Chart, registerables } from "chart.js"
import { API } from "../App"

Chart.register(...registerables)

const SWITCHES = ["1", "2", "3"]

export default function BandwidthChart({ portStats }) {
  const canvasRef  = useRef(null)
  const chartRef   = useRef(null)
  const [selSw,    setSelSw]    = useState("1")
  const [selPort,  setSelPort]  = useState(null)  // port_no number | null = all ports of switch
  const [loading,  setLoading]  = useState(false)

  // Lấy danh sách port của switch đang chọn
  const portsOfSw = portStats
    .filter(r => String(r.dpid) === selSw)
    .map(r => r.port_no)
    .sort((a, b) => a - b)

  useEffect(() => {
    // Khi switch thay đổi, reset port selection
    if (portsOfSw.length > 0 && !portsOfSw.includes(selPort)) {
      setSelPort(portsOfSw[0] ?? null)
    }
  }, [selSw, portStats])

  useEffect(() => {
    if (!canvasRef.current) return
    if (chartRef.current) chartRef.current.destroy()

    chartRef.current = new Chart(canvasRef.current, {
      type: "line",
      data: { labels: [], datasets: [] },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 300 },
        plugins: {
          legend: { position: "top", labels: { font: { family: "DM Sans", size: 11 } } },
          tooltip: {
            callbacks: {
              label: ctx => ` ${ctx.dataset.label}: ${ctx.parsed.y.toFixed(2)} Mbps`
            }
          }
        },
        scales: {
          x: {
            ticks: { font: { family: "DM Mono", size: 10 }, maxRotation: 45 },
            grid:  { color: "rgba(0,0,0,0.05)" }
          },
          y: {
            title: { display: true, text: "Mbps", font: { size: 11 } },
            ticks: {
              font: { family: "DM Mono", size: 10 },
              callback: function(value) {
                if (Math.abs(value) < 0.0001) return 0;
                return value;
              }
            },
            grid:  { color: "rgba(0,0,0,0.05)" },
            beginAtZero: true
          }
        }
      }
    })
    return () => chartRef.current?.destroy()
  }, [])

  // Fetch history khi switch/port thay đổi hoặc khi dashboard refresh dữ liệu nền
  useEffect(() => {
    if (!selPort || !chartRef.current) return
    setLoading(true)
    let cancelled = false

    fetch(`${API}/api/history/${selSw}/${selPort}`)
      .then(r => r.json())
      .then(data => {
        if (cancelled || !chartRef.current) return
        const labels = data.map(d =>
          new Date(d.timestamp * 1000).toLocaleTimeString("vi", { hour:"2-digit", minute:"2-digit", second:"2-digit" }))
        chartRef.current.data = {
          labels,
          datasets: [
            {
              label:           `Port ${selPort} RX`,
              data:            data.map(d => (d.speed_rx || 0) / 1e6),
              borderColor:     "#4a7fe0",
              backgroundColor: "rgba(74,127,224,0.08)",
              borderWidth:     2,
              pointRadius:     3,
              tension:         0.3,
              fill:            true,
            },
            {
              label:           `Port ${selPort} TX`,
              data:            data.map(d => (d.speed_tx || 0) / 1e6),
              borderColor:     "#8b5cf6",
              backgroundColor: "rgba(139,92,246,0.06)",
              borderWidth:     2,
              pointRadius:     3,
              tension:         0.3,
              fill:            false,
            },
          ]
        }
        chartRef.current.update()
      })
      .catch(() => {})
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [selSw, selPort, portStats])

  return (
    <div className="card">
      <div className="card-header">
        <div>
          <div className="card-title">Biểu đồ băng thông</div>
          <div className="card-sub">
            {selPort ? `s${selSw} · port ${selPort} · 20 điểm gần nhất` : "Chọn port"}
          </div>
        </div>
        <div style={{ display:"flex", gap:8, alignItems:"center", flexWrap:"wrap" }}>
          {/* Switch tabs */}
          <div className="sw-tabs">
            {SWITCHES.map(sw => (
              <button key={sw}
                className={`sw-tab ${selSw===sw?"active":""}`}
                onClick={() => setSelSw(sw)}>S{sw}</button>
            ))}
          </div>
          {/* Port tabs */}
          <div className="sw-tabs">
            {portsOfSw.map(p => (
              <button key={p}
                className={`sw-tab ${selPort===p?"active":""}`}
                onClick={() => setSelPort(p)}>eth{p}</button>
            ))}
          </div>
        </div>
      </div>
      <div style={{ position:"relative", height:220 }}>
        {loading && (
          <div style={{
            position:"absolute", inset:0,
            display:"flex", alignItems:"center", justifyContent:"center",
            fontSize:12, color:"var(--text3)"
          }}>Đang tải...</div>
        )}
        <canvas ref={canvasRef} />
      </div>
    </div>
  )
}
