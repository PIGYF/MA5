import React, { useEffect, useRef } from "react";
import { ColorType, CrosshairMode, LineStyle, createChart } from "lightweight-charts";
import { useThemeMode } from "./lib";

const line = (rows = []) => rows.filter((row) => row?.time && row?.value !== null && row?.value !== undefined);

export function ReboundChart({ payload, title }) {
  const themeMode = useThemeMode();
  const priceRef = useRef(null);
  const rsiRef = useRef(null);
  const biasRef = useRef(null);
  const latestValue = (rows = []) => {
    const values = line(rows);
    return values.length ? Number(values[values.length - 1].value) : null;
  };
  const maLegend = [
    ["MA5", "#f5a623", latestValue(payload?.ma5)],
    ["MA20", "#4c8dff", latestValue(payload?.ma20)],
    ["MA60", "#ab47bc", latestValue(payload?.ma60)],
    ["MA120", "#26a69a", latestValue(payload?.ma120)],
    ["MA180", "#94a3b8", latestValue(payload?.ma180)],
  ].filter(([, , value]) => Number.isFinite(value));

  useEffect(() => {
    if (!payload?.ohlc?.length || !priceRef.current || !rsiRef.current || !biasRef.current) return undefined;
    const light = themeMode === "light";
    const colors = light
      ? { background: "#ffffff", text: "#5f6673", grid: "#edf0f4", border: "#d6dbe3", crosshair: "#87909d" }
      : { background: "#101722", text: "#9aa6b5", grid: "#1b2632", border: "#2a3745", crosshair: "#5d6b7a" };
    const options = {
      layout: { background: { type: ColorType.Solid, color: colors.background }, textColor: colors.text, fontFamily: "Inter, Microsoft YaHei UI, sans-serif" },
      grid: { vertLines: { color: colors.grid }, horzLines: { color: colors.grid } },
      rightPriceScale: { borderColor: colors.border, minimumWidth: 68 },
      timeScale: { borderColor: colors.border, rightOffset: 5, barSpacing: 8, minBarSpacing: 3 },
      crosshair: { mode: CrosshairMode.Normal, vertLine: { color: colors.crosshair }, horzLine: { color: colors.crosshair } },
      handleScroll: { mouseWheel: true, pressedMouseMove: true },
      handleScale: { mouseWheel: true, pinch: true, axisPressedMouseMove: true },
    };
    const priceChart = createChart(priceRef.current, { ...options, width: priceRef.current.clientWidth, height: priceRef.current.clientHeight, rightPriceScale: { ...options.rightPriceScale, scaleMargins: { top: .08, bottom: .25 } } });
    const candle = priceChart.addCandlestickSeries({ upColor: "#26a69a", downColor: "#ef5350", borderUpColor: "#26a69a", borderDownColor: "#ef5350", wickUpColor: "#26a69a", wickDownColor: "#ef5350", priceLineVisible: false });
    candle.setData(payload.ohlc);
    candle.setMarkers([...(payload.markers || []), ...(payload.signals || [])].sort((a, b) => String(a.time).localeCompare(String(b.time))));
    const movingAverage = (rows, title, color, options = {}) => {
      const series = priceChart.addLineSeries({ color, lineWidth: 1, title, priceLineVisible: false, lastValueVisible: false, ...options });
      const data = line(rows);
      series.setData(data);
      if (data.length) series.createPriceLine({ price: Number(data[data.length - 1].value), color, title, lineVisible: false, axisLabelVisible: true });
      return series;
    };
    movingAverage(payload.ma5, "MA5", "#f5a623", { lineWidth: 2 });
    movingAverage(payload.ma20, "MA20", "#4c8dff", { lineWidth: 2 });
    movingAverage(payload.ma60, "MA60", "#ab47bc");
    movingAverage(payload.ma120, "MA120", "#26a69a");
    movingAverage(payload.ma180, "MA180", "#94a3b8", { lineStyle: LineStyle.Dashed });
    const volume = priceChart.addHistogramSeries({ priceScaleId: "", priceFormat: { type: "volume" }, priceLineVisible: false, lastValueVisible: false });
    volume.setData(payload.volume || []);
    priceChart.priceScale("").applyOptions({ scaleMargins: { top: .8, bottom: 0 } });

    const rsiChart = createChart(rsiRef.current, { ...options, width: rsiRef.current.clientWidth, height: rsiRef.current.clientHeight });
    const rsi = rsiChart.addLineSeries({ color: "#f59e0b", lineWidth: 2, title: "RSI", priceLineVisible: false });
    rsi.setData(line(payload.rsi));
    const rsi30 = rsiChart.addLineSeries({ color: "#7c3aed", lineWidth: 1, lineStyle: LineStyle.Dashed, title: "超跌", priceLineVisible: false, lastValueVisible: false });
    rsi30.setData(payload.ohlc.map((row) => ({ time: row.time, value: Number(payload.settings?.oversold_rsi ?? 30) })));
    const rsi35 = rsiChart.addLineSeries({ color: "#64748b", lineWidth: 1, lineStyle: LineStyle.Dotted, title: "触发", priceLineVisible: false, lastValueVisible: false });
    rsi35.setData(payload.ohlc.map((row) => ({ time: row.time, value: Number(payload.settings?.trigger_rsi ?? 35) })));

    const biasChart = createChart(biasRef.current, { ...options, width: biasRef.current.clientWidth, height: biasRef.current.clientHeight });
    const bias6 = biasChart.addLineSeries({ color: "#f5a623", lineWidth: 2, title: "BIAS6", priceLineVisible: false });
    const bias12 = biasChart.addLineSeries({ color: "#4c8dff", lineWidth: 1, title: "BIAS12", priceLineVisible: false });
    const bias24 = biasChart.addLineSeries({ color: "#94a3b8", lineWidth: 1, title: "BIAS24", priceLineVisible: false });
    bias6.setData(line(payload.bias6)); bias12.setData(line(payload.bias12)); bias24.setData(line(payload.bias24));
    const zero = biasChart.addLineSeries({ color: "#64748b", lineWidth: 1, lineStyle: LineStyle.Dotted, priceLineVisible: false, lastValueVisible: false });
    zero.setData(payload.ohlc.map((row) => ({ time: row.time, value: 0 })));

    const charts = [priceChart, rsiChart, biasChart];
    let syncing = false;
    charts.forEach((chart, sourceIndex) => chart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
      if (!range || syncing) return;
      syncing = true;
      charts.forEach((target, targetIndex) => { if (targetIndex !== sourceIndex) target.timeScale().setVisibleLogicalRange(range); });
      syncing = false;
    }));
    charts.forEach((chart) => chart.timeScale().fitContent());
    const resize = new ResizeObserver(() => {
      priceChart.applyOptions({ width: priceRef.current?.clientWidth || 0, height: priceRef.current?.clientHeight || 0 });
      rsiChart.applyOptions({ width: rsiRef.current?.clientWidth || 0, height: rsiRef.current?.clientHeight || 0 });
      biasChart.applyOptions({ width: biasRef.current?.clientWidth || 0, height: biasRef.current?.clientHeight || 0 });
    });
    [priceRef.current, rsiRef.current, biasRef.current].forEach((node) => resize.observe(node));
    return () => { resize.disconnect(); charts.forEach((chart) => chart.remove()); };
  }, [payload, themeMode]);

  return <section className="rebound-chart">
    <header><strong>{title || payload?.symbol || "超跌反弹"}</strong><span><i className="setup-dot" />超跌观察</span><span><i className="signal-dot" />反弹买点</span></header>
    <div className="rebound-price-wrap">
      <div className="rebound-price" ref={priceRef} />
      <div className="rebound-ma-legend" aria-label="均线图例">{maLegend.map(([name, color, value]) => <span key={name}><i style={{ background: color }} /><b>{name}</b><em>{value.toFixed(2)}</em></span>)}</div>
    </div>
    <div className="rebound-indicator"><label>RSI</label><div ref={rsiRef} /></div>
    <div className="rebound-indicator"><label>BIAS</label><div ref={biasRef} /></div>
  </section>;
}
