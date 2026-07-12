import React, { useEffect, useRef, useState } from "react";
import { backtestDates, getJson, toQuery, usePersistentState } from "./lib";
import { LazyStrategyChart } from "./LazyStrategyChart";
import { LazyBacktestReport } from "./LazyBacktestReport";
import { LazyBatchReport } from "./LazyBatchReport";
import { Checkbox, Field, FilterSection, Icon, PageToolbar, ResizableWorkspace, WorkspaceEmpty } from "./ui";

function Toggle({ form, setForm, name, label }) {
  return <Checkbox label={label} checked={form[name]} onChange={(checked) => setForm({ ...form, [name]: checked })} />;
}

function Input({ form, setForm, name, label, type = "number", step = "any" }) {
  return <Field label={label}><input type={type} step={step} value={form[name] ?? ""} onChange={(event) => setForm({ ...form, [name]: event.target.value })} /></Field>;
}

function AShareSymbolInput({ value, onChange, label = "股票代码/名称", bare = false }) {
  const [items, setItems] = useState([]);
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);
  const requestRef = useRef(0);

  useEffect(() => {
    const query = String(value || "").trim();
    if (!query) {
      setItems([]);
      setOpen(false);
      return undefined;
    }
    const requestId = ++requestRef.current;
    const timer = window.setTimeout(async () => {
      try {
        const payload = await getJson(`/cn/suggest?${toQuery({ q: query, limit: 10 })}`);
        if (requestId !== requestRef.current) return;
        const nextItems = payload.suggestions || [];
        setItems(nextItems);
        setActiveIndex(-1);
        setOpen(nextItems.length > 0);
      } catch {
        if (requestId === requestRef.current) setOpen(false);
      }
    }, 120);
    return () => window.clearTimeout(timer);
  }, [value]);

  function select(item) {
    onChange(item.symbol);
    setOpen(false);
  }

  function handleKeyDown(event) {
    if (!open || !items.length) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const direction = event.key === "ArrowDown" ? 1 : -1;
      setActiveIndex((current) => (current + direction + items.length) % items.length);
    } else if (event.key === "Enter" && activeIndex >= 0) {
      event.preventDefault();
      select(items[activeIndex]);
    } else if (event.key === "Escape") {
      setOpen(false);
    }
  }

  const control = <div className="symbol-autocomplete">
    <input required autoComplete="off" placeholder="代码、公司名称或拼音首字母" value={value}
      onChange={(event) => onChange(event.target.value)} onFocus={() => setOpen(items.length > 0)}
      onBlur={() => window.setTimeout(() => setOpen(false), 100)} onKeyDown={handleKeyDown}
      role="combobox" aria-expanded={open} aria-autocomplete="list" />
    {open ? <div className="symbol-suggestions" role="listbox">{items.map((item, index) => <button
      key={item.symbol} type="button" className={index === activeIndex ? "active" : ""}
      onMouseDown={(event) => event.preventDefault()} onClick={() => select(item)}
      role="option" aria-selected={index === activeIndex}>
      <strong>{item.symbol}</strong><span>{item.name}</span><small>{[item.initials, item.exchange].filter(Boolean).join(" / ")}</small>
    </button>)}</div> : null}
  </div>;
  return bare ? control : <Field label={label}>{control}</Field>;
}

export function Home({ market, navigate }) {
  const actions = [
    { page: "scan", icon: "scan", title: "盘后选股", note: "扫描下一交易日可执行的 B 点" },
    { page: "watchlist", icon: "star", title: "自选池", note: "跟踪关注股票与策略图表" },
    { page: "backtest", icon: "chart", title: "单票回测", note: "验证策略、指数与持续持有表现" },
    { page: "batch", icon: "batch", title: "批量回测", note: "按总资金观察组合收益" },
  ];
  return <><PageToolbar title={`${market === "cn" ? "A股" : "美股"}策略工作台`} subtitle="当前市场下统一进入选股、跟踪和回测流程" /><section className="launch-list">{actions.map((item) => <button key={item.page} type="button" onClick={() => navigate(market, item.page)}><Icon name={item.icon} /><span><strong>{item.title}</strong><small>{item.note}</small></span><b>进入</b></button>)}</section><CacheMaintenance /></>;
}

