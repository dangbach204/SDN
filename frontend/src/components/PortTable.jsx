import { useState } from "react"

export default function PortTable({ rows }) {
  const [filter, setFilter] = useState("all")
  const filtered = filter === "all" ? rows : rows.filter(r => String(r.dpid) === filter)

  return (
    <div className="card">
      <div className="card-header">
        <div>
          <div className="card-title">Port stats chi tiết</div>
          <div className="card-sub">60 giây gần nhất · sắp xếp theo băng thông</div>
        </div>
        <div className="sw-tabs">
          {[["all","All"],["1","S1"],["2","S2"],["3","S3"]].map(([v, l]) => (
            <button
              key={v}
              className={`sw-tab ${filter === v ? "active" : ""}`}
              onClick={() => setFilter(v)}
            >{l}</button>
          ))}
        </div>
      </div>
      <table>
        <thead>
          <tr>
            <th>Switch</th>
            <th>Port</th>
            <th>RX (Mbps)</th>
            <th>TX (Mbps)</th>
            <th>Peak</th>
            <th>Util%</th>
          </tr>
        </thead>
        <tbody>
          {filtered.length === 0 && (
            <tr><td colSpan={6} className="empty">Chưa có dữ liệu</td></tr>
          )}
          {filtered.map(r => {
            const rx  = ((r.avg_rx  || 0) / 1e6).toFixed(2)
            const tx  = ((r.avg_tx  || 0) / 1e6).toFixed(2)
            const pk  = ((r.peak    || 0) / 1e6).toFixed(2)
            const pct = Math.min(((r.avg_rx || 0) + (r.avg_tx || 0)) / (50e6) * 100, 100)
            const col = pct >= 80 ? "var(--red)" : pct >= 50 ? "var(--yellow)" : "var(--blue)"
            return (
              <tr key={`${r.dpid}-${r.port_no}`}>
                <td className="mono bold">s{r.dpid}</td>
                <td className="mono">eth{r.port_no}</td>
                <td style={{ color: "var(--blue)" }}>{rx}M</td>
                <td style={{ color: "var(--purple)" }}>{tx}M</td>
                <td className="bold">{pk}M</td>
                <td>
                  <div className="pct-bar-wrap">
                    <div className="pct-bar">
                      <div className="pct-fill" style={{ width: `${pct}%`, background: col }} />
                    </div>
                    <span className="mono" style={{ color: col, minWidth: 36, fontSize: 10 }}>
                      {pct.toFixed(0)}%
                    </span>
                  </div>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
