export default function AnomalyTable({ rows }) {
  return (
    <div className="card">
      <div className="card-header">
        <div>
          <div className="card-title">Nhật ký cảnh báo</div>
          <div className="card-sub">30 cảnh báo gần nhất</div>
        </div>
      </div>
      <div style={{ overflowX: "auto" }}>
        <table>
          <thead>
            <tr>
              <th>Thời gian</th>
              <th>Switch</th>
              <th>Port</th>
              <th>Mức</th>
              <th>Giá trị (Mbps)</th>
              <th>Ngưỡng (Mbps)</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr><td colSpan={6} className="empty">Chưa có cảnh báo</td></tr>
            )}
            {rows.map((r, i) => (
              <tr key={i}>
                <td className="mono text-muted" style={{ fontSize: 11 }}>{r.time}</td>
                <td className="mono bold">s{r.dpid}</td>
                <td className="mono">{r.port_no}</td>
                <td><span className={`badge ${r.level}`}>{r.level}</span></td>
                <td className="mono">{((r.value     || 0) / 1e6).toFixed(2)}</td>
                <td className="mono text-muted">{((r.threshold || 0) / 1e6).toFixed(2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
