export function PanelHeader({ icon: Icon, title }) {
  return (
    <div className="panel-header">
      <Icon size={17} />
      <h2>{title}</h2>
    </div>
  );
}

export function Metric({ label, value, loading = false }) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>{value ?? (loading ? "..." : "0")}</strong>
    </div>
  );
}

export function EmptyLine({ text }) {
  return <p className="empty-line">{text}</p>;
}
