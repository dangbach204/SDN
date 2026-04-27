import { useMemo, useState } from "react"

const FILTERS = [
  ["all", "All"],
  ["1", "S1"],
  ["2", "S2"],
  ["3", "S3"],
]

const UPLINK_PORTS = new Set([
  "1-1", // s1 port 1 -> s2
  "1-2", // s1 port 2 -> s3
  "2-1", // s2 port 1 -> s1
  "2-2", // s2 port 2 -> s3
  "3-1", // s3 port 1 -> s2
  "3-2", // s3 port 2 -> s1
])

function utilColorClass(pct) {
  if (pct > 80) return "c-red"
  if (pct >= 50) return "c-yellow"
  return "c-blue"
}

export default function UtilizationBars({ rows, appliedLimits = {} }) {
  const [filter, setFilter] = useState("all")

  const allRows = useMemo(() => {
    if (!Array.isArray(rows)) return []
    return [...rows]
      .map(r => ({
        ...r,
        dpid: Number(r.dpid),
        port_no: Number(r.port_no),
      }))
      .filter(r => Number.isFinite(r.dpid) && Number.isFinite(r.port_no))
      .sort((a, b) => (a.dpid - b.dpid) || (a.port_no - b.port_no))
  }, [rows])

  const filteredRows = useMemo(() => {
    if (filter === "all") return allRows
    return allRows.filter(r => String(r.dpid) === filter)
  }, [allRows, filter])

  return (
    <div className="card">
      <div className="card-header">
        <div>
          <div className="card-title">Link utilization</div>
          <div className="card-sub">% so với capacity thực tế của từng port</div>
        </div>
      </div>

      <div className="sw-tabs">
        {FILTERS.map(([value, label]) => (
          <button
            key={value}
            className={`sw-tab ${filter === value ? "active" : ""}`}
            onClick={() => setFilter(value)}
          >
            {label}
          </button>
        ))}
      </div>

      <div className="card-sub">Showing {filteredRows.length} / {allRows.length} ports</div>

      <div className="util-list">
        {filteredRows.length === 0 && <div className="empty">Chưa có dữ liệu</div>}
        {filteredRows.map(r => {
          const pct = Math.max(0, Math.min(r.utilization_pct ?? 0, 100))
          const colorClass = utilColorClass(pct)
          const isUplink = UPLINK_PORTS.has(`${r.dpid}-${r.port_no}`)
          const capacityMbps = Number(r.capacity_mbps) > 0 ? Number(r.capacity_mbps) : 50
          const limitMbps = appliedLimits[`${r.dpid}-${r.port_no}`]
          const limitPct = Number.isFinite(limitMbps)
            ? Math.min((limitMbps / capacityMbps) * 100, 100)
            : null
          const limitText = limitPct === null ? "None" : `${limitPct.toFixed(1)}%`

          return (
            <div key={`${r.dpid}-${r.port_no}`} className="util-item">
              <div className="util-row">
                <span className="util-name">
                  s{r.dpid} · port {r.port_no} · {capacityMbps}Mbps
                  {isUplink && <span className="badge WARN">uplink</span>}
                </span>
                <span className={`util-pct ${colorClass}`}>{pct.toFixed(1)}% · limit {limitText}</span>
              </div>
              <div className="util-track">
                <div className={`util-fill ${colorClass}`} style={{ width: `${pct}%`, background: "currentColor" }} />
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
