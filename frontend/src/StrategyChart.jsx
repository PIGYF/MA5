import React, { useEffect, useMemo, useRef, useState } from "react";
import { ColorType, CrosshairMode, LineStyle, createChart } from "lightweight-charts";
import { getJson, toQuery, usePersistentState, useThemeMode } from "./lib";
import { Icon } from "./ui";

const periods = ["1m", "3m", "6m", "1y", "3y", "5y"];
const defaultControls = { ma5: true, ma20: true, volume: true, kdj: true, signals: false, defense25: false, defense: false, holding: true };
const lineData = (rows = []) => rows.map((row) => row.time ? row : ({ time: row.x, value: row.y })).filter((row) => row.time && row.value !== null && row.value !== undefined);
const ohlcData = (rows = []) => rows.map((row) => row.time ? row : ({ time: row.x, open: row.open, high: row.high, low: row.low, close: row.close }));
const volumeData = (rows = []) => rows.map((row) => row.time ? row : ({ time: row.x, value: row.y, color: row.color }));
const alignLineToTimeline = (rows, timeline) => {
  const values = new Map(lineData(rows).map((row) => [String(row.time), row]));
  return timeline.map((time) => values.get(String(time)) || { time });
};

function normalize(payload) {
  const uniqueMarkers = (rows) => [...new Map(rows.filter((row) => row?.time).map((row) => [`${row.time}|${row.position}|${row.text}|${row.shape}`, row])).values()];
  const ohlc = ohlcData(payload.ohlc);
  const timeline = ohlc.map((row) => row.time);
  return {
    ...payload,
    ohlc,
    volume: volumeData(payload.volume),
    ma5: lineData(payload.ma || payload.ma5 || payload.zx_short_trend),
    ma20: lineData(payload.ma20 || payload.zx_multi_trend),
    volumeMa: lineData(payload.volMa || payload.volume_ma20),
    volumeThreshold: lineData(payload.volThreshold),
    k: alignLineToTimeline(payload.kdjK || payload.k, timeline),
    d: alignLineToTimeline(payload.kdjD || payload.d, timeline),
    j: alignLineToTimeline(payload.kdjJ || payload.j, timeline),
    defense: lineData(payload.ma5Stop),
    defense25: lineData(payload.ma5Stop25),
    markers: uniqueMarkers([...(payload.markers || []), ...(payload.entryMarkers || []), ...(payload.exitMarkers || [])]),
    signals: uniqueMarkers([...(payload.signalMarkers || []), ...(payload.holdBuyMarkers || []), ...(payload.holdSellMarkers || []), ...(payload.signals || []).map((row) => ({ time: row.x, position: "belowBar", color: "#089981", shape: "circle", text: row.text || "B" }))]),
    holdingPeriods: payload.holdingPeriods || [],
  };
}

function ChartToggle({ active, label, onClick }) {
  return <button type="button" className={`chart-chip ${active ? "active" : ""}`} aria-pressed={active} onClick={onClick}>{label}</button>;
}

