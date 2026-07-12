import React from "react";

const BacktestReport = React.lazy(() => import("./BacktestReport").then((module) => ({ default: module.BacktestReport })));

export function LazyBacktestReport(props) {
  return <React.Suspense fallback={<section className="native-report"><div className="frame-loading"><span /><b>正在载入回测组件</b></div></section>}><BacktestReport {...props} /></React.Suspense>;
}
