/**
 * 数字怎么显示。结果里混着几种单位，集中在这一处并有单元测试：
 * - 单位是 % 的字段（涨跌幅 pct_chg、换手率等）是百分数：1.41 表示 1.41%
 * - 单位是「小数百分比」的（Pct、PctSince 算出来的，偏离、回撤）是涨跌，小数：-0.0078 表示 -0.78%
 * - 单位是「分位」的（Rank、TsRank 算出来的）也是小数，但不带正负号、不分红绿
 * - 成本是基点：30 表示 0.3%
 * 单位由后端随结果给出（结果里每一列的 unit、field），和卡上的写法一致（litmus/spec/card.py）。
 * 颜色按 A 股习惯：红涨绿跌。
 */

import type { BoardType, Cell, Column, DataDate, Delay, Kind, SyncState, Target } from "./types";

export const UP_COLOR = "#d9363e";
export const DOWN_COLOR = "#2f9e44";
export const EMPTY = "—";

/** 涨跌：小数，按百分比显示、带正负号（和后端 expr.RATIO 同一个标记） */
export const RATIO_UNIT = "小数百分比";
/** 分位：0~1 的小数，按百分比显示，不带正负号、不分红绿（后端 expr.PERCENTILE） */
export const PERCENTILE_UNIT = "分位";

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

/** 结果表里的一格：单位和字段来自结果里的这一列 */
export function formatCell(value: Cell | undefined, column: Pick<Column, "unit" | "field">): string {
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "string") return value;
  if (!isNumber(value)) return EMPTY;
  if (column.field && MONEY_FIELDS.has(column.field)) return formatYuan(value);
  if (column.unit === RATIO_UNIT) return formatFraction(value);
  if (column.unit === PERCENTILE_UNIT) return formatFraction(value, 2, false);
  if (column.unit === "%") return formatPercentValue(value, 2, SIGNED_FIELDS.has(column.field ?? ""));
  if (column.unit === "个" || column.unit === "股" || column.unit === "天") return fixed(value, 0);
  return fixed(value, 2);
}

/** 涨跌类红涨绿跌，其余不上色 */
export function cellColor(value: Cell | undefined, column: Pick<Column, "unit" | "field">): string | undefined {
  const signed = SIGNED_FIELDS.has(column.field ?? "") || column.unit === RATIO_UNIT;
  return signed && typeof value === "number" ? trendColor(value) : undefined;
}

/** 列名。金额、百分数的格子里已经带了单位，列名不再写；板块的价格是点位 */
export function columnTitle(column: Column, board: boolean): string {
  const unit =
    board && BOARD_POINT_FIELDS.has(column.field ?? "")
      ? "点"
      : column.field && MONEY_FIELDS.has(column.field)
        ? ""
        : ["倍", "元", "个", "股", "天"].includes(column.unit)
          ? column.unit
          : "";
  if (!unit) return column.name;
  // 「收盘价（不复权）」不写成「收盘价（不复权）（元）」
  return column.name.endsWith("）") ? `${column.name.slice(0, -1)}，${unit}）` : `${column.name}（${unit}）`;
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
  universe_equal_weight: "买入日算的范围等权平均",
  "index:000300.SH": "沪深300",
  "index:000905.SH": "中证500",
};

/** 结果、确认卡的标题：股票表 / 板块表 · 申万一级行业 / 卡 / 统计 */
export function kindTitle(kind: Kind, target: Target = "stock"): string {
  if (kind === "table") return target === "stock" ? "股票表" : `板块表 · ${BOARD_TYPE_LABELS[target]}`;
  if (kind === "card") return "卡";
  return "统计";
}

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