export function StrategyChart({ market, symbol, params = {}, title, onClose, className = "", data = null, showSignals = false }) {
  const themeMode = useThemeMode();
  const [preset, setPreset] = usePersistentState("chart.preset", params.preset || "1y");
  const [controls, setControls] = usePersistentState("chart.controls", defaultControls);
  const [payload, setPayload] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const priceRef = useRef(null); const kdjRef = useRef(null); const holdingRef = useRef(null);
  const visibleRangeRef = useRef(null); const chartRuntimeRef = useRef(null); const chartPayloadRef = useRef(null);
  const url = useMemo(() => data ? "" : `${market === "cn" ? "/cn/watchlist/chart" : "/watchlist/chart"}?${toQuery({ ...params, symbol, preset })}`, [data, market, params, preset, symbol]);

  useEffect(() => {
    if (typeof controls.holding !== "boolean") setControls((current) => ({ ...current, holding: true }));
  }, [controls.holding, setControls]);

  useEffect(() => {
    if (data) { setPayload(normalize(data)); setLoading(false); setError(""); return undefined; }
    let cancelled = false; setLoading(true); setError("");
    getJson(url).then((data) => { if (!cancelled) setPayload(normalize(data)); }).catch((exception) => { if (!cancelled) setError(exception.message); }).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [data, url]);

  useEffect(() => {
    if (!payload?.ohlc?.length || !priceRef.current || !kdjRef.current) return undefined;
    if (chartPayloadRef.current !== payload) { visibleRangeRef.current = null; chartPayloadRef.current = payload; }
    const isLight = themeMode === "light";
    const chartColors = isLight ? { background: "#ffffff", text: "#5f6673", grid: "#edf0f4", border: "#d6dbe3", crosshair: "#87909d", crosshairLabel: "#596474" } : { background: "#101722", text: "#9aa6b5", grid: "#1b2632", border: "#2a3745", crosshair: "#5d6b7a", crosshairLabel: "#334155" };
    const options = { layout: { background: { type: ColorType.Solid, color: chartColors.background }, textColor: chartColors.text, fontFamily: "Inter, Microsoft YaHei UI, sans-serif" }, grid: { vertLines: { color: chartColors.grid }, horzLines: { color: chartColors.grid } }, rightPriceScale: { borderColor: chartColors.border, minimumWidth: 72 }, timeScale: { borderColor: chartColors.border, rightOffset: 6, barSpacing: 8, minBarSpacing: 3 }, crosshair: { mode: CrosshairMode.Normal, vertLine: { color: chartColors.crosshair, labelBackgroundColor: chartColors.crosshairLabel }, horzLine: { color: chartColors.crosshair, labelBackgroundColor: chartColors.crosshairLabel } }, handleScroll: { mouseWheel: true, pressedMouseMove: true }, handleScale: { mouseWheel: true, pinch: true, axisPressedMouseMove: true } };
    const priceChart = createChart(priceRef.current, { ...options, width: priceRef.current.clientWidth, height: priceRef.current.clientHeight, rightPriceScale: { borderColor: chartColors.border, minimumWidth: 72, scaleMargins: { top: .08, bottom: .28 } } });
    const candle = priceChart.addCandlestickSeries({ upColor: "#26a69a", downColor: "#ef5350", borderUpColor: "#26a69a", borderDownColor: "#ef5350", wickUpColor: "#26a69a", wickDownColor: "#ef5350", priceLineVisible: false });
    candle.setData(payload.ohlc);
    const ma5Series = priceChart.addLineSeries({ color: "#f5a623", lineWidth: 2, title: "MA5", priceLineVisible: false });
    const ma20Series = priceChart.addLineSeries({ color: "#94a3b8", lineWidth: 1, title: "MA20", priceLineVisible: false, lastValueVisible: false });
    const defense25Series = priceChart.addLineSeries({ color: "#dc2626", lineWidth: 1, title: "MA5 -2.5%", priceLineVisible: false, lastValueVisible: false });
    const defenseSeries = priceChart.addLineSeries({ color: "#ef4444", lineWidth: 1, lineStyle: LineStyle.Dashed, title: `MA5 -${payload.ma5StopPct || 7.5}%`, priceLineVisible: false, lastValueVisible: false });
    const volumeSeries = priceChart.addHistogramSeries({ priceScaleId: "", priceFormat: { type: "volume" }, priceLineVisible: false, lastValueVisible: false });
    const volumeMaSeries = priceChart.addLineSeries({ color: "#2962ff", lineWidth: 1, priceScaleId: "", title: "成交量均线", priceLineVisible: false });
    const thresholdSeries = priceChart.addLineSeries({ color: "#f97316", lineWidth: 1, lineStyle: LineStyle.Dashed, priceScaleId: "", title: `${payload.volMultiplier || 1.45}x Vol`, priceLineVisible: false });
    priceChart.priceScale("").applyOptions({ scaleMargins: { top: .78, bottom: 0 } });
    const kdjChart = createChart(kdjRef.current, { ...options, width: kdjRef.current.clientWidth, height: kdjRef.current.clientHeight });
    const kSeries = kdjChart.addLineSeries({ color: "#4c8dff", lineWidth: 1, title: "K", priceLineVisible: false });
    const dSeries = kdjChart.addLineSeries({ color: "#f5b942", lineWidth: 1, title: "D", priceLineVisible: false });
    const jSeries = kdjChart.addLineSeries({ color: isLight ? "#596474" : "#d7dee8", lineWidth: 2, title: "J", priceLineVisible: false });
    let syncing = false;
    let holdingFrame = null;
    let holdingEnabled = controls.holding !== false;
    const renderHoldingPeriods = () => {
      if (!holdingRef.current || !priceRef.current) return;
      holdingRef.current.replaceChildren();
      const plotCanvas = priceRef.current.querySelector("canvas");
      if (!plotCanvas) return;
      const chartRect = priceRef.current.getBoundingClientRect();
      const plotRect = plotCanvas.getBoundingClientRect();
      const plotLeft = plotRect.left - chartRect.left;
      const spacing = priceChart.timeScale().options().barSpacing || 8;
      const width = plotRect.width;
      holdingRef.current.style.left = `${plotLeft}px`;
      holdingRef.current.style.right = "auto";
      holdingRef.current.style.width = `${width}px`;
      (holdingEnabled ? payload.holdingPeriods : []).forEach((period) => {
        const startX = priceChart.timeScale().timeToCoordinate(period.start);
        const endX = priceChart.timeScale().timeToCoordinate(period.end);
        if (startX === null || endX === null) return;
        const left = Math.max(0, Math.min(startX, endX) - spacing * .5);
        const right = Math.min(width, Math.max(startX, endX) + spacing * .5);
        if (right <= left) return;
        const band = document.createElement("div"); band.className = "native-holding-band"; band.style.left = `${left}px`; band.style.width = `${right - left}px`; band.title = `${period.label || "持仓"}: ${period.start} - ${period.end}`; holdingRef.current.appendChild(band);
      });
    };
    const scheduleHoldingPeriods = () => { if (holdingFrame !== null) cancelAnimationFrame(holdingFrame); holdingFrame = requestAnimationFrame(() => { holdingFrame = null; renderHoldingPeriods(); }); };
    const applyControls = (current) => {
      ma5Series.setData(current.ma5 ? payload.ma5 : []);
      ma20Series.setData(current.ma20 ? payload.ma20 : []);
      defense25Series.setData(current.defense25 ? payload.defense25 : []);
      defenseSeries.setData(current.defense ? payload.defense : []);
      volumeSeries.setData(current.volume ? payload.volume : []);
      volumeMaSeries.setData(current.volume ? payload.volumeMa : []);
      thresholdSeries.setData(current.volume ? payload.volumeThreshold : []);
      kSeries.setData(current.kdj ? payload.k : []); dSeries.setData(current.kdj ? payload.d : []); jSeries.setData(current.kdj ? payload.j : []);
      candle.setMarkers([...(payload.markers || []), ...((showSignals || current.signals) ? payload.signals : [])].sort((a, b) => String(a.time).localeCompare(String(b.time))));
      holdingEnabled = current.holding !== false;
      scheduleHoldingPeriods();
    };
    const runtime = { applyControls };
    chartRuntimeRef.current = runtime;
    applyControls(controls);
    priceChart.timeScale().subscribeVisibleLogicalRangeChange((range) => { scheduleHoldingPeriods(); if (!range || syncing) return; visibleRangeRef.current = range; syncing = true; kdjChart.timeScale().setVisibleLogicalRange(range); syncing = false; });
    kdjChart.timeScale().subscribeVisibleLogicalRangeChange((range) => { if (!range || syncing) return; syncing = true; priceChart.timeScale().setVisibleLogicalRange(range); syncing = false; });
    if (visibleRangeRef.current) {
      priceChart.timeScale().setVisibleLogicalRange(visibleRangeRef.current);
      kdjChart.timeScale().setVisibleLogicalRange(visibleRangeRef.current);
    } else {
      priceChart.timeScale().fitContent(); kdjChart.timeScale().fitContent();
    }
    const resize = new ResizeObserver(() => { if (priceRef.current) priceChart.applyOptions({ width: priceRef.current.clientWidth, height: priceRef.current.clientHeight }); if (kdjRef.current) kdjChart.applyOptions({ width: kdjRef.current.clientWidth, height: kdjRef.current.clientHeight }); scheduleHoldingPeriods(); }); resize.observe(priceRef.current); resize.observe(kdjRef.current);
    priceRef.current.__fitChart = () => { priceChart.timeScale().fitContent(); kdjChart.timeScale().fitContent(); scheduleHoldingPeriods(); };
    scheduleHoldingPeriods();
    return () => { if (chartRuntimeRef.current === runtime) chartRuntimeRef.current = null; visibleRangeRef.current = priceChart.timeScale().getVisibleLogicalRange() || visibleRangeRef.current; if (holdingFrame !== null) cancelAnimationFrame(holdingFrame); resize.disconnect(); priceChart.remove(); kdjChart.remove(); };
  }, [payload, showSignals, themeMode]);

  useEffect(() => { chartRuntimeRef.current?.applyControls(controls); }, [controls]);

  const toggle = (key) => setControls((current) => ({ ...current, [key]: !current[key] }));
  return <section className={`native-chart ${controls.kdj ? "" : "hide-kdj"} ${className}`} data-execution-markers={payload?.markers?.length || 0} data-signal-markers={payload?.signals?.length || 0} data-holding-periods={payload?.holdingPeriods?.length || 0}>
    <header className="native-chart-toolbar"><strong>{title || symbol}</strong>{data ? null : <div className="native-periods">{periods.map((item) => <button key={item} type="button" className={preset === item ? "active" : ""} onClick={() => setPreset(item)}>{item.toUpperCase()}</button>)}</div>}<span className="chart-separator" />
      <ChartToggle label="MA5" active={controls.ma5} onClick={() => toggle("ma5")} /><ChartToggle label="MA20" active={controls.ma20} onClick={() => toggle("ma20")} /><ChartToggle label="成交量" active={controls.volume} onClick={() => toggle("volume")} /><ChartToggle label="KDJ" active={controls.kdj} onClick={() => toggle("kdj")} />{showSignals ? null : <ChartToggle label="B/S" active={controls.signals} onClick={() => toggle("signals")} />}<ChartToggle label="持仓区间" active={controls.holding !== false} onClick={() => toggle("holding")} /><ChartToggle label="2.5%防守线" active={controls.defense25} onClick={() => toggle("defense25")} /><ChartToggle label={`${payload?.ma5StopPct || 7.5}%止损线`} active={controls.defense} onClick={() => toggle("defense")} />
      <button className="icon-button" type="button" title="适应窗口" onClick={() => priceRef.current?.__fitChart?.()}><Icon name="expand" /></button>{onClose ? <button className="icon-button" type="button" title="关闭" onClick={onClose}><Icon name="close" /></button> : null}</header>
    <div className="native-chart-body">{loading ? <div className="frame-loading"><span /><b>正在加载图表</b></div> : null}{error ? <div className="frame-error"><strong>{error}</strong><button type="button" onClick={() => setPreset((current) => current)}>重试</button></div> : null}<div className="native-price-wrap"><div ref={priceRef} className="native-price-chart" /><div ref={holdingRef} className="native-holding-periods" /></div><div ref={kdjRef} className="native-kdj-chart" /></div>
  </section>;
}
