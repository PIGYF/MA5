import React, { useEffect, useMemo, useState } from "react";
import { backtestDates, getJson, numberText, toQuery, usePersistentState } from "./lib";
import { Checkbox, Field, FilterSection, Icon, PageToolbar, ResizableWorkspace, WorkspaceEmpty } from "./ui";
import { ReboundChart } from "./ReboundChart";

const defaultForm = (end) => ({
  symbol: "NVDA", preset: "1y", ...backtestDates(end), initial_cash: 100000, commission_pct: 0.1, slippage_pct: 0,
  percentile_lookback_years: 1, rsi_percentile: 10, bias_mid_percentile: 10,
  decline_days: 5, decline_pct: -10,
  wait_days: 5, trigger_rsi: 35,
  require_bias_mid_turn: true, require_previous_high_break: true, require_bullish_candle: true,
  require_volume_confirmation: true, volume_length: 5, volume_multiplier: 1,
  low_stop_buffer_pct: 2, hard_stop_pct: 8, exit_rsi: 60, target_bias_long: -2, max_hold_days: 10,
});

function useReboundForm(storageKey, end) {
  const [stored, setStored] = usePersistentState(storageKey, () => defaultForm(end));
  return [{ ...defaultForm(end), ...stored }, setStored];
}

function Input({ form, setForm, name, label, step = "any", type = "number" }) {
  return <Field label={label}><input type={type} step={step} value={form[name] ?? ""} onChange={(event) => setForm({ ...form, [name]: event.target.value })} /></Field>;
}

function Toggle({ form, setForm, name, label }) {
  return <Checkbox label={label} checked={form[name]} onChange={(checked) => setForm({ ...form, [name]: checked })} />;
}

function ReboundParameters({ form, setForm, compact = false }) {
  return <>
    <FilterSection title="动态超跌" note="仅使用当日前历史分位数">
      <div className="advanced-grid">
        <Input form={form} setForm={setForm} name="percentile_lookback_years" label="历史回看（年）" step="1" />
        <Input form={form} setForm={setForm} name="rsi_percentile" label="RSI14分位数（%）" />
        <Input form={form} setForm={setForm} name="bias_mid_percentile" label="BIAS12分位数（%）" />
        <Input form={form} setForm={setForm} name="decline_days" label="累计跌幅周期" step="1" />
        <Input form={form} setForm={setForm} name="decline_pct" label="累计跌幅阈值（%）" />
      </div>
    </FilterSection>
    <FilterSection title="止跌确认" note="超跌后观察触发">
      <div className="advanced-grid"><Input form={form} setForm={setForm} name="wait_days" label="等待窗口（天）" step="1" /><Input form={form} setForm={setForm} name="trigger_rsi" label="RSI上穿值" /><Input form={form} setForm={setForm} name="volume_length" label="成交量均线" step="1" /><Input form={form} setForm={setForm} name="volume_multiplier" label="确认量能倍数" /></div>
      <div className="check-grid wide"><Toggle form={form} setForm={setForm} name="require_bias_mid_turn" label="BIAS12止跌" /><Toggle form={form} setForm={setForm} name="require_previous_high_break" label="突破前日最高价" /><Toggle form={form} setForm={setForm} name="require_bullish_candle" label="收阳线" /><Toggle form={form} setForm={setForm} name="require_volume_confirmation" label="成交量确认" /></div>
    </FilterSection>
    <FilterSection title="退出规则" note="快进快出">
      <div className="advanced-grid"><Input form={form} setForm={setForm} name="low_stop_buffer_pct" label="低点止损缓冲（%）" /><Input form={form} setForm={setForm} name="hard_stop_pct" label="最大止损（%）" /><Input form={form} setForm={setForm} name="exit_rsi" label="RSI止盈" /><Input form={form} setForm={setForm} name="target_bias_long" label="BIAS24修复目标（%）" /><Input form={form} setForm={setForm} name="max_hold_days" label="最长持仓（天）" step="1" /></div>
    </FilterSection>
  </>;
}

function changePreset(form, setForm, preset) {
  if (preset === "custom") { setForm({ ...form, preset }); return; }
  const end = new Date(`${form.end}T00:00:00`); const start = new Date(end);
  if (preset === "3m") start.setMonth(start.getMonth() - 3);
  if (preset === "6m") start.setMonth(start.getMonth() - 6);
  if (preset === "1y") start.setFullYear(start.getFullYear() - 1);
  if (preset === "3y") start.setFullYear(start.getFullYear() - 3);
  setForm({ ...form, preset, start: start.toISOString().slice(0, 10) });
}

