import React, { useEffect, useMemo, useState } from "react";
import { getJson, isJobRunning, numberText, toQuery } from "./lib";
import { Checkbox, ChartFrame, Field, FilterSection, Icon, PageToolbar, Progress, WorkspaceEmpty } from "./ui";

function value(form, key) {
  return form?.[key] ?? "";
}

function NumberField({ form, setForm, name, label, step = "any" }) {
  return <Field label={label}><input type="number" step={step} value={value(form, name)} onChange={(event) => setForm({ ...form, [name]: event.target.value })} /></Field>;
}

function SelectField({ form, setForm, name, label, options }) {
  return <Field label={label}><select value={value(form, name)} onChange={(event) => setForm({ ...form, [name]: event.target.value })}>{options.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select></Field>;
}

function Toggle({ form, setForm, name, label }) {
  return <Checkbox label={label} checked={form[name]} onChange={(checked) => setForm({ ...form, [name]: checked })} />;
}

function UsFilters({ form, setForm }) {
  return <>
    <FilterSection title="扫描范围" note="股票池与规模">
      <SelectField form={form} setForm={setForm} name="universe_source" label="股票池来源" options={[{ value: "auto", label: "按市值自动筛选" }, { value: "manual", label: "手动股票池" }]} />
      <SelectField form={form} setForm={setForm} name="asset_type" label="资产类型" options={[{ value: "stocks", label: "只扫 Stocks" }, { value: "etf", label: "只扫 ETF" }, { value: "all", label: "Stocks + ETF" }]} />
      <NumberField form={form} setForm={setForm} name="min_market_cap_billion" label="最低市值（亿美元）" />
      <NumberField form={form} setForm={setForm} name="max_market_cap_billion" label="最高市值（亿美元）" />
      <NumberField form={form} setForm={setForm} name="min_screener_volume" label="最低当日成交量" />
      <NumberField form={form} setForm={setForm} name="max_symbols" label="最多扫描" step="1" />
      <NumberField form={form} setForm={setForm} name="max_workers" label="并发数" step="1" />
      {form.universe_source === "manual" ? <Field label="手动股票池" wide><textarea rows="3" value={value(form, "symbols")} placeholder="ASTS,NVDA,TSLA" onChange={(event) => setForm({ ...form, symbols: event.target.value })} /></Field> : null}
    </FilterSection>
    <FilterSection title="基础过滤" note="日期、价格与财报">
      <Field label="开始日期"><input type="date" value={value(form, "start")} onChange={(event) => setForm({ ...form, start: event.target.value })} /></Field>
      <Field label="结束日期"><input type="date" value={value(form, "end")} onChange={(event) => setForm({ ...form, end: event.target.value })} /></Field>
      <NumberField form={form} setForm={setForm} name="min_price" label="最低价格（美元）" />
      <NumberField form={form} setForm={setForm} name="min_avg_dollar_volume" label="20日最低成交额" />
      <SelectField form={form} setForm={setForm} name="earnings_filter" label="财报风险" options={[{ value: "show", label: "显示全部" }, { value: "hide_3d", label: "隐藏3天内财报" }, { value: "hide_7d", label: "隐藏7天内财报" }, { value: "hide_unknown", label: "隐藏未知财报" }]} />
      <div className="check-grid"><Toggle form={form} setForm={setForm} name="hide_weak" label="隐藏 Weak 候选" /></div>
    </FilterSection>
    <FilterSection title="信号参数" note="B1 / B2 与量能">
      <NumberField form={form} setForm={setForm} name="ma_length" label="均线周期" step="1" />
      <NumberField form={form} setForm={setForm} name="vol_length" label="均量周期" step="1" />
      <NumberField form={form} setForm={setForm} name="vol_high_days" label="连续放量天数" step="1" />
      <NumberField form={form} setForm={setForm} name="vol_high_multiplier" label="连续放量倍数" />
      <NumberField form={form} setForm={setForm} name="vol_multiplier" label="巨量倍数" />
      <NumberField form={form} setForm={setForm} name="massive_window" label="巨量观察窗口" step="1" />
      <NumberField form={form} setForm={setForm} name="massive_min_count" label="巨量最少次数" step="1" />
      <NumberField form={form} setForm={setForm} name="reentry_pct" label="B2回踩距离（%）" />
    </FilterSection>
    <FilterSection title="可选条件" note="默认关闭">
      <div className="check-grid">
        <Toggle form={form} setForm={setForm} name="require_ma5_rising" label="MA5 向上" />
        <Toggle form={form} setForm={setForm} name="require_5ma_gt_20ma" label="MA5 > MA20" />
        <Toggle form={form} setForm={setForm} name="b1_require_20ma_gt_50ma" label="20MA > 50MA" />
        <Toggle form={form} setForm={setForm} name="secondary_big_red_b1" label="大阴线 B1" />
        <Toggle form={form} setForm={setForm} name="secondary_above_ma5_3d" label="连续三天 > MA5" />
      </div>
    </FilterSection>
  </>;
}

function CnFilters({ form, setForm, boards }) {
  const selected = new Set(form.boards || []);
  function toggleBoard(board, checked) {
    const next = new Set(selected);
    if (checked) next.add(board); else next.delete(board);
    setForm({ ...form, boards: [...next] });
  }
  return <>
    <FilterSection title="扫描范围" note="板块、市值与流动性">
      <div className="field wide"><span>上市板块</span><div className="check-grid compact">{boards.map((board) => <Checkbox key={board.value} label={board.label} checked={selected.has(board.value)} onChange={(checked) => toggleBoard(board.value, checked)} />)}</div></div>
      <NumberField form={form} setForm={setForm} name="min_market_cap" label="最低市值（亿元）" />
      <NumberField form={form} setForm={setForm} name="max_symbols" label="最多扫描" step="1" />
      <NumberField form={form} setForm={setForm} name="max_workers" label="并发数" step="1" />
      <NumberField form={form} setForm={setForm} name="min_avg_amount_20d_100m" label="20日均成交额（亿元）" />
      <NumberField form={form} setForm={setForm} name="min_control_amount_20d_100m" label="低流动性提示（亿元）" />
    </FilterSection>
    <FilterSection title="信号参数" note="B1 / B2 与量能">
      <NumberField form={form} setForm={setForm} name="vol_high_days" label="连续放量天数" step="1" />
      <NumberField form={form} setForm={setForm} name="vol_high_multiplier" label="连续放量倍数" />
      <NumberField form={form} setForm={setForm} name="vol_multiplier" label="巨量倍数" />
      <NumberField form={form} setForm={setForm} name="massive_window" label="巨量观察窗口" step="1" />
      <NumberField form={form} setForm={setForm} name="massive_min_count" label="巨量最少次数" step="1" />
      <NumberField form={form} setForm={setForm} name="reentry_pct" label="B2回踩距离（%）" />
      <NumberField form={form} setForm={setForm} name="strong_volume_score" label="Strong 量能分" />
      <NumberField form={form} setForm={setForm} name="medium_volume_score" label="Medium 量能分" />
    </FilterSection>
    <FilterSection title="可选条件" note="默认关闭">
      <div className="check-grid">
        <Toggle form={form} setForm={setForm} name="require_ma5_rising" label="MA5 向上" />
        <Toggle form={form} setForm={setForm} name="require_5ma_gt_20ma" label="MA5 > MA20" />
        <Toggle form={form} setForm={setForm} name="b1_require_20ma_gt_50ma" label="20MA > 50MA" />
        <Toggle form={form} setForm={setForm} name="secondary_big_red_b1" label="大阴线 B1" />
        <Toggle form={form} setForm={setForm} name="secondary_above_ma5_3d" label="连续三天 > MA5" />
      </div>
    </FilterSection>
  </>;
}

function CandidateTable({ market, rows, selected, onSelect, onAdd }) {
  return <div className="table-wrap"><table><thead><tr><th>操作</th><th>代码</th><th>公司</th><th>B点</th><th>入选原因</th><th>技术分</th><th>量比</th><th>{market === "cn" ? "20日额" : "市值"}</th><th>行业</th></tr></thead><tbody>
    {rows.length ? rows.map((row) => {
      const symbol = row.symbol;
      const score = market === "cn" ? row.volume_score : row.technical_score;
      const rating = market === "cn" ? row.candidate_rating : row.technical_rating;
      const reasons = market === "cn"
        ? [row.signal_type, row.ma5_rising && "MA5向上", row.ma5_gt_20 && "MA5>MA20", row.ma20_gt_50 && "20MA>50MA"].filter(Boolean)
        : [row.signal_label, row.reason_summary || row.signal_reason].filter(Boolean);
      return <tr key={symbol} className={selected === symbol ? "selected" : ""} onClick={() => onSelect(row)}>
        <td><button className="icon-button" type="button" title="加入自选" onClick={(event) => { event.stopPropagation(); onAdd(row); }}><Icon name="plus" /></button></td>
        <td><button className="symbol-button" type="button">{symbol}</button></td>
        <td>{market === "cn" ? (row.name || "-") : (row.company_display_name || row.company_name || "-")}</td>
        <td>{market === "cn" ? (row.signal_type || "-") : (row.signal_label || "-")}</td>
        <td><div className="reason-tags">{reasons.slice(0, 4).map((reason) => <span key={String(reason)}>{String(reason)}</span>)}</div></td>
        <td><span className={`score score-${rating || "Medium"}`}>{Number(score || 0).toFixed(market === "cn" ? 1 : 0)}</span></td>
        <td>{numberText(row.volume_ratio)}x</td>
        <td>{market === "cn" ? `${numberText(Number(row.avg_amount_20d || 0) / 100000000)}亿` : (row.market_cap_billion || "-")}</td>
        <td>{market === "cn" ? (row.sector || "-") : (row.industry_zh || row.sector_zh || "-")}</td>
      </tr>;
    }) : <tr><td colSpan="9" className="empty">当前信号日暂无候选</td></tr>}
  </tbody></table></div>;
}

export function Scanner({ market, bootstrap, latest, setLatest, reloadWatchlist }) {
  const [form, setForm] = useState(() => ({ ...(bootstrap?.defaults || {}) }));
  const [job, setJob] = useState(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [selectedRow, setSelectedRow] = useState(null);
  const [filtersOpen, setFiltersOpen] = useState(true);
  const prefix = `/api/${market}`;
  const rows = latest?.candidates || [];
  const running = isJobRunning(job);
  const jobId = job?.job_id;

  useEffect(() => setForm({ ...(bootstrap?.defaults || {}) }), [bootstrap]);

  useEffect(() => {
    if (!selectedRow && rows.length) setSelectedRow(rows[0]);
  }, [rows, selectedRow]);

  useEffect(() => {
    let cancelled = false;
    getJson(`${prefix}/scan/active`).then((payload) => { if (!cancelled && payload.job_id) setJob(payload); }).catch(() => {});
    return () => { cancelled = true; };
  }, [prefix]);

  useEffect(() => {
    if (!running || !jobId) return undefined;
    const timer = window.setInterval(async () => {
      try {
        const payload = await getJson(`${prefix}/scan/status?${toQuery({ id: jobId, job_id: jobId })}`);
        setJob(payload);
        if (["done", "stopped"].includes(payload.status)) {
          const latestPayload = await getJson(`${prefix}/scan/latest`);
          setLatest(latestPayload.latest || null);
        }
      } catch (exception) {
        setError(exception.message);
      }
    }, 1000);
    return () => window.clearInterval(timer);
  }, [jobId, prefix, running, setLatest]);

  async function startScan() {
    setError(""); setNotice(""); setSelectedRow(null);
    try { setJob(await getJson(`${prefix}/scan/start?${toQuery(form)}`)); }
    catch (exception) { setError(exception.message); }
  }

  async function jobAction(action) {
    if (!jobId) return;
    try { setJob(await getJson(`${prefix}/scan/${action}?${toQuery({ id: jobId, job_id: jobId })}`)); }
    catch (exception) { setError(exception.message); }
  }

  async function add(row) {
    const params = market === "cn" ? { symbol: row.symbol, name: row.name, sector: row.sector, group: "候选" } : { symbol: row.symbol, group: "候选" };
    try {
      await getJson(`${prefix}/watchlist/add?${toQuery(params)}`);
      await reloadWatchlist(market);
      setNotice(`${row.symbol} 已加入自选池`);
    } catch (exception) { setError(exception.message); }
  }

  const chartUrl = useMemo(() => {
    if (!selectedRow) return "";
    return market === "cn"
      ? `/cn/candidate/chart?${toQuery({ ...form, symbol: selectedRow.symbol })}`
      : `/candidate/chart?${toQuery({ ...form, symbol: selectedRow.symbol, fast: 1 })}`;
  }, [form, market, selectedRow]);

  const signalDate = latest?.signal_date || "-";
  return <>
    <PageToolbar title={`${market === "cn" ? "A股" : "美股"}选股器`} subtitle={`盘后扫描最后一根已完成日 K · 信号日 ${signalDate}`} actions={<>
      {latest?.csv ? <a className="tool-button" href={latest.csv}><Icon name="download" />CSV</a> : null}
      <button className="primary-action" type="button" disabled={running} onClick={startScan}><Icon name="play" />开始选股</button>
      {running && market === "us" ? <button className="tool-button" type="button" onClick={() => jobAction(job.status === "paused" ? "resume" : "pause")}><Icon name={job.status === "paused" ? "play" : "pause"} />{job.status === "paused" ? "继续" : "暂停"}</button> : null}
      {running ? <button className="danger-action" type="button" onClick={() => jobAction("stop")}><Icon name="stop" />终止</button> : null}
    </>} />
    {error ? <div className="message error">{error}</div> : null}
    {notice ? <div className="message success">{notice}</div> : null}
    <section className={`scanner-workspace ${filtersOpen ? "" : "filters-collapsed"}`}>
      <aside className="filter-rail"><div className="rail-title"><Icon name="filter" /><strong>选股条件</strong><span>{market === "cn" ? "A Share" : "US"}</span><button className="rail-toggle" type="button" title={filtersOpen ? "收起条件" : "展开条件"} onClick={() => setFiltersOpen((open) => !open)}><Icon name={filtersOpen ? "collapse" : "expand"} /></button></div><div className="filter-content">{market === "cn" ? <CnFilters form={form} setForm={setForm} boards={bootstrap?.boards || []} /> : <UsFilters form={form} setForm={setForm} />}</div></aside>
      <section className="result-workspace">
        {job ? <Progress job={job} /> : <div className="result-summary"><strong>当前结果</strong><span>{signalDate}</span><b>{rows.length} 只</b></div>}
        <CandidateTable market={market} rows={rows} selected={selectedRow?.symbol} onSelect={setSelectedRow} onAdd={add} />
        {chartUrl ? <ChartFrame title={`${selectedRow.symbol} 策略图表`} src={chartUrl} className="scanner-chart" onClose={() => setSelectedRow(null)} /> : <WorkspaceEmpty title="暂无候选图表" note="当前信号日没有可显示的候选股票" />}
      </section>
    </section>
  </>;
}
