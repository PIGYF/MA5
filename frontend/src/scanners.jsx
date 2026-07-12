import React, { useEffect, useMemo, useState } from "react";
import { getJson, isJobRunning, numberText, toQuery, usePersistentState } from "./lib";
import { filterCandidates } from "./resultFilters";
import { Checkbox, ChartFrame, Field, FilterSection, Icon, PageToolbar, Progress, ResizableWorkspace, WorkspaceEmpty } from "./ui";

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
  const [sort, setSort] = usePersistentState(`scanner.${market}.sort`, { key: "score", direction: "desc" });
  const [widths, setWidths] = usePersistentState(`scanner.${market}.columns`, [52, 82, 180, 70, 260, 78, 72, 100, 150, 76]);
  const sortedRows = useMemo(() => [...rows].sort((left, right) => {
    const read = (row) => sort.key === "score" ? Number(market === "cn" ? row.volume_score : row.technical_score) : sort.key === "volume" ? Number(row.volume_ratio) : String(row[sort.key] || row.symbol || "");
    const a = read(left); const b = read(right); const result = typeof a === "number" ? a - b : String(a).localeCompare(String(b));
    return sort.direction === "asc" ? result : -result;
  }), [market, rows, sort]);
  const sortBy = (key) => setSort((current) => ({ key, direction: current.key === key && current.direction === "desc" ? "asc" : "desc" }));
  const resizeColumn = (index, event) => {
    event.preventDefault(); event.stopPropagation();
    const startX = event.clientX; const startWidth = widths[index];
    const move = (moveEvent) => setWidths((current) => current.map((width, currentIndex) => currentIndex === index ? Math.max(56, startWidth + moveEvent.clientX - startX) : width));
    const stop = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", stop); };
    window.addEventListener("pointermove", move); window.addEventListener("pointerup", stop);
  };
  const headers = [{ label: "操作" }, { label: "代码", key: "symbol" }, { label: "公司", key: "company_name" }, { label: "B点", key: "signal_type" }, { label: "入选原因" }, { label: "技术分", key: "score" }, { label: "量比", key: "volume" }, { label: market === "cn" ? "20日额" : "市值", key: market === "cn" ? "avg_amount_20d" : "market_cap_billion" }, { label: "行业", key: "sector" }, { label: "入选", key: "selection_streak" }];
  return <div className="table-wrap"><table className="candidate-table"><colgroup>{widths.map((width, index) => <col key={index} style={{ width }} />)}</colgroup><thead><tr>{headers.map((header, index) => <th key={header.label} className={header.key ? "sortable" : ""} onClick={() => header.key && sortBy(header.key)}><span>{header.label}{sort.key === header.key ? (sort.direction === "asc" ? " ↑" : " ↓") : ""}</span><i onPointerDown={(event) => resizeColumn(index, event)} /></th>)}</tr></thead><tbody>
    {sortedRows.length ? sortedRows.map((row) => {
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
        <td>{row.is_new_candidate ? <span className="new-candidate">新</span> : `${Number(row.selection_streak || 1)}次`}</td>
      </tr>;
    }) : <tr><td colSpan="10" className="empty">当前筛选下暂无候选</td></tr>}
  </tbody></table></div>;
}

