import React, { useEffect, useState } from "react";
import { LazyStrategyChart } from "./LazyStrategyChart";
import { Icon } from "./ui";

function parseTable(table) {
  const title = table.closest("div.table-wrap")?.previousElementSibling?.textContent?.trim() || "明细";
  return { title, headers: [...table.querySelectorAll("thead th")].map((item) => item.textContent.trim()), rows: [...table.querySelectorAll("tbody tr")].map((row) => [...row.querySelectorAll("td")].map((cell) => cell.textContent.trim())) };
}

function parseBatch(html) {
  const doc = new DOMParser().parseFromString(html, "text/html");
  const hint = doc.querySelector("section.result .hint")?.textContent?.trim() || "批量回测结果";
  const tables = [...doc.querySelectorAll("section.result .table-wrap table")].map(parseTable);
  const charts = [...doc.querySelectorAll("[data-batch-chart-url]")].map((button) => ({ symbol: button.dataset.batchChartSymbol, url: button.dataset.batchChartUrl }));
  return { hint, tables, charts };
}

export function BatchReport({ url, onClose }) {
  const [report, setReport] = useState(null); const [selected, setSelected] = useState(null); const [chartData, setChartData] = useState(null); const [error, setError] = useState("");
  useEffect(() => { let cancelled = false; fetch(url).then((response) => response.text()).then((html) => { if (cancelled) return; const parsed = parseBatch(html); setReport(parsed); setSelected(parsed.charts[0] || null); }).catch((exception) => { if (!cancelled) setError(exception.message); }); return () => { cancelled = true; }; }, [url]);
  useEffect(() => { if (!selected?.url) { setChartData(null); return undefined; } let cancelled = false; setChartData(null); fetch(selected.url).then((response) => response.text()).then((html) => { const doc = new DOMParser().parseFromString(html, "text/html"); const raw = doc.getElementById("chart-data")?.textContent; if (!raw) throw new Error("个股图表数据缺失"); if (!cancelled) setChartData(JSON.parse(raw)); }).catch((exception) => { if (!cancelled) setError(exception.message); }); return () => { cancelled = true; }; }, [selected]);
  if (error) return <section className="native-report"><div className="frame-error"><strong>{error}</strong></div></section>;
  if (!report) return <section className="native-report"><div className="frame-loading"><span /><b>正在计算组合回测</b></div></section>;
  return <section className="native-report"><header><strong>批量回测结果</strong><button className="icon-button" type="button" title="关闭" onClick={onClose}><Icon name="close" /></button></header><div className="native-report-scroll"><p className="batch-hint">{report.hint}</p>{report.tables.map((table) => <details className="native-trades" key={table.title} open={table.title.includes("策略对比")}><summary>{table.title} · {table.rows.length} 行</summary><div className="table-wrap"><table><thead><tr>{table.headers.map((header) => <th key={header}>{header}</th>)}</tr></thead><tbody>{table.rows.map((row, rowIndex) => <tr key={rowIndex}>{row.map((cell, index) => <td key={index}>{cell}</td>)}</tr>)}</tbody></table></div></details>)}{report.charts.length ? <><div className="batch-chart-tabs">{report.charts.map((item) => <button type="button" className={selected?.symbol === item.symbol ? "active" : ""} key={item.symbol} onClick={() => setSelected(item)}>{item.symbol}</button>)}</div>{chartData ? <LazyStrategyChart market="us" symbol={selected.symbol} data={chartData} title={`${selected.symbol} · 实际交易点`} className="backtest-native-chart" /> : <div className="frame-loading"><span /><b>正在载入个股图表</b></div>}</> : null}</div></section>;
}
