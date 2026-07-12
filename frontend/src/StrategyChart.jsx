import React, { useEffect, useMemo, useRef, useState } from "react";
import { ColorType, CrosshairMode, LineStyle, createChart } from "lightweight-charts";
import { getJson, toQuery, usePersistentState } from "./lib";
import { Icon } from "./ui";

const periods = ["1m", "3m", "6m", "1y", "3y", "5y"];
const defaultControls = { ma5: true, ma20: true, volume: true, kdj: true, signals: false, defense: false };
const lineData = (rows = []) => rows.map((row) => row.time ? row : ({ time: row.x, value: row.y })).filter((row) => row.time && row.value !== null && row.value !== undefined);
const ohlcData = (rows = []) => rows.map((row) => row.time ? row : ({ time: row.x, open: row.open, high: row.high, low: row.low, close: row.close }));
const volumeData = (rows = []) => rows.map((row) => row.time ? row : ({ time: row.x, value: row.y, color: row.color }));

function normalize(payload) {
  return {
    ...payload,
    ohlc: ohlcData(payload.ohlc),
    volume: volumeData(payload.volume),
    ma5: lineData(payload.ma || payload.ma5 || payload.zx_short_trend),
    ma20: lineData(payload.ma20 || payload.zx_multi_trend),
    volumeMa: lineData(payload.volMa || payload.volume_ma20),
    volumeThreshold: lineData(payload.volThreshold),
    k: lineData(payload.kdjK || payload.k), d: lineData(payload.kdjD || payload.d), j: lineData(payload.kdjJ || payload.j),
    defense: lineData(payload.ma5Stop),
    markers: payload.markers || [],
    signals: [...(payload.signalMarkers || []), ...(payload.signals || []).map((row) => ({ time: row.x, position: "belowBar", color: "#089981", shape: "circle", text: row.text || "B" }))],
  };
}

function ChartToggle({ active, label, onClick }) {
  return <button type="button" className={`chart-chip ${active ? "active" : ""}`} aria-pressed={active} onClick={onClick}>{label}</button>;
}

