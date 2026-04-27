import { useMemo } from "react"
import { API } from "../App"

function fmtScore(v) {
  if (typeof v !== "number" || Number.isNaN(v)) return "—"
  return v.toFixed(3)
}

function fmtPct(v) {
  if (typeof v !== "number" || Number.isNaN(v)) return "—"
  return `${v.toFixed(1)}%`
}

function parseKpi(jsonText) {
  if (!jsonText) return null
  try {
    return JSON.parse(jsonText)
  } catch {
    return null
  }
}

export default function ControlLoopPanel({ state, actions, onRefresh }) {
  const enabled = Boolean(state?.enabled)
  const runtime = state?.runtime || {}
  const last = state?.last_cycle || {}

  const recentStats = useMemo(() => {
    const total = Array.isArray(actions) ? actions.length : 0
    const success = (actions || []).filter(a => a.execution_ok && a.verification_ok).length
    const rollback = (actions || []).filter(a => a.rollback_performed).length
    return { total, success, rollback }
  }, [actions])

  async function toggleEnabled() {
    try {
      await fetch(`${API}/api/control/enabled`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: !enabled }),
      })
      await onRefresh()
    } catch {
      alert("Không đổi được trạng thái closed-loop")
    }
  }

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <div className="card-header">
        <div>
          <div className="card-title">Autonomous Closed-Loop Control</div>
          <div className="card-sub">Detect - Decide - Enforce - Verify - Keep/Rollback/Re-optimize</div>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <span className={`loop-pill ${enabled ? "enabled" : "disabled"}`}>
            {enabled ? "ENABLED" : "DISABLED"}
          </span>
          <button className="btn-refresh" onClick={toggleEnabled}>
            {enabled ? "Tạm dừng" : "Kích hoạt"}
          </button>
        </div>
      </div>

      <div className="loop-stats">
        <div className="loop-stat">
          <div className="loop-stat-label">Active actions</div>
          <div className="loop-stat-val c-blue">{runtime.active_actions ?? 0}</div>
        </div>
        <div className="loop-stat">
          <div className="loop-stat-label">Cooldown ports</div>
          <div className="loop-stat-val c-yellow">{runtime.cooldown_ports ?? 0}</div>
        </div>
        <div className="loop-stat">
          <div className="loop-stat-label">Last cycle congested</div>
          <div className="loop-stat-val c-red">{last.congested ?? 0}</div>
        </div>
        <div className="loop-stat">
          <div className="loop-stat-label">Last cycle applied</div>
          <div className="loop-stat-val c-green">{last.actions_applied ?? 0}</div>
        </div>
        <div className="loop-stat">
          <div className="loop-stat-label">Recent success ratio</div>
          <div className="loop-stat-val c-purple">
            {recentStats.total > 0 ? `${recentStats.success}/${recentStats.total}` : "—"}
          </div>
        </div>
      </div>

      <div style={{ overflowX: "auto", marginTop: 12 }}>
        <table>
          <thead>
            <tr>
              <th>Time</th>
              <th>Port</th>
              <th>Strategy</th>
              <th>Action</th>
              <th>Confidence</th>
              <th>Score</th>
              <th>Decision</th>
              <th>KPI util before/after</th>
            </tr>
          </thead>
          <tbody>
            {(actions || []).length === 0 && (
              <tr>
                <td colSpan={8} className="empty">Chưa có control action</td>
              </tr>
            )}
            {(actions || []).map(a => {
              const before = parseKpi(a.before_kpi)
              const after = parseKpi(a.after_kpi)
              return (
                <tr key={a.id}>
                  <td className="mono text-muted" style={{ fontSize: 11 }}>{a.time}</td>
                  <td className="mono">s{a.dpid}/p{a.port_no}</td>
                  <td>{a.strategy}</td>
                  <td className="mono">{a.action_type} {a.action_param ? `(${a.action_param})` : ""}</td>
                  <td>{fmtPct((a.confidence || 0) * 100)}</td>
                  <td className="mono">{fmtScore(a.score)}</td>
                  <td>
                    <span className={`loop-decision ${a.decision || "pending"}`}>
                      {a.decision || "pending"}
                    </span>
                  </td>
                  <td className="mono" style={{ fontSize: 11 }}>
                    {before?.utilization_pct != null ? before.utilization_pct.toFixed(1) : "—"}
                    {" -> "}
                    {after?.utilization_pct != null ? after.utilization_pct.toFixed(1) : "—"}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
