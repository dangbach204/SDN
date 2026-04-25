export default function UtilizationBars({ rows }) {
  return (
    <div className="card">
      <div className="card-header">
        <div>
          <div className="card-title">Link utilization</div>
          <div className="card-sub">% so với 50 Mbps capacity</div>
        </div>
      </div>
      <div className="util-list">
        {rows.length === 0 && <div className="empty">Chưa có dữ liệu</div>}
        {rows.map(r => {
          const pct   = Math.min(r.utilization_pct ?? 0, 100)
          const color = pct >= 80 ? "var(--red)" : pct >= 50 ? "var(--yellow)" : "var(--blue)"
          return (
            <div key={`${r.dpid}-${r.port_no}`} className="util-item">
              <div className="util-row">
                <span className="util-name">s{r.dpid} · p{r.port_no}</span>
                <span className="util-pct" style={{ color }}>{pct.toFixed(1)}%</span>
              </div>
              <div className="util-track">
                <div className="util-fill" style={{ width: `${pct}%`, background: color }} />
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
