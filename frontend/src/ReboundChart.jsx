import React, { useEffect, useMemo, useRef, useState } from "react";
import { ColorType, CrosshairMode, LineStyle, createChart } from "lightweight-charts";
import { useThemeMode } from "./lib";

const line = (rows = []) => rows.filter((row) => row?.time && row?.value !== null && row?.value !== undefined);
const timeKey = (time) => {
  if (!time) return "";
  if (typeof time === "string") return time;
  if (typeof time === "number") return String(time);
  return `${time.year}-${String(time.month).padStart(2, "0")}-${String(time.day).padStart(2, "0")}`;
};
const valueMap = (rows = []) => new Map(rows.map((row) => [timeKey(row.time), Number(row.value)]));
const finite = (value) => Number.isFinite(Number(value));
const decimal = (value, digits = 2, suffix = "") => finite(value) ? `${Number(value).toFixed(digits)}${suffix}` : "-";
const compactVolume = (value) => {
  if (!finite(value)) return "-";
  const amount = Number(value);
  if (Math.abs(amount) >= 1e9) return `${(amount / 1e9).toFixed(2)}B`;
  if (Math.abs(amount) >= 1e6) return `${(amount / 1e6).toFixed(2)}M`;
  if (Math.abs(amount) >= 1e3) return `${(amount / 1e3).toFixed(1)}K`;
  return amount.toFixed(0);
};

