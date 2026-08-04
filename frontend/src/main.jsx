import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { getJson, routeFromLocation, routePath } from "./lib";
import { BatchBacktest, Backtest, Home, Watchlist } from "./portfolios";
import { Scanner } from "./scanners";
import { Shell } from "./ui";
import "./styles.css";

const ReboundBacktest = React.lazy(() => import("./rebound").then((module) => ({ default: module.ReboundBacktest })));
const ReboundWatchlist = React.lazy(() => import("./rebound").then((module) => ({ default: module.ReboundWatchlist })));

async function loadWatchlist(market) {
  if (market !== "cn") {
    const payload = await getJson(`/api/${market}/watchlist`);
    return payload.items || [];
  }
  const [cnPayload, hkPayload] = await Promise.all([
    getJson("/api/cn/watchlist"),
    getJson("/api/hk/watchlist"),
  ]);
  return [...(cnPayload.items || []), ...(hkPayload.items || [])];
}

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

  function navigate(market, page, requestedStrategy) {
    const strategy = market === "cn" ? "ma5" : (requestedStrategy || route.strategy || "ma5");
    const allowedPage = strategy === "rebound" && !["backtest", "watchlist"].includes(page) ? "backtest" : page;
    const next = { market, strategy, page: allowedPage };
    window.history.pushState({}, "", routePath(market, allowedPage, strategy));
    setRoute(next);
    window.scrollTo({ top: 0, left: 0 });
  }

  async function reloadWatchlist(market) {
    const items = await loadWatchlist(market);
    setWatchlists((current) => ({ ...current, [market]: items }));
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
      loadWatchlist(targetMarket).then((items) => {
        if (!cancelled) setWatchlists((current) => ({ ...current, [targetMarket]: items }));
      }).catch((exception) => { if (!cancelled && targetMarket === route.market) setError(exception.message); });
    });
    return () => { cancelled = true; };
  }, []); // Initial market data streams independently; route changes reuse the loaded cache.

  useEffect(() => {
    let cancelled = false;
    let refreshing = false;
    const refreshMarketState = async () => {
      if (refreshing || document.visibilityState === "hidden") return;
      refreshing = true;
      try {
        const payload = await getJson(`/api/${route.market}/scanner/bootstrap`);
        if (cancelled) return;
        setBootstraps((current) => ({ ...current, [route.market]: payload }));
        setLatest((current) => ({ ...current, [route.market]: payload.latest_scan?.latest || null }));
      } catch (exception) {
        if (!cancelled) setError(exception.message);
      } finally {
        refreshing = false;
      }
    };
    const onVisible = () => { if (document.visibilityState === "visible") refreshMarketState(); };
    refreshMarketState();
    const timer = window.setInterval(refreshMarketState, 300000);
    window.addEventListener("focus", refreshMarketState);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
      window.removeEventListener("focus", refreshMarketState);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [route.market]);

  const market = route.market;
  const strategy = route.strategy || "ma5";
  const bootstrap = bootstraps[market];
  const setMarketLatest = (next) => setLatest((current) => ({ ...current, [market]: next }));

  if (!bootstrap) {
    return <main className="loading-screen"><div className="loading-mark" /><strong>正在载入策略工作台</strong>{error ? <span>{error}</span> : null}</main>;
  }

  return <Shell route={route} navigate={navigate} marketEnvironment={bootstrap?.market_environment}>
    {error ? <div className="message error global-message">{error}</div> : null}
    {strategy === "ma5" && route.page === "home" ? <Home market={market} navigate={navigate} /> : null}
    {strategy === "ma5" && route.page === "scan" ? <Scanner key={market} market={market} bootstrap={bootstrap} latest={latest[market]} setLatest={setMarketLatest} reloadWatchlist={reloadWatchlist} /> : null}
    {strategy === "ma5" && route.page === "watchlist" ? <Watchlist key={market} market={market} items={watchlists[market]} reload={reloadWatchlist} /> : null}
    {strategy === "ma5" && route.page === "backtest" ? <Backtest key={market} market={market} defaults={bootstrap?.defaults || {}} /> : null}
    {strategy === "ma5" && route.page === "batch" ? <BatchBacktest key={market} market={market} defaults={bootstrap?.defaults || {}} /> : null}
    {strategy === "rebound" && route.page === "backtest" ? <React.Suspense fallback={<main className="loading-screen"><div className="loading-mark" /><strong>正在载入超跌反弹策略</strong></main>}><ReboundBacktest defaults={bootstrap?.defaults || {}} /></React.Suspense> : null}
    {strategy === "rebound" && route.page === "watchlist" ? <React.Suspense fallback={<main className="loading-screen"><div className="loading-mark" /><strong>正在载入超跌反弹策略</strong></main>}><ReboundWatchlist /></React.Suspense> : null}
  </Shell>;
}

createRoot(document.getElementById("root")).render(<AppErrorBoundary><App /></AppErrorBoundary>);
