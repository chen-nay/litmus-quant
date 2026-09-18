/**
 * 数字怎么显示。结果里混着三种单位，集中在这一处并有单元测试：
 * - 字段单位是 % 的（涨跌幅 pct_chg、换手率等）是百分数：1.41 表示 1.41%
 * - 个股回看的涨跌、同期对照、跑赢比例，按 Pct(...) 排序的排序值，都是小数：-0.0078 表示 -0.78%
 * - 成本是基点：30 表示 0.3%
 * 颜色按 A 股习惯：红涨绿跌。
 */

import type { BoardType, Cell, DataDate, Delay, FieldInfo, Sort, SyncState } from "./types";

export const UP_COLOR = "#d9363e";
export const DOWN_COLOR = "#2f9e44";
export const EMPTY = "—";

export interface FieldMeta {
  name: string; // 不带 $
  label: string;
  unit: string;
}

/** 结果是小数的那一列的单位：按 Pct(...) 排序的排序值 */
export const FRACTION_UNIT = "小数";

const MONEY_FIELDS = new Set(["amount", "market_cap", "circ_mv"]);
const SIGNED_FIELDS = new Set(["pct_chg", "revenue_yoy", "profit_yoy"]);
const BOARD_POINT_FIELDS = new Set(["open", "high", "low", "close"]);

function isNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/** toFixed，但不出现 -0.00 */
function fixed(value: number, digits: number): string {
  const text = value.toFixed(digits);
  return Number(text) === 0 ? (0).toFixed(digits) : text;
}

export function formatNumber(value: number | null | undefined, digits = 2): string {
  return isNumber(value) ? fixed(value, digits) : EMPTY;
}

/** 百分数：1.41 → "1.41%"；sign 时正数带 + */
export function formatPercentValue(
  value: number | null | undefined,
  digits = 2,
  sign = false,
): string {
  if (!isNumber(value)) return EMPTY;
  const text = fixed(value, digits);
  return sign && Number(text) > 0 ? `+${text}%` : `${text}%`;
}

/** 小数：-0.0078 → "-0.78%" */
export function formatFraction(value: number | null | undefined, digits = 2, sign = true): string {
  return isNumber(value) ? formatPercentValue(value * 100, digits, sign) : EMPTY;
}

/** 基点：30 → "0.30%" */
export function formatBps(value: number | null | undefined): string {
  return isNumber(value) ? `${fixed(value / 100, 2)}%` : EMPTY;
}

/** 元：1 亿以上按「亿」，1 万以上按「万」 */
export function formatYuan(value: number | null | undefined): string {
  if (!isNumber(value)) return EMPTY;
  const size = Math.abs(value);
  if (size >= 1e8) return `${fixed(value / 1e8, 2)}亿`;
  if (size >= 1e4) return `${fixed(value / 1e4, 2)}万`;
  return fixed(value, 2);
}

export function trendColor(value: number | null | undefined): string | undefined {
  if (!isNumber(value) || value === 0) return undefined;
  return value > 0 ? UP_COLOR : DOWN_COLOR;
}

/** 结果表里的一格。name 不带 $，unit 来自 /api/fields */
export function formatCell(name: string, value: Cell | undefined, unit: string): string {
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "string") return value;
  if (!isNumber(value)) return EMPTY;
  if (MONEY_FIELDS.has(name)) return formatYuan(value);
  if (unit === FRACTION_UNIT) return formatFraction(value);
  if (unit === "%") return formatPercentValue(value, 2, SIGNED_FIELDS.has(name));
  if (unit === "个" || unit === "股" || unit === "天") return fixed(value, 0);
  return fixed(value, 2);
}

/** 涨跌类的字段、按 Pct(...) 排序的排序值红涨绿跌，其余不上色 */
export function cellColor(name: string, value: Cell | undefined, unit = ""): string | undefined {
  const signed = SIGNED_FIELDS.has(name) || unit === FRACTION_UNIT;
  return signed && typeof value === "number" ? trendColor(value) : undefined;
}

/** 列名。金额、百分数的格子里已经带了单位，列名不再写；板块的价格是点位 */
export function columnTitle(field: FieldMeta, board: boolean): string {
  if (!field.unit || field.unit === "%" || field.unit === "布尔" || MONEY_FIELDS.has(field.name)) {
    return field.label;
  }
  const unit = board && BOARD_POINT_FIELDS.has(field.name) ? "点" : field.unit;
  // 「收盘价（不复权）」不写成「收盘价（不复权）（元）」
  return field.label.endsWith("）")
    ? `${field.label.slice(0, -1)}，${unit}）`
    : `${field.label}（${unit}）`;
}

