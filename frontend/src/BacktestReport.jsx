import React, { useEffect, useRef, useState } from "react";
import { ColorType, createChart } from "lightweight-charts";
import { LazyStrategyChart } from "./LazyStrategyChart";
import { useThemeMode } from "./lib";
import { Icon } from "./ui";

function parseReport(html) {
  const documentNode = new DOMParser().parseFromString(html, "text/html");
  const json = (id) => { try { return JSON.parse(documentNode.getElementById(id)?.textContent || "null"); } catch { return null; } };
  const metrics = [...documentNode.querySelectorAll(".metric")].map((item) => ({ label: item.querySelector("span")?.textContent || "", value: item.querySelector("strong")?.textContent || "-" }));
  const details = [...documentNode.querySelectorAll("details.report-details")];
  const tradeDetail = details.find((item) => item.querySelector("summary")?.textContent?.includes("交易明细"));
  const headers = tradeDetail ? [...tradeDetail.querySelectorAll("th")].map((item) => item.textContent.trim()) : [];
  const trades = tradeDetail ? [...tradeDetail.querySelectorAll("tbody tr")].map((row) => [...row.querySelectorAll("td")].map((cell) => cell.textContent.trim())) : [];
  return { title: documentNode.querySelector("h1")?.textContent || "回测结果", metrics, chart: json("chart-data"), benchmark: json("benchmark-data"), headers, trades };
}

function EquityCompare({ data }) {
  const ref = useRef(null);
  const themeMode = useThemeMode();
  useEffect(() => {
    if (!data || !ref.current) return undefined;
    const isLight = themeMode === "light";
    const colors = isLight ? { background: "#ffffff", text: "#5f6673", grid: "#edf0f4", border: "#d6dbe3" } : { background: "#101722", text: "#9aa6b5", grid: "#1b2632", border: "#2a3745" };
    const chart = createChart(ref.current, { width: ref.current.clientWidth, height: ref.current.clientHeight, layout: { background: { type: ColorType.Solid, color: colors.background }, textColor: colors.text }, grid: { vertLines: { color: colors.grid }, horzLines: { color: colors.grid } }, rightPriceScale: { borderColor: colors.border }, timeScale: { borderColor: colors.border } });
    const add = (rows, title, color) => { const series = chart.addLineSeries({ title, color, lineWidth: 2, priceLineVisible: false }); series.setData((rows || []).map(([time, value]) => ({ time, value }))); };
    add(data.strategy, "策略", "#2962ff"); add(data.buyHold, data.buyHoldSymbol || "买入持有", "#089981"); add(data.benchmark, data.benchmarkSymbol || "指数", "#7c3aed"); chart.timeScale().fitContent();
    const observer = new ResizeObserver(() => chart.applyOptions({ width: ref.current?.clientWidth || 0, height: ref.current?.clientHeight || 0 })); observer.observe(ref.current);
    return () => { observer.disconnect(); chart.remove(); };
  }, [data, themeMode]);
  return <section className="equity-compare"><header><strong>策略 / 持有 / 指数</strong></header><div ref={ref} /></section>;
}

export function BacktestReport({ url, symbol, market, onClose }) {
  const [report, setReport] = useState(null); const [error, setError] = useState("");
  useEffect(() => { let cancelled = false; setReport(null); setError(""); fetch(url).then((response) => { if (!response.ok) throw new Error(`回测失败：${response.status}`); return response.text(); }).then((html) => { if (!cancelled) setReport(parseReport(html)); }).catch((exception) => { if (!cancelled) setError(exception.message); }); return () => { cancelled = true; }; }, [url]);
  if (error) return <section className="native-report"><div className="frame-error"><strong>{error}</strong></div></section>;
  if (!report) return <section className="native-report"><div className="frame-loading"><span /><b>正在计算回测</b></div></section>;
  return <section className="native-report"><header><strong>{report.title}</strong><button className="icon-button" type="button" title="关闭" onClick={onClose}><Icon name="close" /></button></header><div className="native-report-scroll"><div className="report-metrics">{report.metrics.map((item) => <span key={item.label}><small>{item.label}</small><strong>{item.value}</strong></span>)}</div>{report.benchmark ? <EquityCompare data={report.benchmark} /> : null}{report.chart ? <LazyStrategyChart market={market} symbol={symbol} title="K线、均线、成交量与交易点" data={report.chart} showSignals className="backtest-native-chart" /> : null}<details className="native-trades"><summary>交易明细 · {report.trades.length} 笔</summary><div className="table-wrap"><table><thead><tr>{report.headers.map((header) => <th key={header}>{header}</th>)}</tr></thead><tbody>{report.trades.map((row, index) => <tr key={index}>{row.map((cell, cellIndex) => <td key={cellIndex}>{cell}</td>)}</tr>)}</tbody></table></div></details></div></section>;
}
