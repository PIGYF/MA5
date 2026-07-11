import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { getJson, routeFromLocation, routePath } from "./lib";
import { BatchBacktest, Backtest, Home, Watchlist } from "./portfolios";
import { Scanner } from "./scanners";
import { Shell } from "./ui";
import "./styles.css";

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
    Promise.all([
      getJson("/api/us/scanner/bootstrap"),
      getJson("/api/cn/scanner/bootstrap"),
      getJson("/api/us/watchlist"),
      getJson("/api/cn/watchlist"),
    ]).then(([usBoot, cnBoot, usWatch, cnWatch]) => {
      if (cancelled) return;
      setBootstraps({ us: usBoot, cn: cnBoot });
      setLatest({ us: usBoot.latest_scan?.latest || null, cn: cnBoot.latest_scan?.latest || null });
      setWatchlists({ us: usWatch.items || [], cn: cnWatch.items || [] });
    }).catch((exception) => setError(exception.message));
    return () => { cancelled = true; };
  }, []);

  const market = route.market;
  const bootstrap = bootstraps[market];
  const setMarketLatest = (next) => setLatest((current) => ({ ...current, [market]: next }));

  if (!bootstraps.us || !bootstraps.cn) {
    return <main className="loading-screen"><div className="loading-mark" /><strong>正在载入策略工作台</strong>{error ? <span>{error}</span> : null}</main>;
  }

  return <Shell route={route} navigate={navigate} marketEnvironment={bootstrap?.market_environment}>
    {error ? <div className="message error global-message">{error}</div> : null}
    {route.page === "home" ? <Home navigate={navigate} /> : null}
    {route.page === "scan" ? <Scanner key={market} market={market} bootstrap={bootstrap} latest={latest[market]} setLatest={setMarketLatest} reloadWatchlist={reloadWatchlist} /> : null}
    {route.page === "watchlist" ? <Watchlist key={market} market={market} items={watchlists[market]} reload={reloadWatchlist} /> : null}
    {route.page === "backtest" ? <Backtest key={market} market={market} defaults={bootstrap?.defaults || {}} /> : null}
    {route.page === "batch" ? <BatchBacktest key={market} market={market} defaults={bootstrap?.defaults || {}} /> : null}
  </Shell>;
}

createRoot(document.getElementById("root")).render(<App />);
