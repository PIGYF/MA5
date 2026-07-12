import React from "react";
const BatchReport = React.lazy(() => import("./BatchReport").then((module) => ({ default: module.BatchReport })));
export function LazyBatchReport(props) { return <React.Suspense fallback={<section className="native-report"><div className="frame-loading"><span /><b>正在载入组合结果</b></div></section>}><BatchReport {...props} /></React.Suspense>; }
