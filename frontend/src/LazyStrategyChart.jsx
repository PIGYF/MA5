import React from "react";

const StrategyChart = React.lazy(() => import("./StrategyChart").then((module) => ({ default: module.StrategyChart })));

export function LazyStrategyChart(props) {
  return <React.Suspense fallback={<section className={`native-chart ${props.className || ""}`}><div className="frame-loading"><span /><b>正在载入图表组件</b></div></section>}><StrategyChart {...props} /></React.Suspense>;
}
