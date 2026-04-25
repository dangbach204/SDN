export default function Summary({ data }) {
  const cards = [
    { label: "Tổng bản ghi",  value: data?.total_records?.toLocaleString() ?? "—", accent: "blue",   sub: "Port stats đã lưu" },
    { label: "Tổng cảnh báo", value: data?.total_anomalies ?? "—",                 accent: "red",    sub: "Bất thường phát hiện" },
    { label: "Mức HIGH",       value: data?.high   ?? "—",                          accent: "red",    sub: "Vượt ngưỡng cao" },
    { label: "Mức WARN",       value: data?.warn   ?? "—",                          accent: "yellow", sub: "Cần theo dõi" },
    { label: "Mức ZSCORE",     value: data?.zscore ?? "—",                          accent: "purple", sub: "Bất thường thống kê" },
  ]
  return (
    <div className="stats">
      {cards.map(c => (
        <div key={c.label} className={`stat-card accent-${c.accent}`}>
          <div className="stat-label">{c.label}</div>
          <div className={`stat-val c-${c.accent}`}>{c.value}</div>
          <div className="stat-sub">{c.sub}</div>
        </div>
      ))}
    </div>
  )
}
