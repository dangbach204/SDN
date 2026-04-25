import { useState } from "react"

const HOST_INFO = {
  h1:  { ip:"10.0.0.1",  mac:"00:00:00:00:00:01", sw:"s1", port:"eth2" },
  h2:  { ip:"10.0.0.2",  mac:"00:00:00:00:00:02", sw:"s1", port:"eth3" },
  h3:  { ip:"10.0.0.3",  mac:"00:00:00:00:00:03", sw:"s1", port:"eth4" },
  h4:  { ip:"10.0.0.4",  mac:"00:00:00:00:00:04", sw:"s1", port:"eth5" },
  h5:  { ip:"10.0.0.5",  mac:"00:00:00:00:00:05", sw:"s2", port:"eth3" },
  h6:  { ip:"10.0.0.6",  mac:"00:00:00:00:00:06", sw:"s2", port:"eth4" },
  h7:  { ip:"10.0.0.7",  mac:"00:00:00:00:00:07", sw:"s2", port:"eth5" },
  h8:  { ip:"10.0.0.8",  mac:"00:00:00:00:00:08", sw:"s2", port:"eth6" },
  h9:  { ip:"10.0.0.9",  mac:"00:00:00:00:00:09", sw:"s3", port:"eth2" },
  h10: { ip:"10.0.0.10", mac:"00:00:00:00:00:0a", sw:"s3", port:"eth3" },
  h11: { ip:"10.0.0.11", mac:"00:00:00:00:00:0b", sw:"s3", port:"eth4" },
  h12: { ip:"10.0.0.12", mac:"00:00:00:00:00:0c", sw:"s3", port:"eth5" },
}

function linkColor(utilPct) {
  if (utilPct >= 80) return "#e05a4a"
  if (utilPct >= 30) return "#f5a623"
  if (utilPct >  0)  return "#4a7fe0"
  return "#d0d4e8"
}
function linkWidth(utilPct) {
  return utilPct >= 80 ? 4 : utilPct >= 30 ? 3 : 2
}