export function ReboundChart({ payload, title }) {
  const themeMode = useThemeMode();
  const priceRef = useRef(null);
  const rsiRef = useRef(null);
  const biasRef = useRef(null);
  const clearPinRef = useRef(() => {});
  const [cursorData, setCursorData] = useState(null);
  const [cursorPinned, setCursorPinned] = useState(false);

  const latestValue = (rows = []) => {
    const values = line(rows);
    return values.length ? Number(values[values.length - 1].value) : null;
  };
  const maLegend = [
    ["MA5", "#f5a623", cursorData?.ma5 ?? latestValue(payload?.ma5)],
    ["MA20", "#4c8dff", cursorData?.ma20 ?? latestValue(payload?.ma20)],
    ["MA60", "#ab47bc", cursorData?.ma60 ?? latestValue(payload?.ma60)],
    ["MA120", "#26a69a", cursorData?.ma120 ?? latestValue(payload?.ma120)],
    ["MA180", "#94a3b8", cursorData?.ma180 ?? latestValue(payload?.ma180)],
  ].filter(([, , value]) => Number.isFinite(value));

  const cursorItems = useMemo(() => cursorData ? [
    ["开", decimal(cursorData.open)], ["高", decimal(cursorData.high)], ["低", decimal(cursorData.low)], ["收", decimal(cursorData.close)],
    ["成交量", compactVolume(cursorData.volume)], ["MA5", decimal(cursorData.ma5)], ["MA20", decimal(cursorData.ma20)],
    ["MA60", decimal(cursorData.ma60)], ["MA120", decimal(cursorData.ma120)], ["MA180", decimal(cursorData.ma180)],
    ["RSI", decimal(cursorData.rsi, 1)], ["BIAS6", decimal(cursorData.bias6, 2, "%")],
    ["BIAS12", decimal(cursorData.bias12, 2, "%")], ["BIAS24", decimal(cursorData.bias24, 2, "%")],
    ["累计跌幅", decimal(cursorData.decline, 2, "%")],
    ["信号", cursorData.signal || "-"],
  ] : [], [cursorData]);

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
    const chartMarkers = [...(payload.markers || []), ...(payload.signals || [])].sort((a, b) => timeKey(a.time).localeCompare(timeKey(b.time)));
    candle.setMarkers(chartMarkers);
    const movingAverage = (rows, seriesTitle, color, seriesOptions = {}) => {
      const series = priceChart.addLineSeries({ color, lineWidth: 1, title: seriesTitle, priceLineVisible: false, lastValueVisible: false, ...seriesOptions });
      const data = line(rows);
      series.setData(data);
      if (data.length) series.createPriceLine({ price: Number(data[data.length - 1].value), color, title: seriesTitle, lineVisible: false, axisLabelVisible: true });
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

    const ohlcByTime = new Map(payload.ohlc.map((row) => [timeKey(row.time), row]));
    const volumeByTime = valueMap(payload.volume);
    const maps = {
      ma5: valueMap(payload.ma5), ma20: valueMap(payload.ma20), ma60: valueMap(payload.ma60),
      ma120: valueMap(payload.ma120), ma180: valueMap(payload.ma180), rsi: valueMap(payload.rsi),
      bias6: valueMap(payload.bias6), bias12: valueMap(payload.bias12), bias24: valueMap(payload.bias24),
      decline: valueMap(payload.decline),
    };
    const signalsByTime = new Map();
    chartMarkers.forEach((marker) => {
      const key = timeKey(marker.time);
      const label = marker.text || marker.title;
      if (!label) return;
      signalsByTime.set(key, [...(signalsByTime.get(key) || []), label]);
    });
    const readDate = (time) => {
      const key = timeKey(time);
      const bar = ohlcByTime.get(key);
      if (!bar) return;
      setCursorData({
        time: key, open: Number(bar.open), high: Number(bar.high), low: Number(bar.low), close: Number(bar.close),
        volume: volumeByTime.get(key), ma5: maps.ma5.get(key), ma20: maps.ma20.get(key), ma60: maps.ma60.get(key),
        ma120: maps.ma120.get(key), ma180: maps.ma180.get(key), rsi: maps.rsi.get(key), bias6: maps.bias6.get(key),
        bias12: maps.bias12.get(key), bias24: maps.bias24.get(key), decline: maps.decline.get(key),
        signal: (signalsByTime.get(key) || []).join(" / "),
      });
    };

    const charts = [priceChart, rsiChart, biasChart];
    const crosshairSeries = [candle, rsi, bias6];
    let syncingRange = false;
    let syncingCrosshair = false;
    let pinnedTime = "";
    charts.forEach((chart, sourceIndex) => chart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
      if (!range || syncingRange) return;
      syncingRange = true;
      charts.forEach((target, targetIndex) => { if (targetIndex !== sourceIndex) target.timeScale().setVisibleLogicalRange(range); });
      syncingRange = false;
    }));
    const crosshairValue = (chartIndex, key) => {
      if (chartIndex === 0) return Number(ohlcByTime.get(key)?.close);
      if (chartIndex === 1) return maps.rsi.get(key);
      return maps.bias6.get(key);
    };
    const syncCrosshairs = (sourceIndex, time) => {
      const key = timeKey(time);
      if (!key || syncingCrosshair) return;
      syncingCrosshair = true;
      charts.forEach((chart, targetIndex) => {
        if (targetIndex === sourceIndex) return;
        const value = crosshairValue(targetIndex, key);
        if (finite(value)) chart.setCrosshairPosition(Number(value), time, crosshairSeries[targetIndex]);
      });
      syncingCrosshair = false;
    };
    const moveHandlers = charts.map((_, sourceIndex) => (param) => {
      if (syncingCrosshair || pinnedTime || !param.time) return;
      readDate(param.time);
      syncCrosshairs(sourceIndex, param.time);
    });
    const clickHandlers = charts.map((_, sourceIndex) => (param) => {
      if (!param.time) return;
      const key = timeKey(param.time);
      if (pinnedTime === key) {
        pinnedTime = "";
        setCursorPinned(false);
        return;
      }
      pinnedTime = key;
      setCursorPinned(true);
      readDate(param.time);
      syncCrosshairs(sourceIndex, param.time);
    });
    charts.forEach((chart, index) => {
      chart.subscribeCrosshairMove(moveHandlers[index]);
      chart.subscribeClick(clickHandlers[index]);
    });
    clearPinRef.current = () => {
      pinnedTime = "";
      setCursorPinned(false);
    };

    readDate(payload.ohlc[payload.ohlc.length - 1].time);
    charts.forEach((chart) => chart.timeScale().fitContent());
    const resize = new ResizeObserver(() => {
      priceChart.applyOptions({ width: priceRef.current?.clientWidth || 0, height: priceRef.current?.clientHeight || 0 });
      rsiChart.applyOptions({ width: rsiRef.current?.clientWidth || 0, height: rsiRef.current?.clientHeight || 0 });
      biasChart.applyOptions({ width: biasRef.current?.clientWidth || 0, height: biasRef.current?.clientHeight || 0 });
    });
    [priceRef.current, rsiRef.current, biasRef.current].forEach((node) => resize.observe(node));
    return () => {
      clearPinRef.current = () => {};
      resize.disconnect();
      charts.forEach((chart, index) => {
        chart.unsubscribeCrosshairMove(moveHandlers[index]);
        chart.unsubscribeClick(clickHandlers[index]);
        chart.remove();
      });
    };
  }, [payload, themeMode]);

  return <section className="rebound-chart">
    <header><strong>{title || payload?.symbol || "超跌反弹"}</strong><span><i className="setup-dot" />超跌观察</span><span><i className="signal-dot" />反弹买点</span></header>
    <div className="rebound-cursor-strip" aria-live="polite">
      <div className="rebound-cursor-date"><small>交易日</small><b>{cursorData?.time || "-"}</b></div>
      <div className="rebound-cursor-values">{cursorItems.map(([label, value]) => <span key={label}><small>{label}</small><b>{value}</b></span>)}</div>
      <button type="button" className={cursorPinned ? "active" : ""} onClick={() => clearPinRef.current()} disabled={!cursorPinned} title={cursorPinned ? "解除日期锁定" : "点击图表锁定日期"}>{cursorPinned ? "解除" : "点击锁定"}</button>
    </div>
    <div className="rebound-price-wrap">
      <div className="rebound-price" ref={priceRef} />
      <div className="rebound-ma-legend" aria-label="均线图例">{maLegend.map(([name, color, value]) => <span key={name}><i style={{ background: color }} /><b>{name}</b><em>{value.toFixed(2)}</em></span>)}</div>
    </div>
    <div className="rebound-indicator"><label>RSI</label><div ref={rsiRef} /></div>
    <div className="rebound-indicator"><label>BIAS</label><div ref={biasRef} /></div>
  </section>;
}
