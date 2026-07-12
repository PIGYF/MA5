import React from "react";
import { routePath, usePersistentState } from "./lib";

const paths = {
  home: "M3 10.8 12 3l9 7.8 M5 10v10h14V10 M9 20v-6h6v6",
  scan: "M4 7V4h3 M17 4h3v3 M20 17v3h-3 M7 20H4v-3 M8 12h8 M12 8v8",
  star: "m12 3 2.7 5.5 6.1.9-4.4 4.3 1 6.1L12 16.9l-5.4 2.9 1-6.1-4.4-4.3 6.1-.9L12 3z",
  chart: "M4 19h16 M6 15l4-4 3 3 5-7 M18 7h-4 M18 7v4",
  batch: "M4 6h16 M4 12h16 M4 18h16",
  stop: "M6 6h12v12H6z",
  pause: "M8 5v14 M16 5v14",
  play: "M8 5v14l11-7z",
  trash: "M4 7h16 M10 11v6 M14 11v6 M6 7l1 14h10l1-14 M9 7V4h6v3",
  download: "M12 3v11 m-5-4 5 5 5-5 M5 20h14",
  plus: "M12 5v14 M5 12h14",
  close: "M6 6l12 12 M18 6 6 18",
  filter: "M4 5h16l-6 7v6l-4 2v-8L4 5z",
  collapse: "M15 5l-7 7 7 7",
  expand: "M9 5l7 7-7 7",
  external: "M7 17 17 7 M10 7h7v7",
};

export function Icon({ name }) {
  return <svg className="icon" viewBox="0 0 24 24" aria-hidden="true"><path d={paths[name] || paths.chart} /></svg>;
}

const pages = [
  { key: "scan", label: "选股", icon: "scan" },
  { key: "watchlist", label: "自选池", icon: "star" },
  { key: "backtest", label: "回测", icon: "chart" },
  { key: "batch", label: "批量回测", icon: "batch" },
];

export function Shell({ route, navigate, marketEnvironment, children }) {
  const market = route.market;
  const environment = marketEnvironment || {};
  return (
    <main className="app-shell">
      <header className="topbar">
        <a className="brand" href="/app/" onClick={(event) => { event.preventDefault(); navigate(market, "home"); }}>
          <Icon name="chart" /><strong>MA5 Strategy Lab</strong>
        </a>
        <div className="market-switch" aria-label="市场切换">
          <button className={market === "us" ? "active" : ""} onClick={() => navigate("us", route.page === "home" ? "scan" : route.page)}>美股</button>
          <button className={market === "cn" ? "active" : ""} onClick={() => navigate("cn", route.page === "home" ? "scan" : route.page)}>A股</button>
        </div>
        <nav className="main-nav" aria-label="功能导航">
          {pages.map((item) => (
            <a key={item.key} href={routePath(market, item.key)} className={route.page === item.key ? "active" : ""} onClick={(event) => { event.preventDefault(); navigate(market, item.key); }}>
              <Icon name={item.icon} />{item.label}
            </a>
          ))}
        </nav>
        <div className={`market-state tone-${environment.tone || "neutral"}`}>
          <i />
          <span>{environment.state || (market === "cn" ? "盘后复盘" : "Market")}</span>
          <b>{environment.symbol || (market === "cn" ? "A股" : "QQQ")}</b>
          {environment.vix ? <em>VIX {Number(environment.vix).toFixed(1)}</em> : null}
        </div>
      </header>
      {children}
    </main>
  );
}

export function PageToolbar({ title, subtitle, actions }) {
  return <section className="page-toolbar"><div><h1>{title}</h1>{subtitle ? <p>{subtitle}</p> : null}</div><div className="toolbar-actions">{actions}</div></section>;
}

export function FilterSection({ title, note, children, open = true }) {
  return <details className="filter-section" open={open}><summary><span>{title}</span>{note ? <small>{note}</small> : null}</summary><div className="filter-body">{children}</div></details>;
}

export function Field({ label, children, wide = false }) {
  return <label className={wide ? "field wide" : "field"}><span>{label}</span>{children}</label>;
}

export function Checkbox({ label, checked, onChange }) {
  return <label className="checkline"><input type="checkbox" checked={Boolean(checked)} onChange={(event) => onChange(event.target.checked)} /><span>{label}</span></label>;
}

export function Progress({ job }) {
  const total = Number(job?.total || 0);
  const scanned = Number(job?.scanned || 0);
  const percent = Number(job?.progress_pct ?? (total ? Math.round((scanned / total) * 100) : 0));
  return <div className="progress-strip"><div><strong>{job?.status_label || job?.message || job?.status}</strong><span>{scanned}/{total || "-"}</span><span>候选 {job?.candidates || 0}</span><span>失败 {job?.errors || 0}</span><em>{job?.current || job?.stage || ""}</em></div><div className="progress-track"><i style={{ width: `${Math.max(0, Math.min(100, percent))}%` }} /></div></div>;
}

export function ChartFrame({ title, src, onClose, className = "", showHeader = true }) {
  const [loading, setLoading] = React.useState(true);
  const [failed, setFailed] = React.useState(false);
  React.useEffect(() => setLoading(true), [src]);
  return <section className={`chart-drawer ${showHeader ? "" : "no-header"} ${className}`}>
    {showHeader ? <header><strong>{title}</strong>{onClose ? <button className="icon-button" type="button" onClick={onClose} title="关闭" aria-label="关闭图表"><Icon name="close" /></button> : null}</header> : null}
    <div className="chart-frame-body">
      {loading && !failed ? <div className="frame-loading" role="status"><span /><b>正在加载图表</b></div> : null}
      {failed ? <div className="frame-error"><strong>图表加载失败</strong><button type="button" onClick={() => { setFailed(false); setLoading(true); }}>重试</button></div> : null}
      <iframe loading="lazy" src={src} title={title} onError={() => { setLoading(false); setFailed(true); }} onLoad={(event) => { setLoading(false); setFailed(false); try { event.currentTarget.contentWindow.scrollTo(0, 0); } catch { /* same-origin report in normal use */ } }} />
    </div>
  </section>;
}

export function ResizableWorkspace({ storageKey, className, initial = 300, min = 220, max = 460, children }) {
  const [size, setSize] = usePersistentState(`layout.${storageKey}`, initial);
  const startDrag = (event) => {
    if (window.matchMedia("(max-width: 820px)").matches) return;
    event.preventDefault();
    const startX = event.clientX;
    const startSize = Number(size) || initial;
    const move = (moveEvent) => setSize(Math.max(min, Math.min(max, startSize + moveEvent.clientX - startX)));
    const stop = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", stop); };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop);
  };
  const parts = React.Children.toArray(children);
  return <section className={`${className} resizable-workspace`} style={{ "--rail-size": `${size}px` }}>
    {parts[0]}
    <div className="resize-handle resize-handle-x" role="separator" aria-orientation="vertical" tabIndex="0" onPointerDown={startDrag} onKeyDown={(event) => {
      if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
      event.preventDefault(); setSize(Math.max(min, Math.min(max, Number(size) + (event.key === "ArrowRight" ? 12 : -12))));
    }} />
    {parts.slice(1)}
  </section>;
}

export function WorkspaceEmpty({ title = "暂无结果", note = "" }) {
  return <section className="workspace-empty"><div className="empty-grid" /><div><Icon name="chart" /><strong>{title}</strong>{note ? <span>{note}</span> : null}</div></section>;
}