export default function Topology({ portStats }) {
  const [selected, setSelected] = useState(null)

  // Tính utilization cho từng switch (tổng avg tất cả ports)
  const swUtil = {}
  for (const r of portStats) {
    if (!swUtil[r.dpid]) swUtil[r.dpid] = 0
    swUtil[r.dpid] += ((r.avg_rx||0) + (r.avg_tx||0)) / (50e6) * 100
  }

  // Uplink giữa s1-s2: lấy port 1 của s2 (port kết nối về s1)
  const s1Ports = portStats.filter(r => r.dpid===1)
  const s2Ports = portStats.filter(r => r.dpid===2)
  const s3Ports = portStats.filter(r => r.dpid===3)
  const uplinkPct12 = s2Ports[0] ? ((s2Ports[0].avg_rx||0)+(s2Ports[0].avg_tx||0))/(50e6)*100 : 0
  const uplinkPct23 = s3Ports[0] ? ((s3Ports[0].avg_rx||0)+(s3Ports[0].avg_tx||0))/(50e6)*100 : 0

  function selectSwitch(id) {
    const ports = portStats.filter(r => r.dpid===id)
    setSelected({
      type: "switch", id,
      ports,
      total: ports.reduce((a,r)=>(a+(r.avg_rx||0)+(r.avg_tx||0)),0),
    })
  }
  function selectHost(name) {
    setSelected({ type:"host", name, ...HOST_INFO[name] })
  }

  return (
    <div className="card">
      <div className="card-header">
        <div>
          <div className="card-title">Topology mạng</div>
          <div className="card-sub">Nhấn vào switch/host để xem chi tiết</div>
        </div>
      </div>

      <div className="topo-wrap">
        <svg className="topo-svg" viewBox="0 0 520 250">
          {/* Controller */}
          <g style={{ cursor:"pointer" }} onClick={() => setSelected({ type:"controller" })}>
            <rect x="185" y="6" width="150" height="34" rx="8" fill="#eef3fd" stroke="#c5d6f8" strokeWidth="1.5"/>
            <text x="260" y="20" textAnchor="middle" fontFamily="DM Sans" fontSize="11" fontWeight="600" fill="#4a7fe0">Ryu Controller</text>
            <text x="260" y="33" textAnchor="middle" fontFamily="DM Mono" fontSize="9" fill="#9ca3af">127.0.0.1:6653</text>
          </g>

          {/* Controller → switches (dashed) */}
          {[100, 260, 420].map((x,i) => (
            <line key={i} x1="260" y1="40" x2={x} y2="95"
              stroke="#c5d6f8" strokeWidth="1.5" strokeDasharray="4 3"/>
          ))}

          {/* Uplink s1—s2 */}
          <line x1="100" y1="118" x2="245" y2="118"
            stroke={linkColor(uplinkPct12)} strokeWidth={linkWidth(uplinkPct12)}/>
          <text x="172" y="113" textAnchor="middle" fontFamily="DM Mono" fontSize="9" fill="#9ca3af">
            {uplinkPct12.toFixed(0)}%
          </text>

          {/* Uplink s2—s3 */}
          <line x1="275" y1="118" x2="415" y2="118"
            stroke={linkColor(uplinkPct23)} strokeWidth={linkWidth(uplinkPct23)}/>
          <text x="345" y="113" textAnchor="middle" fontFamily="DM Mono" fontSize="9" fill="#9ca3af">
            {uplinkPct23.toFixed(0)}%
          </text>

          {/* Switches */}
          {[
            { id:1, x:70,  hosts:["h1","h2","h3","h4"] },
            { id:2, x:230, hosts:["h5","h6","h7","h8"] },
            { id:3, x:390, hosts:["h9","h10","h11","h12"] },
          ].map(sw => {
            const u = Math.min(swUtil[sw.id]||0, 100)
            const col = u>=80?"#e05a4a":u>=30?"#f5a623":"#4a7fe0"
            return (
              <g key={sw.id}>
                {/* Switch box */}
                <g style={{ cursor:"pointer" }} onClick={() => selectSwitch(sw.id)}>
                  <rect x={sw.x-30} y="96" width="60" height="44" rx="8"
                    fill={`rgba(74,127,224,0.08)`} stroke="#4a7fe0" strokeWidth="1.5"/>
                  <text x={sw.x} y="112" textAnchor="middle" fontFamily="DM Sans"
                    fontSize="12" fontWeight="600" fill="#1a1d2e">s{sw.id}</text>
                  <text x={sw.x} y="128" textAnchor="middle" fontFamily="DM Mono"
                    fontSize="9" fill={col}>{u.toFixed(0)}%</text>
                </g>

                {/* Hosts */}
                {sw.hosts.map((h, hi) => {
                  const hx = sw.x - 45 + hi * 30
                  const hy = 185
                  return (
                    <g key={h} style={{ cursor:"pointer" }} onClick={() => selectHost(h)}>
                      <line x1={sw.x} y1="140" x2={hx} y2={hy}
                        stroke="#d0d4e8" strokeWidth="1.5"/>
                      <circle cx={hx} cy={hy} r="14" fill="#f7f8fc" stroke="#d0d4e8" strokeWidth="1.5"/>
                      <text x={hx} y={hy+1} textAnchor="middle" dominantBaseline="middle"
                        fontFamily="DM Mono" fontSize="8" fill="#6b7280">{h}</text>
                    </g>
                  )
                })}
              </g>
            )
          })}
        </svg>
      </div>

      {/* Detail panel */}
      {selected && (
        <div className="port-info">
          {selected.type === "controller" && (
            <>
              <div className="port-info-title">Ryu Controller</div>
              <div className="info-grid">
                <span className="text-muted">Địa chỉ</span>
                <span className="mono">127.0.0.1:6653</span>
                <span className="text-muted">Giao thức</span>
                <span>OpenFlow 1.3</span>
                <span className="text-muted">Switch kết nối</span>
                <span style={{ color:"var(--green)", fontWeight:600 }}>s1, s2, s3</span>
              </div>
            </>
          )}
          {selected.type === "switch" && (
            <>
              <div className="port-info-title">Switch s{selected.id} — {selected.ports.length} ports</div>
              <div className="text-muted" style={{ fontSize:12, marginBottom:8 }}>
                Tổng BW: <strong style={{ color:"var(--blue)" }}>
                  {(selected.total/1e6/2).toFixed(1)} Mbps avg
                </strong>
              </div>
              <table style={{ fontSize:11 }}>
                <thead><tr>
                  <th>Port</th><th>RX Mbps</th><th>TX Mbps</th><th>Util%</th>
                </tr></thead>
                <tbody>
                  {selected.ports.map(r => {
                    const pct = ((r.avg_rx||0)+(r.avg_tx||0))/(50e6)*100
                    return (
                      <tr key={r.port_no}>
                        <td className="mono bold">eth{r.port_no}</td>
                        <td style={{ color:"var(--blue)" }}>{((r.avg_rx||0)/1e6).toFixed(2)}</td>
                        <td style={{ color:"var(--purple)" }}>{((r.avg_tx||0)/1e6).toFixed(2)}</td>
                        <td style={{ color: pct>=80?"var(--red)":pct>=30?"var(--yellow)":"inherit" }}>
                          {pct.toFixed(1)}%
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </>
          )}
          {selected.type === "host" && (
            <>
              <div className="port-info-title">Host {selected.name}</div>
              <div className="info-grid">
                <span className="text-muted">IP</span>
                <span className="mono bold">{selected.ip}</span>
                <span className="text-muted">MAC</span>
                <span className="mono">{selected.mac}</span>
                <span className="text-muted">Kết nối</span>
                <span className="bold">{selected.sw} · {selected.port}</span>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  )
}
