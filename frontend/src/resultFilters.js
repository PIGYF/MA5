export function candidateScore(market, row) {
  return Number(market === "cn" ? row.volume_score : row.technical_score);
}

export function filterCandidates(rows, market, filters) {
  const query = String(filters.query || "").trim().toLowerCase();
  return rows.filter((row) => {
    const score = candidateScore(market, row);
    const haystack = [row.symbol, row.name, row.company_display_name, row.company_name, row.sector, row.industry_zh, row.sector_zh].join(" ").toLowerCase();
    if (query && !haystack.includes(query)) return false;
    if (filters.signal !== "all" && !String(row.signal_type || row.signal_label || "").includes(filters.signal)) return false;
    if (filters.rating !== "all" && String(market === "cn" ? row.candidate_rating : row.technical_rating) !== filters.rating) return false;
    if (filters.minScore !== "" && score < Number(filters.minScore)) return false;
    if (filters.onlyNew && !row.is_new_candidate) return false;
    if (filters.consecutive && Number(row.selection_streak || 1) < 2) return false;
    if (filters.bigRedB1 && !row.big_red_b1) return false;
    if (filters.aboveMa5ThreeDays && !row.above_ma5_3d) return false;
    return true;
  });
}
