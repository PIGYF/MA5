import React, { useEffect, useState } from "react";
import { backtestDates, getJson, toQuery } from "./lib";
import { Checkbox, ChartFrame, Field, FilterSection, Icon, PageToolbar, WorkspaceEmpty } from "./ui";

function Toggle({ form, setForm, name, label }) {
  return <Checkbox label={label} checked={form[name]} onChange={(checked) => setForm({ ...form, [name]: checked })} />;
}

function Input({ form, setForm, name, label, type = "number", step = "any" }) {
  return <Field label={label}><input type={type} step={step} value={form[name] ?? ""} onChange={(event) => setForm({ ...form, [name]: event.target.value })} /></Field>;
}

export function Home({ navigate }) {
  const actions = [
    { page: "scan", icon: "scan", title: "盘后选股", note: "扫描下一交易日可执行的 B 点" },
    { page: "watchlist", icon: "star", title: "自选池", note: "跟踪关注股票与策略图表" },
    { page: "backtest", icon: "chart", title: "单票回测", note: "验证策略、指数与持续持有表现" },
    { page: "batch", icon: "batch", title: "批量回测", note: "按总资金观察组合收益" },
  ];
  return <><PageToolbar title="策略工作台" subtitle="选择市场后进入同一套选股、跟踪和回测流程" /><section className="launch-list">{actions.map((item) => <button key={item.page} type="button" onClick={() => navigate("us", item.page)}><Icon name={item.icon} /><span><strong>{item.title}</strong><small>{item.note}</small></span><b>进入</b></button>)}</section></>;
}

export function Watchlist({ market, items, reload }) {
  const [symbol, setSymbol] = useState("");
  const [selected, setSelected] = useState(items[0]?.symbol || "");
  const [error, setError] = useState("");
  const prefix = `/api/${market}`;
  const selectedItem = items.find((item) => item.symbol === selected) || items[0];

  useEffect(() => {
    if (!items.length) setSelected("");
    else if (!items.some((item) => item.symbol === selected)) setSelected(items[0].symbol);
  }, [items, selected]);

  async function add(event) {
    event.preventDefault(); setError("");
    try { await getJson(`${prefix}/watchlist/add?${toQuery({ symbol, group: "观察" })}`); setSymbol(""); await reload(market); }
    catch (exception) { setError(exception.message); }
  }

  async function remove(symbolToDelete) {
    setError("");
    try { await getJson(`${prefix}/watchlist/delete?${toQuery({ symbol: symbolToDelete })}`); await reload(market); }
    catch (exception) { setError(exception.message); }
  }

  const chartSrc = selectedItem ? (market === "cn" ? `/cn/candidate/chart?${toQuery({ symbol: selectedItem.symbol })}` : `/candidate/chart?${toQuery({ symbol: selectedItem.symbol, preset: "1y", fast: 1 })}`) : "";
  return <>
    <PageToolbar title={`${market === "cn" ? "A股" : "美股"}自选池`} subtitle="左侧管理股票，右侧直接查看策略图表" />
    {error ? <div className="message error">{error}</div> : null}
    <section className="watch-workspace">
      <aside className="watch-rail">
        <form onSubmit={add}><input required placeholder={market === "cn" ? "代码或中文名称" : "输入股票代码"} value={symbol} onChange={(event) => setSymbol(event.target.value.toUpperCase())} /><button type="submit" title="加入自选"><Icon name="plus" /></button></form>
        <div className="watch-list">{items.length ? items.map((item) => <button key={item.symbol} type="button" className={selectedItem?.symbol === item.symbol ? "active" : ""} onClick={() => setSelected(item.symbol)}><span><strong>{item.symbol}</strong><small>{item.name || item.group || "观察"}</small></span><i role="button" tabIndex="0" title="删除" onClick={(event) => { event.stopPropagation(); remove(item.symbol); }}><Icon name="trash" /></i></button>) : <div className="empty">暂无自选股票</div>}</div>
      </aside>
      <section className="watch-chart">{selectedItem ? <><header><div><strong>{selectedItem.symbol}</strong><span>{selectedItem.name || selectedItem.note || selectedItem.sector || "策略图表"}</span></div></header><iframe src={chartSrc} title={`${selectedItem.symbol} 图表`} /></> : <div className="empty">从左侧加入或选择一只股票</div>}</section>
    </section>
  </>;
}

function StrategyFields({ market, form, setForm }) {
  return <div className="advanced-grid">
    <Input form={form} setForm={setForm} name="ma_length" label="MA周期" />
    <Input form={form} setForm={setForm} name="vol_length" label="均量周期" />
    <Input form={form} setForm={setForm} name="vol_high_days" label="连续放量天数" />
    <Input form={form} setForm={setForm} name="vol_high_multiplier" label="连续放量倍数" />
    <Input form={form} setForm={setForm} name="vol_multiplier" label="巨量倍数" />
    <Input form={form} setForm={setForm} name="massive_window" label="巨量窗口" />
    <Input form={form} setForm={setForm} name="massive_min_count" label="巨量最少次数" />
    <Input form={form} setForm={setForm} name="reentry_pct" label="B2回踩距离（%）" />
    <Input form={form} setForm={setForm} name="stop_5ma_pct" label="MA5防守（%）" />
    <Input form={form} setForm={setForm} name="below_20ma_stop_days" label="跌破MA20天数" />
    <Input form={form} setForm={setForm} name="hard_stop_pct" label="成本止损（%）" />
    <div className="check-grid wide">
      <Toggle form={form} setForm={setForm} name="require_ma5_rising" label="MA5 向上" />
      <Toggle form={form} setForm={setForm} name="require_5ma_gt_20ma" label="MA5 > MA20" />
      <Toggle form={form} setForm={setForm} name="b1_require_20ma_gt_50ma" label="20MA > 50MA" />
      <Toggle form={form} setForm={setForm} name="secondary_big_red_b1" label="大阴线 B1" />
      <Toggle form={form} setForm={setForm} name="secondary_above_ma5_3d" label="连续三天 > MA5" />
    </div>
    {market === "cn" ? <Input form={form} setForm={setForm} name="max_buy_gap_pct" label="最大高开（%）" /> : null}
  </div>;
}