export function Scanner({ market, bootstrap, latest, setLatest, reloadWatchlist }) {
  const isMobile = window.matchMedia("(max-width: 820px)").matches;
  const [form, setForm] = usePersistentState(`scanner.${market}.form`, () => ({ ...(bootstrap?.defaults || {}) }));
  const [job, setJob] = useState(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [selectedSymbol, setSelectedSymbol] = usePersistentState(`scanner.${market}.selected`, "");
  const [filtersOpen, setFiltersOpen] = usePersistentState(`scanner.${market}.filters.${isMobile ? "mobile" : "desktop"}`, () => !isMobile);
  const [tableHeight, setTableHeight] = usePersistentState(`scanner.${market}.tableHeight`, 220);
  const [resultFilter, setResultFilter] = usePersistentState(`scanner.${market}.resultFilter`, { query: "", signal: "all", rating: "all", minScore: "", onlyNew: false, consecutive: false });
  const [starting, setStarting] = useState(false);
  const prefix = `/api/${market}`;
  const rows = latest?.candidates || [];
  const visibleRows = useMemo(() => filterCandidates(rows, market, resultFilter), [market, resultFilter, rows]);
  const selectedRow = visibleRows.find((row) => row.symbol === selectedSymbol) || null;
  const running = isJobRunning(job);
  const jobId = job?.job_id;

  useEffect(() => setForm((current) => ({ ...(bootstrap?.defaults || {}), ...current })), [bootstrap, setForm]);

  useEffect(() => {
    if (!selectedRow && visibleRows.length) setSelectedSymbol(visibleRows[0].symbol);
    if (!visibleRows.length && selectedSymbol) setSelectedSymbol("");
  }, [selectedRow, selectedSymbol, setSelectedSymbol, visibleRows]);

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
    setError(""); setNotice(""); setSelectedSymbol("");
    setStarting(true);
    try { setJob(await getJson(`${prefix}/scan/start?${toQuery(form)}`)); }
    catch (exception) { setError(exception.message); }
    finally { setStarting(false); }
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
  const startTableResize = (event) => {
    if (window.matchMedia("(max-width: 820px)").matches) return;
    event.preventDefault(); const startY = event.clientY; const startHeight = Number(tableHeight) || 220;
    const move = (moveEvent) => setTableHeight(Math.max(132, Math.min(520, startHeight + moveEvent.clientY - startY)));
    const stop = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", stop); };
    window.addEventListener("pointermove", move); window.addEventListener("pointerup", stop);
  };
  return <>
    <PageToolbar title={`${market === "cn" ? "A股" : "美股"}选股器`} subtitle={`盘后扫描最后一根已完成日 K · 信号日 ${signalDate}`} actions={<>
      {latest?.csv ? <a className="tool-button" href={latest.csv}><Icon name="download" />CSV</a> : null}
      <button className="tool-button mobile-filter-action" type="button" onClick={() => setFiltersOpen((open) => !open)} aria-expanded={filtersOpen}><Icon name="filter" />{filtersOpen ? "收起条件" : "选股条件"}</button>
      <button className="primary-action" type="button" disabled={running || starting} onClick={startScan}><Icon name="play" />{starting ? "正在启动" : "开始选股"}</button>
      {running && market === "us" ? <button className="tool-button" type="button" onClick={() => jobAction(job.status === "paused" ? "resume" : "pause")}><Icon name={job.status === "paused" ? "play" : "pause"} />{job.status === "paused" ? "继续" : "暂停"}</button> : null}
      {running ? <button className="danger-action" type="button" onClick={() => jobAction("stop")}><Icon name="stop" />终止</button> : null}
    </>} />
    {error ? <div className="message error">{error}</div> : null}
    {notice ? <div className="message success">{notice}</div> : null}
    <ResizableWorkspace storageKey={`scanner.${market}.rail`} className={`scanner-workspace ${filtersOpen ? "" : "filters-collapsed"}`} initial={300} min={240} max={440}>
      <aside className="filter-rail"><div className="rail-title"><Icon name="filter" /><strong>选股条件</strong><span>{market === "cn" ? "A Share" : "US"}</span><button className="rail-toggle" type="button" title={filtersOpen ? "收起条件" : "展开条件"} onClick={() => setFiltersOpen((open) => !open)}><Icon name={filtersOpen ? "collapse" : "expand"} /></button></div><div className="filter-content">{market === "cn" ? <CnFilters form={form} setForm={setForm} boards={bootstrap?.boards || []} /> : <UsFilters form={form} setForm={setForm} />}</div></aside>
      <section className="result-workspace" style={{ "--table-height": `${tableHeight}px` }}>
        {job ? <Progress job={job} /> : <div className="result-summary"><strong>当前结果</strong><span>{signalDate}</span><span>{latest?.source || "缓存"}</span><span>数据至 {latest?.cache?.latest || signalDate}</span><span>缓存 {latest?.cache?.cached_symbols ?? rows.length}/{rows.length}</span><b>{rows.length} 只</b></div>}
        <div className="result-filterbar">
          <input aria-label="筛选候选" placeholder="代码 / 公司 / 行业" value={resultFilter.query} onChange={(event) => setResultFilter({ ...resultFilter, query: event.target.value })} />
          <select aria-label="B点类型" value={resultFilter.signal} onChange={(event) => setResultFilter({ ...resultFilter, signal: event.target.value })}><option value="all">全部B点</option><option value="B1">B1</option><option value="B2">B2</option></select>
          <select aria-label="评级" value={resultFilter.rating} onChange={(event) => setResultFilter({ ...resultFilter, rating: event.target.value })}><option value="all">全部评级</option><option value="Strong">Strong</option><option value="Medium">Medium</option></select>
          <input aria-label="最低技术分" type="number" placeholder="最低分" value={resultFilter.minScore} onChange={(event) => setResultFilter({ ...resultFilter, minScore: event.target.value })} />
          <Checkbox label="仅新增" checked={resultFilter.onlyNew} onChange={(checked) => setResultFilter({ ...resultFilter, onlyNew: checked })} />
          <Checkbox label="连续入选" checked={resultFilter.consecutive} onChange={(checked) => setResultFilter({ ...resultFilter, consecutive: checked })} />
          <button className="icon-button" type="button" title="清除筛选" aria-label="清除筛选" onClick={() => setResultFilter({ query: "", signal: "all", rating: "all", minScore: "", onlyNew: false, consecutive: false })}><Icon name="close" /></button>
          <span>{visibleRows.length}/{rows.length}</span>
        </div>
        <CandidateTable market={market} rows={visibleRows} selected={selectedRow?.symbol} onSelect={(row) => setSelectedSymbol(row.symbol)} onAdd={add} />
        <div className="resize-handle resize-handle-y" role="separator" aria-orientation="horizontal" tabIndex="0" onPointerDown={startTableResize} onKeyDown={(event) => {
          if (!["ArrowUp", "ArrowDown"].includes(event.key)) return;
          event.preventDefault(); setTableHeight(Math.max(132, Math.min(520, Number(tableHeight) + (event.key === "ArrowDown" ? 16 : -16))));
        }} />
        {chartUrl ? <ChartFrame title={`${selectedRow.symbol} 策略图表`} src={chartUrl} className="scanner-chart" onClose={() => setSelectedSymbol("")} /> : <WorkspaceEmpty title="暂无候选图表" note="当前信号日没有可显示的候选股票" />}
      </section>
    </ResizableWorkspace>
  </>;
}