const cacheAreas = [
  { key: "reports", label: "报告文件", action: "清理报告" },
  { key: "prices", label: "行情缓存", action: "清理行情缓存" },
  { key: "us_market", label: "美股市场与公司信息", action: "清理美股缓存" },
  { key: "ashare", label: "A股股票池、行业与K线", action: "清理A股缓存" },
  { key: "latest", label: "最新扫描结果", action: "清理扫描结果" },
];

function CacheMaintenance() {
  const [areas, setAreas] = useState(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");

  async function loadSummary() {
    try {
      const payload = await getJson("/api/cache/summary");
      setAreas(payload.areas || {});
    } catch (exception) {
      setError(exception.message);
    }
  }

  async function clearArea(item) {
    if (!window.confirm(`确认${item.action}？自选池不会被删除，下次使用时会重新拉取相关数据。`)) return;
    setBusy(item.key); setMessage(""); setError("");
    try {
      const payload = await getJson(`/api/cache/clear?${toQuery({ area: item.key })}`);
      setAreas(payload.areas || {});
      setMessage(payload.message || "清理完成。");
    } catch (exception) {
      setError(exception.message);
    } finally {
      setBusy("");
    }
  }

  return <details className="cache-maintenance" onToggle={(event) => { if (event.currentTarget.open && !areas) loadSummary(); }}>
    <summary><span><Icon name="trash" /><strong>缓存维护</strong></span><small>查看占用与清理</small></summary>
    <div className="cache-body">
      {message ? <div className="message success">{message}</div> : null}
      {error ? <div className="message error">{error}</div> : null}
      {!areas && !error ? <div className="cache-loading">正在读取缓存...</div> : null}
      {areas ? <div className="cache-list">{cacheAreas.map((item) => {
        const summary = areas[item.key] || {};
        return <div className="cache-row" key={item.key}><span><strong>{item.label}</strong><small>{Number(summary.files || 0)} 个文件 · {Number(summary.size_mb || 0).toFixed(1)} MB</small></span><button className="danger-action" type="button" disabled={Boolean(busy)} onClick={() => clearArea(item)}>{busy === item.key ? "清理中..." : item.action}</button></div>;
      })}</div> : null}
      <p className="cache-note">部署到网页后，操作的是服务器上的缓存；自选池数据不会被清除。</p>
    </div>
  </details>;
}

export function Watchlist({ market, items, reload }) {
  const [symbol, setSymbol] = useState("");
  const [query, setQuery] = usePersistentState(`watchlist.${market}.query`, "");
  const [sortMode, setSortMode] = usePersistentState(`watchlist.${market}.sort`, "added");
  const orderedItems = React.useMemo(() => [...items].filter((item) => [item.symbol, item.name, item.group, item.sector].join(" ").toLowerCase().includes(query.trim().toLowerCase())).sort((left, right) => {
    if (sortMode === "symbol") return String(left.symbol).localeCompare(String(right.symbol));
    if (sortMode === "performance") return Number(right.performance_pct || -Infinity) - Number(left.performance_pct || -Infinity);
    return String(right.added_at || "").localeCompare(String(left.added_at || ""));
  }), [items, query, sortMode]);
  const [selected, setSelected] = usePersistentState(`watchlist.${market}.selected`, orderedItems[0]?.symbol || "");
  const [error, setError] = useState("");
  const prefix = `/api/${market}`;
  const selectedItem = orderedItems.find((item) => item.symbol === selected) || orderedItems[0];
  const groupedItems = orderedItems.reduce((groups, item) => {
    const date = String(item.added_at || "").slice(0, 10) || "日期未知";
    if (!groups[date]) groups[date] = [];
    groups[date].push(item);
    return groups;
  }, {});
  const watchGroups = Object.entries(groupedItems).sort(([left], [right]) => right.localeCompare(left));

  useEffect(() => {
    if (!items.length) setSelected("");
    else if (!items.some((item) => item.symbol === selected)) setSelected(orderedItems[0].symbol);
  }, [items, selected, orderedItems]);

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

  function openBatch() {
    if (market !== "us") return;
    try {
      const key = "ma5.ui.v1.backtest.us.batch.form";
      const current = JSON.parse(window.localStorage.getItem(key) || "{}");
      window.localStorage.setItem(key, JSON.stringify({ ...current, symbols: orderedItems.map((item) => item.symbol).join(",") }));
      window.history.pushState({}, "", "/app/batch"); window.dispatchEvent(new PopStateEvent("popstate"));
    } catch { window.location.href = "/app/batch"; }
  }
  return <>
    <PageToolbar title={`${market === "cn" ? "A股" : "美股"}自选池`} subtitle="左侧管理股票，右侧直接查看策略图表" actions={market === "us" ? <button className="tool-button" type="button" disabled={!orderedItems.length} onClick={openBatch}><Icon name="batch" />批量回测</button> : null} />
    {error ? <div className="message error">{error}</div> : null}
    <ResizableWorkspace storageKey={`watchlist.${market}.rail`} className="watch-workspace" initial={250} min={220} max={420}>
      <aside className="watch-rail">
        <form onSubmit={add}>{market === "cn" ? <AShareSymbolInput bare value={symbol} onChange={setSymbol} /> : <input required placeholder="输入股票代码" value={symbol} onChange={(event) => setSymbol(event.target.value.toUpperCase())} />}<button type="submit" title="加入自选"><Icon name="plus" /></button></form>
        <div className="watch-tools"><input aria-label="搜索自选股" placeholder="搜索" value={query} onChange={(event) => setQuery(event.target.value)} /><select aria-label="自选股排序" value={sortMode} onChange={(event) => setSortMode(event.target.value)}><option value="added">加入日期</option><option value="symbol">代码</option><option value="performance">涨跌幅</option></select></div>
        <div className="watch-list">{items.length ? watchGroups.map(([date, groupItems]) => <section className="watch-date-group" key={date}><header><strong>{date}</strong><span>{groupItems.length} 只</span></header>{groupItems.map((item) => {
          const gain = Number(item.performance_pct);
          const hasGain = Number.isFinite(gain);
          return <button key={item.symbol} type="button" className={selectedItem?.symbol === item.symbol ? "active" : ""} onClick={() => setSelected(item.symbol)}><span className="watch-item-copy"><span><strong>{item.symbol}</strong><b className={hasGain ? (gain >= 0 ? "gain-up" : "gain-down") : "gain-waiting"}>{hasGain ? `${gain >= 0 ? "+" : ""}${gain.toFixed(2)}%` : "待更新"}</b></span><small>{item.name || item.group || "观察"}</small></span><i role="button" tabIndex="0" title="删除" onClick={(event) => { event.stopPropagation(); remove(item.symbol); }}><Icon name="trash" /></i></button>;
        })}</section>) : <div className="empty">暂无自选股票</div>}</div>
      </aside>
      {selectedItem ? <LazyStrategyChart className="watch-chart" market={market} symbol={selectedItem.symbol} title={`${selectedItem.symbol} · ${selectedItem.name || selectedItem.note || selectedItem.sector || "策略图表"}`} /> : <section className="watch-chart"><div className="empty">从左侧加入或选择一只股票</div></section>}
    </ResizableWorkspace>
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

function AShareExecutionFields({ form, setForm }) {
  return <div className="advanced-grid">
    <Input form={form} setForm={setForm} name="commission_pct" label="手续费（%）" />
    <Input form={form} setForm={setForm} name="stamp_duty_pct" label="卖出印花税（%）" />
    <Input form={form} setForm={setForm} name="slippage_pct" label="滑点（%）" />
    <Input form={form} setForm={setForm} name="max_buy_gap_pct" label="最高可追高开（%）" />
    <Input form={form} setForm={setForm} name="vol_multiplier" label="巨量倍数" />
  </div>;
}

function AShareExitFields({ form, setForm }) {
  return <div className="advanced-grid">
    <Input form={form} setForm={setForm} name="stop_5ma_pct" label="跌破 MA5 止损（%）" />
    <Input form={form} setForm={setForm} name="below_20ma_stop_days" label="连续跌破 MA20 天数" step="1" />
    <Input form={form} setForm={setForm} name="hard_stop_pct" label="硬止损（%）" />
    <Field label="弱趋势卖出" wide>
      <select value={form.weak_trend_exit_mode} onChange={(event) => setForm({ ...form, weak_trend_exit_mode: event.target.value })}>
        <option value="hybrid">混合模式：仅 MA5 &lt; MA20 买入启用</option>
        <option value="off">关闭：全部使用标准止损</option>
        <option value="weak">弱趋势持仓使用修复止损</option>
      </select>
    </Field>
    <Input form={form} setForm={setForm} name="weak_ma5_reclaim_days" label="站回 MA5 期限" step="1" />
    <Input form={form} setForm={setForm} name="weak_ma20_reclaim_days" label="站回 MA20 期限" step="1" />
    <Input form={form} setForm={setForm} name="weak_volume_down_multiplier" label="放量下跌倍数" />
    <Input form={form} setForm={setForm} name="weak_event_low_lookback" label="事件低点窗口" step="1" />
  </div>;
}

function EntryConditionFields({ form, setForm }) {
  return <div className="check-grid backtest-condition-grid">
    <Toggle form={form} setForm={setForm} name="require_ma5_rising" label="MA5 向上" />
    <Toggle form={form} setForm={setForm} name="require_5ma_gt_20ma" label="MA5 > MA20" />
    <Toggle form={form} setForm={setForm} name="b1_require_20ma_gt_50ma" label="MA20 > MA50" />
    <Toggle form={form} setForm={setForm} name="secondary_big_red_b1" label="大阴线 B1" />
    <Toggle form={form} setForm={setForm} name="secondary_above_ma5_3d" label="连续三天 > MA5" />
  </div>;
}

function createBacktestForm(market, defaults, dates) {
  if (market !== "cn") return {
    ...defaults, symbol: "MU", preset: "1y", start: dates.start, end: dates.end,
    benchmark: "^IXIC", initial_cash: 100000, commission_pct: 0.1, slippage_pct: 0,
    stop_5ma_pct: 7.5, below_20ma_stop_days: 2, hard_stop_pct: 20,
  };
  return {
    symbol: "600487", preset: "1y", start: dates.start, end: dates.end, initial_cash: 100000,
    commission_pct: 0.03, stamp_duty_pct: 0.05, slippage_pct: 0.3, max_buy_gap_pct: 6,
    vol_multiplier: 1.45, stop_5ma_pct: 7.5, below_20ma_stop_days: 2, hard_stop_pct: 20,
    weak_trend_exit_mode: "hybrid", weak_ma5_reclaim_days: 5, weak_ma20_reclaim_days: 10,
    weak_volume_down_multiplier: 1.5, weak_event_low_lookback: 27,
    require_ma5_rising: true, require_5ma_gt_20ma: true, b1_require_20ma_gt_50ma: true,
    secondary_big_red_b1: false, secondary_above_ma5_3d: false,
  };
}

export function Backtest({ market, defaults }) {
  const dates = backtestDates(defaults?.end);
  const [form, setForm] = usePersistentState(`backtest.${market}.form`, () => createBacktestForm(market, defaults, dates));
  const [src, setSrc] = usePersistentState(`backtest.${market}.result`, "");

  useEffect(() => {
    const nextDates = backtestDates(defaults?.end);
    setForm((current) => ({ ...createBacktestForm(market, defaults, nextDates), ...current }));
  }, [defaults, market]);

  function changePreset(preset) {
    if (preset === "custom") {
      setForm({ ...form, preset });
      return;
    }
    const endDate = new Date(`${form.end}T00:00:00`);
    const startDate = new Date(endDate);
    if (preset === "3m") startDate.setMonth(startDate.getMonth() - 3);
    if (preset === "6m") startDate.setMonth(startDate.getMonth() - 6);
    if (preset === "1y") startDate.setFullYear(startDate.getFullYear() - 1);
    if (preset === "3y") startDate.setFullYear(startDate.getFullYear() - 3);
    if (preset === "5y") startDate.setFullYear(startDate.getFullYear() - 5);
    setForm({ ...form, preset, start: startDate.toISOString().slice(0, 10) });
  }

  function run(event) {
    event.preventDefault();
    setSrc(`${market === "cn" ? "/cn/run/frame" : "/run/frame"}?${toQuery({ ...form, strategy_name: "ratchet", _report_only: 1 })}`);
  }

  return <>
    <PageToolbar title={`${market === "cn" ? "A股" : "美股"}单票回测`} subtitle="信号后下一交易日开盘执行；周期优先于自定义日期" />
    <ResizableWorkspace storageKey={`backtest.${market}.rail`} className="backtest-workspace" initial={300} min={250} max={460}>
      <form className="backtest-rail" onSubmit={run}>
        <div className="rail-title"><Icon name="chart" /><strong>回测设置</strong><span>{market === "cn" ? "A Share" : "US"}</span></div>
        <div className="backtest-fields">
        {market === "cn" ? <AShareSymbolInput value={form.symbol} onChange={(symbol) => setForm({ ...form, symbol })} /> : <Input form={form} setForm={setForm} name="symbol" label="股票代码" type="text" />}
        <Field label="回测周期"><select value={form.preset} onChange={(event) => changePreset(event.target.value)}><option value="3m">3个月</option><option value="6m">6个月</option><option value="1y">1年</option><option value="3y">3年</option>{market === "cn" ? <option value="5y">5年</option> : null}<option value="custom">自定义</option></select></Field>
        <Field label="开始日期"><input type="date" value={form.start} onChange={(event) => setForm({ ...form, start: event.target.value, preset: "custom" })} /></Field>
        <Field label="结束日期"><input type="date" value={form.end} onChange={(event) => setForm({ ...form, end: event.target.value, preset: "custom" })} /></Field>
        <Input form={form} setForm={setForm} name="initial_cash" label="初始资金" />
        <button className="primary-action" type="submit"><Icon name="play" />运行回测</button>
        </div>
        {market === "cn" ? <>
          <FilterSection title="交易成本与执行限制" note="A股"><AShareExecutionFields form={form} setForm={setForm} /></FilterSection>
          <FilterSection title="止损与弱趋势卖出" note="风控"><AShareExitFields form={form} setForm={setForm} /></FilterSection>
          <FilterSection title="可选买入条件" note="默认勾选趋势条件"><EntryConditionFields form={form} setForm={setForm} /></FilterSection>
        </> : <FilterSection title="策略与交易参数" note="高级"><StrategyFields market={market} form={form} setForm={setForm} /></FilterSection>}
      </form>
      <section className="backtest-canvas">{src ? <LazyBacktestReport url={src} symbol={form.symbol} market={market} onClose={() => setSrc("")} /> : <WorkspaceEmpty title="回测结果" note="当前尚未运行回测" />}</section>
    </ResizableWorkspace>
  </>;
}

export function BatchBacktest({ market, defaults }) {
  const dates = backtestDates(defaults?.end);
  const [form, setForm] = usePersistentState("backtest.us.batch.form", () => ({ ...defaults, symbols: "MU,RKLB,NVDA", preset: "1y", start: dates.start, end: dates.end, initial_cash: 100000, position_cash: 10000 }));
  const [src, setSrc] = usePersistentState("backtest.us.batch.result", "");
  if (market === "cn") return <><PageToolbar title="A股批量回测" subtitle="A股批量组合回测尚未启用" /><WorkspaceEmpty title="A股批量回测暂未启用" note="当前可使用 A股单票回测" /></>;
  function run(event) { event.preventDefault(); setSrc(`/batch/run/frame?${toQuery({ ...form, _report_only: 1 })}`); }
  return <><PageToolbar title="美股批量回测" subtitle="按总资金和单票投入金额观察组合收益" /><ResizableWorkspace storageKey="backtest.us.batch.rail" className="backtest-workspace" initial={300} min={250} max={460}><form className="backtest-rail" onSubmit={run}><div className="rail-title"><Icon name="batch" /><strong>组合设置</strong><span>US</span></div><div className="backtest-fields"><Field label="股票列表"><textarea rows="3" value={form.symbols} onChange={(event) => setForm({ ...form, symbols: event.target.value })} /></Field><Field label="回测周期"><select value={form.preset} onChange={(event) => setForm({ ...form, preset: event.target.value })}><option value="3m">3个月</option><option value="6m">6个月</option><option value="1y">1年</option><option value="3y">3年</option><option value="custom">自定义</option></select></Field><Input form={form} setForm={setForm} name="initial_cash" label="总资金" /><Input form={form} setForm={setForm} name="position_cash" label="单票资金" /><button className="primary-action" type="submit"><Icon name="play" />运行批量回测</button></div><FilterSection title="策略参数" note="高级"><StrategyFields market="us" form={form} setForm={setForm} /></FilterSection></form><section className="backtest-canvas">{src ? <LazyBatchReport url={src} onClose={() => setSrc("")} /> : <WorkspaceEmpty title="批量回测结果" note="当前尚未运行批量回测" />}</section></ResizableWorkspace></>;
}