export function StrategyChart({ market, symbol, params = {}, title, onClose, className = "", data = null }) {
  const [preset, setPreset] = usePersistentState("chart.preset", params.preset || "1y");
  const [controls, setControls] = usePersistentState("chart.controls", defaultControls);
  const [payload, setPayload] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const priceRef = useRef(null); const kdjRef = useRef(null);
  const url = useMemo(() => data ? "" : `${market === "cn" ? "/cn/watchlist/chart" : "/watchlist/chart"}?${toQuery({ ...params, symbol, preset })}`, [data, market, params, preset, symbol]);

  useEffect(() => {
    if (data) { setPayload(normalize(data)); setLoading(false); setError(""); return undefined; }
    let cancelled = false; setLoading(true); setError("");
    getJson(url).then((data) => { if (!cancelled) setPayload(normalize(data)); }).catch((exception) => { if (!cancelled) setError(exception.message); }).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [data, url]);

  useEffect(() => {
    if (!payload?.ohlc?.length || !priceRef.current || !kdjRef.current) return undefined;
    const options = { layout: { background: { type: ColorType.Solid, color: "#fff" }, textColor: "#131722", fontFamily: "Inter, Microsoft YaHei UI, sans-serif" }, grid: { vertLines: { color: "#f1f3f6" }, horzLines: { color: "#f1f3f6" } }, rightPriceScale: { borderColor: "#d6dbe3" }, timeScale: { borderColor: "#d6dbe3", rightOffset: 6, barSpacing: 8, minBarSpacing: 3 }, crosshair: { mode: CrosshairMode.Normal }, handleScroll: { mouseWheel: true, pressedMouseMove: true }, handleScale: { mouseWheel: true, pinch: true, axisPressedMouseMove: true } };
    const priceChart = createChart(priceRef.current, { ...options, width: priceRef.current.clientWidth, height: priceRef.current.clientHeight, rightPriceScale: { borderColor: "#d6dbe3", scaleMargins: { top: .08, bottom: .28 } } });
    const candle = priceChart.addCandlestickSeries({ upColor: "#089981", downColor: "#f23645", borderUpColor: "#089981", borderDownColor: "#f23645", wickUpColor: "#089981", wickDownColor: "#f23645", priceLineVisible: false });
    candle.setData(payload.ohlc);
    if (controls.ma5) { const series = priceChart.addLineSeries({ color: "#f5a623", lineWidth: 2, title: "MA5", priceLineVisible: false }); series.setData(payload.ma5); }
    if (controls.ma20) { const series = priceChart.addLineSeries({ color: "#94a3b8", lineWidth: 1, title: "MA20", priceLineVisible: false, lastValueVisible: false }); series.setData(payload.ma20); }
    if (controls.defense) { const series = priceChart.addLineSeries({ color: "#ef4444", lineWidth: 1, lineStyle: LineStyle.Dashed, title: "策略线", priceLineVisible: false }); series.setData(payload.defense); }
    if (controls.volume) {
      const volume = priceChart.addHistogramSeries({ priceScaleId: "", priceFormat: { type: "volume" }, priceLineVisible: false, lastValueVisible: false }); volume.setData(payload.volume); priceChart.priceScale("").applyOptions({ scaleMargins: { top: .78, bottom: 0 } });
      const volumeMa = priceChart.addLineSeries({ color: "#2962ff", lineWidth: 1, priceScaleId: "", title: "成交量均线", priceLineVisible: false }); volumeMa.setData(payload.volumeMa);
      const threshold = priceChart.addLineSeries({ color: "#f97316", lineWidth: 1, lineStyle: LineStyle.Dashed, priceScaleId: "", title: `${payload.volMultiplier || 1.45}x Vol`, priceLineVisible: false }); threshold.setData(payload.volumeThreshold);
    }
    candle.setMarkers([...(payload.markers || []), ...(controls.signals ? payload.signals : [])].sort((a, b) => String(a.time).localeCompare(String(b.time))));
    const kdjChart = createChart(kdjRef.current, { ...options, width: kdjRef.current.clientWidth, height: kdjRef.current.clientHeight });
    [[payload.k, "#2563eb", "K"], [payload.d, "#f59e0b", "D"], [payload.j, "#7c3aed", "J"]].forEach(([data, color, name]) => { const series = kdjChart.addLineSeries({ color, lineWidth: name === "J" ? 2 : 1, title: name, priceLineVisible: false }); series.setData(data); });
    let syncing = false;
    priceChart.timeScale().subscribeVisibleLogicalRangeChange((range) => { if (!range || syncing) return; syncing = true; kdjChart.timeScale().setVisibleLogicalRange(range); syncing = false; });
    kdjChart.timeScale().subscribeVisibleLogicalRangeChange((range) => { if (!range || syncing) return; syncing = true; priceChart.timeScale().setVisibleLogicalRange(range); syncing = false; });
    priceChart.timeScale().fitContent(); kdjChart.timeScale().fitContent();
    const resize = new ResizeObserver(() => { if (priceRef.current) priceChart.applyOptions({ width: priceRef.current.clientWidth, height: priceRef.current.clientHeight }); if (kdjRef.current) kdjChart.applyOptions({ width: kdjRef.current.clientWidth, height: kdjRef.current.clientHeight }); }); resize.observe(priceRef.current); resize.observe(kdjRef.current);
    priceRef.current.__fitChart = () => { priceChart.timeScale().fitContent(); kdjChart.timeScale().fitContent(); };
    return () => { resize.disconnect(); priceChart.remove(); kdjChart.remove(); };
  }, [controls, payload]);

  const toggle = (key) => setControls((current) => ({ ...current, [key]: !current[key] }));
  return <section className={`native-chart ${controls.kdj ? "" : "hide-kdj"} ${className}`}>
    <header className="native-chart-toolbar"><strong>{title || symbol}</strong>{data ? null : <div className="native-periods">{periods.map((item) => <button key={item} type="button" className={preset === item ? "active" : ""} onClick={() => setPreset(item)}>{item.toUpperCase()}</button>)}</div>}<span className="chart-separator" />
      <ChartToggle label="MA5" active={controls.ma5} onClick={() => toggle("ma5")} /><ChartToggle label="MA20" active={controls.ma20} onClick={() => toggle("ma20")} /><ChartToggle label="成交量" active={controls.volume} onClick={() => toggle("volume")} /><ChartToggle label="KDJ" active={controls.kdj} onClick={() => toggle("kdj")} /><ChartToggle label="B/S" active={controls.signals} onClick={() => toggle("signals")} /><ChartToggle label="策略线" active={controls.defense} onClick={() => toggle("defense")} />
      <button className="icon-button" type="button" title="适应窗口" onClick={() => priceRef.current?.__fitChart?.()}><Icon name="expand" /></button>{onClose ? <button className="icon-button" type="button" title="关闭" onClick={onClose}><Icon name="close" /></button> : null}</header>
    <div className="native-chart-body">{loading ? <div className="frame-loading"><span /><b>正在加载图表</b></div> : null}{error ? <div className="frame-error"><strong>{error}</strong><button type="button" onClick={() => setPreset((current) => current)}>重试</button></div> : null}<div ref={priceRef} className="native-price-chart" /><div ref={kdjRef} className="native-kdj-chart" /></div>
  </section>;
}