function ReboundResult({ result }) {
  if (!result) return <WorkspaceEmpty title="超跌反弹回测" note="设置参数后运行，查看超跌观察与实际买卖点" />;
  const metrics = [
    ["策略收益", `${numberText(result.summary?.return_pct)}%`], ["交易次数", result.summary?.trades ?? 0],
    ["胜率", `${numberText(result.summary?.win_rate)}%`], ["最大回撤", `${numberText(result.summary?.max_drawdown)}%`],
    ["超跌事件", result.summary?.setups ?? 0], ["止跌信号", result.summary?.signals ?? 0],
  ];
  return <section className="rebound-result">
    <div className="report-metrics">{metrics.map(([label, value]) => <span key={label}><small>{label}</small><strong>{value}</strong></span>)}</div>
    <ReboundChart payload={result.chart} title={`${result.symbol} · 超跌反弹`} />
    <details className="native-trades" open><summary>信号明细 · {result.events?.length || 0} 个</summary><div className="table-wrap"><table><thead><tr><th>日期</th><th>类型</th><th>收盘价</th><th>RSI14 / 动态阈值</th><th>BIAS12 / 动态阈值</th><th>5日跌幅</th></tr></thead><tbody>{result.events?.length ? result.events.map((event, index) => <tr key={`${event.date}-${event.type}-${index}`}><td>{event.date}</td><td><span className={event.type === "超跌" ? "signal-tag setup" : "signal-tag confirmed"}>{event.type}</span></td><td>{numberText(event.close)}</td><td>{numberText(event.rsi)} / {numberText(event.rsi_threshold)}</td><td>{numberText(event.bias12)}% / {numberText(event.bias12_threshold)}%</td><td>{numberText(event.decline)}%</td></tr>) : <tr><td colSpan="6">区间内没有超跌或止跌信号</td></tr>}</tbody></table></div></details>
    <details className="native-trades"><summary>交易明细 · {result.trades?.length || 0} 笔</summary><div className="table-wrap"><table><thead><tr><th>信号日</th><th>买入日</th><th>卖出日</th><th>收益率</th><th>持仓</th><th>退出原因</th></tr></thead><tbody>{result.trades?.length ? result.trades.map((trade, index) => <tr key={`${trade.entry_date}-${index}`}><td>{trade.entry_signal_date}</td><td>{trade.entry_date}</td><td>{trade.exit_date}</td><td className={Number(trade.pnl_pct) >= 0 ? "gain-up" : "gain-down"}>{numberText(trade.pnl_pct)}%</td><td>{trade.bars_held}日</td><td>{trade.reason}</td></tr>) : <tr><td colSpan="6">区间内没有完成交易</td></tr>}</tbody></table></div></details>
  </section>;
}

export function ReboundBacktest({ defaults }) {
  const [form, setForm] = useReboundForm("rebound.backtest.form", defaults?.end);
  const [result, setResult] = useState(null); const [error, setError] = useState(""); const [loading, setLoading] = useState(false);
  async function run(event) { event.preventDefault(); setError(""); setLoading(true); try { setResult(await getJson(`/api/us/rebound/analyze?${toQuery(form)}`)); } catch (exception) { setError(exception.message); } finally { setLoading(false); } }
  return <><PageToolbar title="超跌反弹 · 参数回测" subtitle="超跌进入观察，反弹确认后下一交易日开盘买入；所有阈值均可调整" />{error ? <div className="message error">{error}</div> : null}<ResizableWorkspace storageKey="rebound.backtest.rail" className="backtest-workspace rebound-workspace" initial={315} min={270} max={480}><form className="backtest-rail" onSubmit={run}><div className="rail-title"><Icon name="chart" /><strong>反弹参数</strong><span>US</span></div><div className="backtest-fields"><Input form={form} setForm={setForm} name="symbol" label="股票代码" type="text" /><Field label="回测周期"><select value={form.preset} onChange={(event) => changePreset(form, setForm, event.target.value)}><option value="3m">3个月</option><option value="6m">6个月</option><option value="1y">1年</option><option value="3y">3年</option><option value="custom">自定义</option></select></Field><Field label="开始日期"><input type="date" value={form.start} onChange={(event) => setForm({ ...form, start: event.target.value, preset: "custom" })} /></Field><Field label="结束日期"><input type="date" value={form.end} onChange={(event) => setForm({ ...form, end: event.target.value, preset: "custom" })} /></Field><button className="primary-action" type="submit" disabled={loading}><Icon name="play" />{loading ? "正在计算" : "运行反弹回测"}</button></div><ReboundParameters form={form} setForm={setForm} /></form><section className="backtest-canvas">{loading ? <div className="frame-loading"><span /><b>正在计算反弹信号</b></div> : <ReboundResult result={result} />}</section></ResizableWorkspace></>;
}

