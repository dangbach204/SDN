import { useState } from "react"
import { API } from "../App"

export default function PortTable({ rows, appliedLimits = {}, appliedBlocks = {}, onRefresh }) {
  const [filter, setFilter] = useState("all")
  const [loading, setLoading] = useState({})
  const filtered = filter === "all" ? rows : rows.filter(r => String(r.dpid) === filter)

  async function runPortAction(dpid, portNo, actionType) {
    const key = `${dpid}-${portNo}-${actionType}`
    setLoading(prev => ({ ...prev, [key]: true }))
    try {
      const res = await fetch(`${API}/api/ports/${dpid}/${portNo}/action`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action_type: actionType, param: 0 }),
      })
      const data = await res.json()
      if (!res.ok || data?.result !== "ok") {
        const msg = data?.verification || data?.action || "Thực thi thất bại"
        alert(`Lỗi: ${msg}`)
        return
      }

      const doneLabel = actionType === "reset_qos" ? "Reset QoS" : "Unblock"
      alert(`${doneLabel} thành công.\n${data.verification || data.action || ""}`)
      if (typeof onRefresh === "function") {
        await onRefresh()
      }
    } catch {
      alert("Lỗi kết nối backend")
    } finally {
      setLoading(prev => ({ ...prev, [key]: false }))
    }
  }

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
            <th>Action</th>
          </tr>
        </thead>
        <tbody>
          {filtered.length === 0 && (
            <tr><td colSpan={7} className="empty">Chưa có dữ liệu</td></tr>
          )}
          {filtered.map(r => {
            const rx  = ((r.avg_rx  || 0) / 1e6).toFixed(2)
            const tx  = ((r.avg_tx  || 0) / 1e6).toFixed(2)
            const pk  = ((r.peak    || 0) / 1e6).toFixed(2)
            const capacityMbps = Number(r.capacity_mbps) > 0 ? Number(r.capacity_mbps) : 50
            const pctRaw = Number.isFinite(Number(r.utilization_pct))
              ? Number(r.utilization_pct)
              : (((r.avg_rx || 0) + (r.avg_tx || 0)) / (capacityMbps * 1e6) * 100)
            const pct = Math.min(pctRaw, 100)
            const col = pct >= 80 ? "var(--red)" : pct >= 50 ? "var(--yellow)" : "var(--blue)"
            const limitMbps = appliedLimits[`${r.dpid}-${r.port_no}`]
            const limitPct = Number.isFinite(limitMbps)
              ? Math.min((limitMbps / capacityMbps) * 100, 100)
              : null
            const isBlocked = appliedBlocks[`${r.dpid}-${r.port_no}`] === true
            const hasLimit = Number.isFinite(limitMbps) && limitMbps > 0
            const resetKey = `${r.dpid}-${r.port_no}-reset_qos`
            const unblockKey = `${r.dpid}-${r.port_no}-unblock`
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
                    <span className="mono" style={{ color: col, minWidth: 118, fontSize: 10 }}>
                      {pct.toFixed(0)}%
                      {" · "}
                      {isBlocked ? (
                        <span className="port-block-label">Block</span>
                      ) : (
                        <span>{limitPct === null ? "None" : `${limitPct.toFixed(1)}%`}</span>
                      )}
                    </span>
                  </div>
                </td>
                <td>
                  <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                    {hasLimit && (
                      <button
                        className="btn btn-dismiss"
                        style={{ padding: "4px 10px", fontSize: 12 }}
                        disabled={loading[resetKey] === true}
                        onClick={() => runPortAction(r.dpid, r.port_no, "reset_qos")}
                      >
                        {loading[resetKey] ? "Đang reset..." : "Reset QoS"}
                      </button>
                    )}
                    {isBlocked && (
                      <button
                        className="btn btn-dismiss"
                        style={{ padding: "4px 10px", fontSize: 12, borderColor: "var(--red)", color: "var(--red)" }}
                        disabled={loading[unblockKey] === true}
                        onClick={() => runPortAction(r.dpid, r.port_no, "unblock")}
                      >
                        {loading[unblockKey] ? "Đang unblock..." : "Unblock"}
                      </button>
                    )}
                    {!hasLimit && !isBlocked && (
                      <span className="text-muted">-</span>
                    )}
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
