import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { getJson, routeFromLocation, routePath } from "./lib";
import { BatchBacktest, Backtest, Home, Watchlist } from "./portfolios";
import { Scanner } from "./scanners";
import { Shell } from "./ui";
import "./styles.css";

try {
  const savedTheme = JSON.parse(window.localStorage.getItem("ma5.ui.v1.theme"));
  document.documentElement.dataset.theme = savedTheme === "light" ? "light" : "dark";
} catch {
  document.documentElement.dataset.theme = "dark";
}

class AppErrorBoundary extends React.Component {
  constructor(props) { super(props); this.state = { error: null }; }
  static getDerivedStateFromError(error) { return { error }; }
  render() {
    if (!this.state.error) return this.props.children;
    return <main className="loading-screen"><strong>页面暂时无法显示</strong><span>{this.state.error.message}</span><button className="primary-action" type="button" onClick={() => window.location.reload()}>重新加载</button></main>;
  }
}

function App() {
  const [route, setRoute] = useState(routeFromLocation());
  const [bootstraps, setBootstraps] = useState({ us: null, cn: null });
  const [latest, setLatest] = useState({ us: null, cn: null });
  const [watchlists, setWatchlists] = useState({ us: [], cn: [] });
  const [error, setError] = useState("");

  function navigate(market, page) {
    const next = { market, page };
    window.history.pushState({}, "", routePath(market, page));
    setRoute(next);
    window.scrollTo({ top: 0, left: 0 });
  }

  async function reloadWatchlist(market) {
    const payload = await getJson(`/api/${market}/watchlist`);
    setWatchlists((current) => ({ ...current, [market]: payload.items || [] }));
  }

  useEffect(() => {
    function onPopState() { setRoute(routeFromLocation()); }
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  useEffect(() => {
    let cancelled = false;
    ["us", "cn"].forEach((targetMarket) => {
      getJson(`/api/${targetMarket}/scanner/bootstrap`).then((payload) => {
        if (cancelled) return;
        setBootstraps((current) => ({ ...current, [targetMarket]: payload }));
        setLatest((current) => ({ ...current, [targetMarket]: payload.latest_scan?.latest || null }));
      }).catch((exception) => { if (!cancelled && targetMarket === route.market) setError(exception.message); });
      getJson(`/api/${targetMarket}/watchlist`).then((payload) => {
        if (!cancelled) setWatchlists((current) => ({ ...current, [targetMarket]: payload.items || [] }));
      }).catch((exception) => { if (!cancelled && targetMarket === route.market) setError(exception.message); });
    });
    return () => { cancelled = true; };
  }, []); // Initial market data streams independently; route changes reuse the loaded cache.

  const market = route.market;
  const bootstrap = bootstraps[market];
  const setMarketLatest = (next) => setLatest((current) => ({ ...current, [market]: next }));

  if (!bootstrap) {
    return <main className="loading-screen"><div className="loading-mark" /><strong>正在载入策略工作台</strong>{error ? <span>{error}</span> : null}</main>;
  }

  return <Shell route={route} navigate={navigate} marketEnvironment={bootstrap?.market_environment}>
    {error ? <div className="message error global-message">{error}</div> : null}
    {route.page === "home" ? <Home market={market} navigate={navigate} /> : null}
    {route.page === "scan" ? <Scanner key={market} market={market} bootstrap={bootstrap} latest={latest[market]} setLatest={setMarketLatest} reloadWatchlist={reloadWatchlist} /> : null}
    {route.page === "watchlist" ? <Watchlist key={market} market={market} items={watchlists[market]} reload={reloadWatchlist} /> : null}
    {route.page === "backtest" ? <Backtest key={market} market={market} defaults={bootstrap?.defaults || {}} /> : null}
    {route.page === "batch" ? <BatchBacktest key={market} market={market} defaults={bootstrap?.defaults || {}} /> : null}
  </Shell>;
}

createRoot(document.getElementById("root")).render(<AppErrorBoundary><App /></AppErrorBoundary>);