export function fieldMap(fields: FieldInfo[] | undefined): Map<string, FieldMeta> {
  return new Map(
    (fields ?? []).map((field) => {
      const name = field.name.replace(/^\$/, "");
      return [name, { name, label: field.label, unit: field.unit }];
    }),
  );
}

/** 排序值那一列：有名称用名称；排序依据就是一个字段时用字段名；没指定排序时是成交额 */
export function sortColumn(
  sort: Pick<Sort, "by" | "label"> | null,
  fields: Map<string, FieldMeta>,
): FieldMeta {
  const by = sort ? sort.by.trim() : "$amount";
  const match = /^\$([a-z_]+)$/.exec(by);
  const field = match ? fields.get(match[1]) : undefined;
  return {
    name: field?.name ?? "",
    label: sort?.label || field?.label || "排序值",
    unit: field?.unit ?? (isPctCall(by) ? FRACTION_UNIT : ""),
  };
}

/** 排序依据整个就是一次 Pct(...) 或 PctSince(...)：算出来是小数。Pct(...) / Std(...) 这种不算 */
function isPctCall(by: string): boolean {
  const open = by.indexOf("(");
  if (!["Pct", "PctSince"].includes(by.slice(0, open))) return false;
  let depth = 0;
  for (let i = open; i < by.length; i += 1) {
    if (by[i] === "(") depth += 1;
    if (by[i] === ")") {
      depth -= 1;
      if (depth === 0) return i === by.length - 1;
    }
  }
  return false;
}

/** 排序依据就是结果里已有的一列（比如按涨跌幅排）时，不再重复显示排序值那一列 */
export function visibleColumns(columns: string[], sort: FieldMeta): string[] {
  return sort.name && columns.includes(sort.name)
    ? columns.filter((name) => name !== "sort_value")
    : columns;
}

export function formatDelay(delay: Delay | null | undefined, action: "买入" | "卖出"): string {
  return delay ? `${action}顺延 ${delay.days} 天（${delay.reason}）` : "";
}

/** 各类数据截至哪天，同一天的并在一起：「股票行情、指数行情 2026-09-14；概念板块行情 2026-09-11」 */
export function latestText(latest: DataDate[] | undefined): string {
  const byDay = new Map<string, string[]>();
  for (const item of latest ?? []) byDay.set(item.date, [...(byDay.get(item.date) ?? []), item.label]);
  return [...byDay].map(([day, labels]) => `${labels.join("、")} ${day}`).join("；");
}

/** 接口给的是服务端本地时间（带时区），取到秒：2026-09-14T21:59:43+08:00 → 2026-09-14 21:59:43 */
export function formatTime(value: string | null | undefined): string {
  return value ? value.slice(0, 19).replace("T", " ") : EMPTY;
}

/** 表格排序用：数值按大小，文字按拼音；空值排最后 */
export function compareCells(a: Cell | undefined, b: Cell | undefined): number {
  const missingA = a === null || a === undefined;
  const missingB = b === null || b === undefined;
  if (missingA || missingB) return missingA === missingB ? 0 : missingA ? 1 : -1;
  if (typeof a === "number" && typeof b === "number") return a - b;
  return String(a).localeCompare(String(b), "zh-CN");
}

// ── 中文名 ──────────────────────────────────────────────────────

export const BENCHMARK_LABELS: Record<string, string> = {
  universe_equal_weight: "买入日全A等权",
  "index:000300.SH": "沪深300",
  "index:000905.SH": "中证500",
};

export function benchmarkLabel(value: string): string {
  return BENCHMARK_LABELS[value] ?? value;
}

export const BASE_LABELS: Record<string, string> = {
  all_a: "沪深A股",
  hs300: "沪深300成分",
  zz500: "中证500成分",
};

export const BOARD_TYPE_LABELS: Record<BoardType, string> = {
  sw_industry: "申万一级行业",
  sw_industry_l2: "申万二级行业",
  concept: "通达信概念板块",
};

export function excludeLabel(value: string): string {
  if (value === "ST") return "ST / *ST";
  if (value === "suspended") return "停牌";
  const days = /^new_listing_(\d+)d$/.exec(value);
  return days ? `上市不满 ${days[1]} 个交易日` : value;
}

const SYNC_STEP_LABELS: Record<string, string> = {
  meta: "基础数据",
  industry: "申万行业",
  concept: "概念板块",
  index: "指数",
  finance: "财务",
  daily: "股票日频",
};

export function stepLabel(step: string | null): string {
  return step ? (SYNC_STEP_LABELS[step] ?? step) : "准备中";
}

export const SYNC_STATE_LABELS: Record<SyncState, string> = {
  idle: "这次启动服务后没有同步过",
  running: "同步中",
  stopping: "正在停止",
  stopped: "已停止",
  failed: "失败",
  done: "完成",
};
