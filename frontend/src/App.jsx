import { useState, useEffect, useCallback } from "react"
import Summary from "./components/Summary"
import BandwidthChart from "./components/BandwidthChart"
import UtilizationBars from "./components/UtilizationBars"
import PortTable from "./components/PortTable"
import AnomalyTable from "./components/AnomalyTable"
import Recommendations from "./components/Recommendations"
import Topology from "./components/Topology"

export const API = import.meta.env.VITE_API_URL || "http://127.0.0.1:8000"

const REFRESH_INTERVAL = 60_000  // 1 phút — khớp với POLL_INTERVAL của Ryu

export default function App() {
  const [summary,    setSummary]    = useState(null)
  const [portStats,  setPortStats]  = useState([])
  const [utilization,setUtilization]= useState([])
  const [anomalies,  setAnomalies]  = useState([])
  const [recs,       setRecs]       = useState([])
  const [lastUpdate, setLastUpdate] = useState("")
  const [online,     setOnline]     = useState(true)

  const fetchAll = useCallback(async () => {
    try {
      const [sum, ps, ut, an, rc] = await Promise.all([
        fetch(`${API}/api/summary`).then(r => r.json()),
        fetch(`${API}/api/port_stats`).then(r => r.json()),
        fetch(`${API}/api/utilization`).then(r => r.json()),
        fetch(`${API}/api/anomalies`).then(r => r.json()),
        fetch(`${API}/api/recommendations`).then(r => r.json()),
      ])
      setSummary(sum)
      setPortStats(ps)
      setUtilization(ut)
      setAnomalies(an)
      setRecs(rc)
      setOnline(true)
      setLastUpdate(new Date().toLocaleTimeString("vi"))
    } catch {
      setOnline(false)
    }
  }, [])

  useEffect(() => {
    fetchAll()
    const id = setInterval(fetchAll, REFRESH_INTERVAL)
    return () => clearInterval(id)
  }, [fetchAll])

  const pendingCount = recs.filter(r => r.status === "pending").length

  return (
    <div className="app">
      {/* Header */}
      <header className="header">
        <div className="header-left">
          <div className="logo">
            <svg viewBox="0 0 20 20" fill="none" width="20" height="20">
              <circle cx="10" cy="10" r="3" fill="white"/>
              <circle cx="3"  cy="5"  r="2" fill="white" opacity=".7"/>
              <circle cx="17" cy="5"  r="2" fill="white" opacity=".7"/>
              <circle cx="3"  cy="15" r="2" fill="white" opacity=".7"/>
              <circle cx="17" cy="15" r="2" fill="white" opacity=".7"/>
              <line x1="10" y1="7"  x2="4"  y2="6"  stroke="white" strokeWidth="1" opacity=".5"/>
              <line x1="10" y1="7"  x2="16" y2="6"  stroke="white" strokeWidth="1" opacity=".5"/>
              <line x1="10" y1="13" x2="4"  y2="14" stroke="white" strokeWidth="1" opacity=".5"/>
              <line x1="10" y1="13" x2="16" y2="14" stroke="white" strokeWidth="1" opacity=".5"/>
            </svg>
          </div>
          <div>
            <div className="h-title">SDN Traffic Monitor</div>
            <div className="h-sub">OpenFlow 1.3 · Ryu · Mininet · Neon PostgreSQL</div>
          </div>
        </div>
        <div className="header-right">
          <span className={`status-dot ${online ? "online" : "offline"}`}/>
          <span className="last-update">
            {online ? `Cập nhật: ${lastUpdate}` : "Mất kết nối backend"}
          </span>
          <button className="btn-refresh" onClick={fetchAll}>↻ Làm mới</button>
        </div>
      </header>

      <main className="main">
        {/* Summary cards */}
        <Summary data={summary} />

        {/* Chart + Utilization */}
        <div className="grid-chart">
          <BandwidthChart portStats={portStats} />
          <UtilizationBars rows={utilization} />
        </div>

        {/* Topology + Port table */}
        <div className="grid-topo">
          <Topology portStats={portStats} />
          <PortTable rows={portStats} />
        </div>

        {/* Recommendations */}
        <Recommendations
          recs={recs}
          pendingCount={pendingCount}
          onRefresh={fetchAll}
        />

        {/* Anomaly log */}
        <AnomalyTable rows={anomalies} />
      </main>
    </div>
  )
}
