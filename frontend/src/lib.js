import { useEffect, useState } from "react";

const STORAGE_PREFIX = "ma5.ui.v1.";

export function usePersistentState(key, initialValue) {
  const storageKey = `${STORAGE_PREFIX}${key}`;
  const [value, setValue] = useState(() => {
    try {
      const saved = window.localStorage.getItem(storageKey);
      return saved === null ? (typeof initialValue === "function" ? initialValue() : initialValue) : JSON.parse(saved);
    } catch {
      return typeof initialValue === "function" ? initialValue() : initialValue;
    }
  });
  useEffect(() => {
    try { window.localStorage.setItem(storageKey, JSON.stringify(value)); } catch { /* storage may be unavailable */ }
  }, [storageKey, value]);
  return [value, setValue];
}

export async function getJson(url) {
  const response = await fetch(url);
  const contentType = response.headers.get("content-type") || "";
  if (!contentType.includes("application/json")) throw new Error(`服务返回异常（${response.status}），请稍后重试`);
  const payload = await response.json();
  if (!response.ok || payload.ok === false || payload.status === "error") {
    throw new Error(payload.error || `请求失败：${response.status}`);
  }
  return payload;
}

export function toQuery(params = {}) {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value === undefined || value === null || value === "") return;
    if (Array.isArray(value)) {
      value.forEach((item) => query.append(key, String(item)));
      return;
    }
    query.set(key, typeof value === "boolean" ? (value ? "1" : "0") : String(value));
  });
  return query.toString();
}

export function routeFromLocation() {
  const parts = window.location.pathname.replace(/^\/app\/?/, "").split("/").filter(Boolean);
  if (!parts.length) return { market: "us", page: "home" };
  if (parts[0] === "cn") return { market: "cn", page: parts[1] || "scan" };
  return { market: "us", page: parts[0] || "home" };
}

export function routePath(market, page) {
  if (page === "home") return "/app/";
  return market === "cn" ? `/app/cn/${page}` : `/app/${page}`;
}

export function isJobRunning(job) {
  return Boolean(job && !["done", "stopped", "error", "idle"].includes(job.status));
}

export function numberText(value, digits = 2) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(digits) : "-";
}

export function isoDateFrom(date) {
  return date.toISOString().slice(0, 10);
}

export function backtestDates(endValue) {
  const end = endValue || isoDateFrom(new Date());
  const startDate = new Date(`${end}T00:00:00`);
  startDate.setFullYear(startDate.getFullYear() - 1);
  return { start: isoDateFrom(startDate), end };
}
