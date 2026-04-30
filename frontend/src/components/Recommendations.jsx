import { useEffect, useMemo, useState } from "react"
import { API } from "../App"

const TAG_CLASS = {
  QoS:         "tag-qos",
  BLOCK:       "tag-block",
  MONITOR:     "tag-monitor",
  INVESTIGATE: "tag-investigate",
}

export default function Recommendations({ recs, pendingCount, onRefresh }) {
  const [filter,          setFilter]          = useState("all")
  const [switchFilter,    setSwitchFilter]    = useState("all")
  const [ethFilter,       setEthFilter]       = useState("all")
  const [switchQuery,     setSwitchQuery]     = useState("")
  const [expanded,        setExpanded]        = useState({})
  const [selectedActions, setSelectedActions] = useState({})
  const [loading,         setLoading]         = useState({})

  const availableSwitches = useMemo(() => {
    return [...new Set(recs.map(r => Number(r.dpid)).filter(Number.isFinite))].sort((a, b) => a - b)
  }, [recs])

  const availableEths = useMemo(() => {
    const scoped = switchFilter === "all"
      ? recs
      : recs.filter(r => String(r.dpid) === switchFilter)

    return [...new Set(scoped.map(r => Number(r.port_no)).filter(Number.isFinite))].sort((a, b) => a - b)
  }, [recs, switchFilter])

  useEffect(() => {
    if (ethFilter !== "all" && !availableEths.includes(Number(ethFilter))) {
      setEthFilter("all")
    }
  }, [availableEths, ethFilter])

  const filtered = useMemo(() => {
    const query = switchQuery.trim().toLowerCase()

    return recs.filter(rec => {
      if (filter !== "all" && rec.status !== filter) return false
      if (switchFilter !== "all" && String(rec.dpid) !== switchFilter) return false
      if (ethFilter !== "all" && String(rec.port_no) !== ethFilter) return false
      if (query) {
        const swLabel = `s${rec.dpid}`.toLowerCase()
        const dpidText = String(rec.dpid).toLowerCase()
        if (!swLabel.includes(query) && !dpidText.includes(query)) return false
      }
      return true
    })
  }, [ethFilter, filter, recs, switchFilter, switchQuery])

  function toggleExpand(id) {
    setExpanded(e => ({ ...e, [id]: !e[id] }))
  }

  function selectAction(recId, action) {
    setSelectedActions(s => ({ ...s, [recId]: action }))
  }

  async function applyRec(rec) {
    const chosen = selectedActions[rec.id]
    if (!chosen && rec.actions_json) return  // must choose first
    setLoading(l => ({ ...l, [rec.id]: true }))
    try {
      const url  = `${API}/api/recommendations/${rec.id}/choose`
      const body = {
        action_id:   chosen?.id   || "default",
        action_type: chosen?.type || rec.action_type,
        param:       chosen?.param || 0,
      }
      const res  = await fetch(url, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify(body),
      })
      const data = await res.json()
      const lines = []
      if (data.action) lines.push(`Thực thi: ${data.action}`)
      if (data.verification) lines.push(`Xác minh: ${data.verification}`)
      if (typeof data.before_mbps === "number" && typeof data.after_mbps === "number") {
        lines.push(`BW trước/sau: ${data.before_mbps.toFixed(2)} -> ${data.after_mbps.toFixed(2)} Mbps`)
      }

      if (lines.length > 0) {
        const prefix = data.result === "ok" ? "Kết quả" : "Thất bại"
        alert(`${prefix}:\n${lines.join("\n")}`)
      }
      await onRefresh()
    } catch {
      alert("Lỗi kết nối backend")
    } finally {
      setLoading(l => ({ ...l, [rec.id]: false }))
    }
  }

  async function dismissRec(id) {
    await fetch(`${API}/api/recommendations/${id}/dismiss`, { method:"POST" })
    await onRefresh()
  }

  return (
    <div className="card" style={{ marginBottom:16 }}>
      <div className="card-header">
        <div>
          <div className="card-title" style={{ display:"flex", alignItems:"center", gap:8 }}>
            Khuyến nghị
            {pendingCount > 0 && (
              <span className="rec-count-badge">{pendingCount}</span>
            )}
          </div>
          <div className="card-sub">Phân tích nguyên nhân và đề xuất hành động</div>
        </div>
        {/* Filter tabs */}
        <div className="tabs">
          {[["all","Tất cả"],["pending","Chờ xử lý"],["applied","Đã áp dụng"],["dismissed","Bỏ qua"]].map(([v,l]) => (
            <button key={v}
              className={`tab ${filter===v?"active":""}`}
              onClick={() => setFilter(v)}>{l}</button>
          ))}
        </div>
      </div>
          <div className="rec-filters">
            <label className="rec-search">
              <span>Tìm switch</span>
              <input
                type="text"
                value={switchQuery}
                onChange={e => setSwitchQuery(e.target.value)}
                placeholder="Nhập s1, s2, ..."
              />
            </label>

            <div className="rec-filter-group">
              <div className="rec-filter-label">Switch</div>
              <div className="sw-tabs rec-tab-row">
                <button
                  className={`sw-tab ${switchFilter === "all" ? "active" : ""}`}
                  onClick={() => setSwitchFilter("all")}
                >Tất cả</button>
                {availableSwitches.map(sw => (
                  <button
                    key={sw}
                    className={`sw-tab ${switchFilter === String(sw) ? "active" : ""}`}
                    onClick={() => setSwitchFilter(String(sw))}
                  >S{sw}</button>
                ))}
              </div>
            </div>

            <div className="rec-filter-group">
              <div className="rec-filter-label">Eth</div>
              <div className="sw-tabs rec-tab-row">
                <button
                  className={`sw-tab ${ethFilter === "all" ? "active" : ""}`}
                  onClick={() => setEthFilter("all")}
                >Tất cả</button>
                {availableEths.map(portNo => (
                  <button
                    key={portNo}
                    className={`sw-tab ${ethFilter === String(portNo) ? "active" : ""}`}
                    onClick={() => setEthFilter(String(portNo))}
                  >eth{portNo}</button>
                ))}
              </div>
            </div>
          </div>

      <div id="recPanel">
        {filtered.length === 0 && (
          <div className="empty">Không có khuyến nghị nào</div>
        )}
        {filtered.map(rec => {
          let actions = []
          try { actions = JSON.parse(rec.actions_json || "[]") } catch {}
          const isPending = rec.status === "pending"
          const chosen    = selectedActions[rec.id]
          const isLoading = loading[rec.id]

          return (
            <div key={rec.id} className={`rec-card ${rec.status} ${expanded[rec.id]?"expanded":""}`}>
              {/* Header — clickable to expand */}
              <div className="rec-header" onClick={() => toggleExpand(rec.id)}>
                <div style={{ flex:1 }}>
                  <div className="rec-title-row">
                    <span className={`badge ${rec.level}`}>{rec.level}</span>
                    <span className={`action-tag ${TAG_CLASS[rec.action_type]||"tag-monitor"}`}>
                      {rec.action_type}
                    </span>
                    <span className="mono text-muted" style={{ fontSize:11 }}>
                      s{rec.dpid} · port {rec.port_no}
                    </span>
                  </div>
                  <div className="rec-msg">{rec.message}</div>
                  <div className="mono text-muted" style={{ fontSize:11, marginTop:4 }}>
                    {rec.time}
                  </div>
                </div>
                <div style={{ display:"flex", alignItems:"center", gap:10, marginLeft:12 }}>
                  <StatusPill status={rec.status} chosen={rec.chosen_action}/>
                  <span className="expand-icon">▼</span>
                </div>
              </div>

              {/* Body */}
              {expanded[rec.id] && (
                <div className="rec-body">
                  {/* Root cause */}
                  <div className="cause-box">
                    <div className="cause-label">🔍 Phân tích nguyên nhân</div>
                    <div className="cause-text">{rec.root_cause || "Chưa có phân tích"}</div>
                  </div>

                  {/* Actions */}
                  {isPending && actions.length > 0 && (
                    <>
                      <div className="actions-label">⚡ Chọn hành động xử lý</div>
                      <div className="action-list">
                        {actions.map(a => (
                          <div key={a.id}
                            className={`action-item ${chosen?.id===a.id?"selected":""}`}
                            onClick={() => selectAction(rec.id, a)}>
                            <div className={`action-radio ${chosen?.id===a.id?"selected":""}`}/>
                            <div style={{ flex:1 }}>
                              <div className="action-name">
                                {a.label}
                                <span className={`action-tag ${TAG_CLASS[a.type]||"tag-monitor"}`}>
                                  {a.type}
                                </span>
                              </div>
                              <div className="action-desc">{a.desc}</div>
                            </div>
                          </div>
                        ))}
                      </div>
                    </>
                  )}

                  {/* Footer buttons */}
                  {isPending && (
                    <div className="rec-footer">
                      <span className="rec-meta">ID #{rec.id}</span>
                      <div style={{ display:"flex", gap:8 }}>
                        <button className="btn btn-dismiss"
                          onClick={() => dismissRec(rec.id)}>Bỏ qua</button>
                        <button className="btn btn-apply"
                          disabled={isLoading || (actions.length > 0 && !chosen)}
                          onClick={() => applyRec(rec)}>
                          {isLoading ? "Đang xử lý..." :
                           actions.length > 0 ? "Áp dụng hành động đã chọn" : "Áp dụng"}
                        </button>
                      </div>
                    </div>
                  )}
                  {!isPending && (
                    <div style={{ paddingTop:8 }}>
                      <span className="rec-meta">ID #{rec.id}</span>
                      <StatusPill status={rec.status} chosen={rec.chosen_action}/>
                    </div>
                  )}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

function StatusPill({ status, chosen }) {
  const map = {
    pending:   { cls:"pending",   text:"⏳ Chờ xử lý" },
    applied:   { cls:"applied",   text:`✓ Đã áp dụng${chosen?` — ${chosen}`:""}` },
    dismissed: { cls:"dismissed", text:"✗ Bỏ qua" },
  }
  const s = map[status]
  if (!s) return null
  return <span className={`status-pill ${s.cls}`}>{s.text}</span>
}