export function Backtest({ market, defaults }) {
  const dates = backtestDates(defaults?.end);
  const [form, setForm] = useState(() => ({
    ...defaults, symbol: market === "cn" ? "600487" : "MU", preset: "1y", start: dates.start, end: dates.end,
    benchmark: "^IXIC", initial_cash: 100000, commission_pct: market === "cn" ? 0.03 : 0.1,
    stamp_duty_pct: 0.05, slippage_pct: market === "cn" ? 0.3 : 0, max_buy_gap_pct: 6,
    stop_5ma_pct: 7.5, below_20ma_stop_days: 2, hard_stop_pct: 20,
  }));
  const [src, setSrc] = useState("");

  useEffect(() => {
    const nextDates = backtestDates(defaults?.end);
    setForm((current) => ({ ...current, ...defaults, start: nextDates.start, end: nextDates.end }));
    setSrc("");
  }, [defaults, market]);

  function run(event) {
    event.preventDefault();
    setSrc(`${market === "cn" ? "/cn/run/frame" : "/run/frame"}?${toQuery({ ...form, strategy_name: "ratchet" })}`);
  }

  return <>
    <PageToolbar title={`${market === "cn" ? "A股" : "美股"}单票回测`} subtitle="信号后下一交易日开盘执行；周期优先于自定义日期" />
    <section className="backtest-workspace">
      <form className="backtest-rail" onSubmit={run}>
        <div className="rail-title"><Icon name="chart" /><strong>回测设置</strong><span>{market === "cn" ? "A Share" : "US"}</span></div>
        <div className="backtest-fields">
        <Input form={form} setForm={setForm} name="symbol" label="股票代码" type="text" />
        <Field label="回测周期"><select value={form.preset} onChange={(event) => setForm({ ...form, preset: event.target.value })}><option value="3m">3个月</option><option value="6m">6个月</option><option value="1y">1年</option><option value="3y">3年</option><option value="custom">自定义</option></select></Field>
        <Input form={form} setForm={setForm} name="start" label="开始日期" type="date" />
        <Input form={form} setForm={setForm} name="end" label="结束日期" type="date" />
        <Input form={form} setForm={setForm} name="initial_cash" label="初始资金" />
        <button className="primary-action" type="submit"><Icon name="play" />运行回测</button>
        </div>
        <FilterSection title="策略与交易参数" note="高级"><StrategyFields market={market} form={form} setForm={setForm} /></FilterSection>
      </form>
      <section className="backtest-canvas">{src ? <ChartFrame title={`${form.symbol} 回测结果`} src={src} className="report-frame" onClose={() => setSrc("")} /> : <WorkspaceEmpty title="回测结果" note="当前尚未运行回测" />}</section>
    </section>
  </>;
}

export function BatchBacktest({ market, defaults }) {
  const dates = backtestDates(defaults?.end);
  const [form, setForm] = useState(() => ({ ...defaults, symbols: "MU,RKLB,NVDA", preset: "1y", start: dates.start, end: dates.end, initial_cash: 100000, position_cash: 10000 }));
  const [src, setSrc] = useState("");
  if (market === "cn") return <><PageToolbar title="A股批量回测" subtitle="A股批量组合回测尚未启用" /><WorkspaceEmpty title="A股批量回测暂未启用" note="当前可使用 A股单票回测" /></>;
  function run(event) { event.preventDefault(); setSrc(`/batch/run/frame?${toQuery(form)}`); }
  return <><PageToolbar title="美股批量回测" subtitle="按总资金和单票投入金额观察组合收益" /><section className="backtest-workspace"><form className="backtest-rail" onSubmit={run}><div className="rail-title"><Icon name="batch" /><strong>组合设置</strong><span>US</span></div><div className="backtest-fields"><Field label="股票列表"><textarea rows="3" value={form.symbols} onChange={(event) => setForm({ ...form, symbols: event.target.value })} /></Field><Field label="回测周期"><select value={form.preset} onChange={(event) => setForm({ ...form, preset: event.target.value })}><option value="3m">3个月</option><option value="6m">6个月</option><option value="1y">1年</option><option value="3y">3年</option><option value="custom">自定义</option></select></Field><Input form={form} setForm={setForm} name="initial_cash" label="总资金" /><Input form={form} setForm={setForm} name="position_cash" label="单票资金" /><button className="primary-action" type="submit"><Icon name="play" />运行批量回测</button></div><FilterSection title="策略参数" note="高级"><StrategyFields market="us" form={form} setForm={setForm} /></FilterSection></form><section className="backtest-canvas">{src ? <ChartFrame title="批量回测结果" src={src} className="report-frame" onClose={() => setSrc("")} /> : <WorkspaceEmpty title="批量回测结果" note="当前尚未运行批量回测" />}</section></section></>;
}