export function ReboundWatchlist() {
  const [items, setItems] = useState([]); const [symbol, setSymbol] = useState(""); const [selected, setSelected] = useState("");
  const [form, setForm] = useReboundForm("rebound.watchlist.form");
  const [appliedForm, setAppliedForm] = useState(form);
  const [result, setResult] = useState(null); const [error, setError] = useState(""); const [loading, setLoading] = useState(false);
  async function reload() { const payload = await getJson("/api/us/rebound/watchlist"); setItems(payload.items || []); }
  useEffect(() => { reload().catch((exception) => setError(exception.message)); }, []);
  useEffect(() => { if (!selected && items.length) setSelected(items[0].symbol); }, [items, selected]);
  useEffect(() => { if (!selected) { setResult(null); return; } let cancelled = false; setLoading(true); getJson(`/api/us/rebound/analyze?${toQuery({ ...appliedForm, symbol: selected })}`).then((payload) => { if (!cancelled) setResult(payload); }).catch((exception) => { if (!cancelled) setError(exception.message); }).finally(() => { if (!cancelled) setLoading(false); }); return () => { cancelled = true; }; }, [selected, appliedForm]);
  async function add(event) { event.preventDefault(); try { await getJson(`/api/us/rebound/watchlist/add?${toQuery({ symbol })}`); setSymbol(""); await reload(); } catch (exception) { setError(exception.message); } }
  async function remove(item) {
    try {
      await getJson(`/api/us/rebound/watchlist/delete?${toQuery({ symbol: item.symbol })}`);
      setItems((current) => current.filter((entry) => entry.symbol !== item.symbol));
      if (selected === item.symbol) {
        setSelected("");
        setResult(null);
      }
      await reload();
    } catch (exception) {
      setError(exception.message);
    }
  }
  return <><PageToolbar title="超跌反弹 · 观察池" subtitle="与MA5自选池分开保存；右侧仅显示反弹信号" />{error ? <div className="message error">{error}</div> : null}<ResizableWorkspace storageKey="rebound.watchlist.rail" className="watch-workspace rebound-watch-workspace" initial={260} min={220} max={420}><aside className="watch-rail"><form onSubmit={add}><input required placeholder="输入美股代码" value={symbol} onChange={(event) => setSymbol(event.target.value.toUpperCase())} /><button type="submit" title="加入超跌反弹观察池"><Icon name="plus" /></button></form><div className="watch-list">{items.length ? items.map((item) => <button key={item.symbol} type="button" className={selected === item.symbol ? "active" : ""} onClick={() => setSelected(item.symbol)}><span className="watch-item-copy"><span><strong>{item.symbol}</strong><b>反弹</b></span><small>{item.group || "超跌观察"}</small></span><i role="button" tabIndex="0" title="删除" onClick={(event) => { event.stopPropagation(); remove(item); }}><Icon name="trash" /></i></button>) : <div className="empty">暂无超跌反弹观察股票</div>}</div><details className="rebound-watch-params"><summary>图表参数</summary><ReboundParameters form={form} setForm={setForm} compact /><button className="primary-action" type="button" onClick={() => setAppliedForm({ ...form })}><Icon name="play" />应用参数</button></details></aside><section className="watch-chart">{loading ? <div className="frame-loading"><span /><b>正在计算反弹信号</b></div> : result ? <ReboundChart payload={result.chart} title={`${selected} · 超跌反弹`} /> : <WorkspaceEmpty title="超跌反弹观察图表" note="从左侧加入或选择一只股票" />}</section></ResizableWorkspace></>;
}
