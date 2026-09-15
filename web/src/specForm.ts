/**
 * 表单 ↔ 查询条件：把表单里的值整理成接口要的 spec，把接口返回的问题标到对应的输入框。
 *
 * 接口只收明确的类型（ARCHITECTURE §4.1）：日期写成 YYYY-MM-DD，数字要是数值——
 * 标签输入框、文本框给的是文字，在这里转好再发。
 */

import type { Dayjs } from "dayjs";

import type {
  BoardListSpec,
  BoardType,
  Condition,
  EventInfo,
  Issue,
  Sort,
  StockHistorySpec,
  StockListSpec,
  Universe,
} from "./types";

export type FieldName = string[];

export interface ConditionValues {
  expr?: string;
  label?: string;
}

export interface SortValues {
  by?: string;
  order?: "asc" | "desc";
  label?: string;
}

export interface StockListValues {
  as_of: Dayjs;
  limit: number | string;
  filter?: ConditionValues;
  sort?: SortValues;
  universe: {
    base: Universe["base"];
    industry?: string | null;
    board?: string | null; // 概念板块代码
    exclude?: string[];
  };
}

export interface BoardListValues {
  board_type: BoardType;
  as_of: Dayjs;
  limit: number | string;
  filter?: ConditionValues;
  sort?: SortValues;
}

export interface HistoryValues {
  target: { code?: string };
  event: { preset_id: string; params?: Record<string, number | string> };
  time_range: [Dayjs, Dayjs];
  horizons: (number | string)[];
  benchmark: string;
  cost_bps: number | string;
}

export function day(value: Dayjs): string {
  return value.format("YYYY-MM-DD");
}

function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

/** 空的、写错的变成 NaN（发出去是 null），由接口报「要是数字」 */
export function toNumber(value: unknown): number {
  if (typeof value === "number") return value;
  const written = text(value);
  return written ? Number(written) : Number.NaN;
}

function condition(values: ConditionValues | undefined): Condition | null {
  const expr = text(values?.expr);
  return expr ? { expr, label: text(values?.label) } : null;
}

function sort(values: SortValues | undefined): Sort | null {
  const by = text(values?.by);
  return by ? { by, order: values?.order ?? "desc", label: text(values?.label) } : null;
}

export function buildStockList(values: StockListValues): StockListSpec {
  const { universe } = values;
  return {
    shape: "stock_list",
    as_of: day(values.as_of),
    filter: condition(values.filter),
    sort: sort(values.sort),
    limit: toNumber(values.limit),
    universe: {
      base: universe.base,
      industry: universe.industry || null,
      board: universe.board ? { type: "concept", code: universe.board } : null,
      exclude: universe.exclude ?? [],
    },
  };
}

export function buildBoardList(values: BoardListValues): BoardListSpec {
  return {
    shape: "board_list",
    board_type: values.board_type,
    as_of: day(values.as_of),
    filter: condition(values.filter),
    sort: sort(values.sort),
    limit: toNumber(values.limit),
  };
}

export function buildHistory(values: HistoryValues, event: EventInfo | undefined): StockHistorySpec {
  const given = values.event.params ?? {};
  const params = Object.fromEntries(
    (event?.params ?? []).map((param) => [param.name, toNumber(given[param.name] ?? param.default)]),
  );
  const horizons = [...new Set(values.horizons.map(toNumber))].sort((a, b) => a - b);
  return {
    shape: "stock_history",
    target: { code: text(values.target.code).toUpperCase() },
    event: { preset_id: values.event.preset_id, params },
    time_range: { from: day(values.time_range[0]), to: day(values.time_range[1]) },
    horizons,
    benchmark: values.benchmark,
    cost_bps: toNumber(values.cost_bps),
  };
}

/** 日期选择器：周末和本地数据范围之外的日子不能选（节假日选了，接口会说不是交易日） */
export function disabledDay(first: string | null | undefined, last: string | null | undefined) {
  return (current: Dayjs): boolean => {
    const weekday = current.day();
    if (weekday === 0 || weekday === 6) return true;
    const value = day(current);
    return (!!first && value < first) || (!!last && value > last);
  };
}

// ── 问题 → 输入框 ───────────────────────────────────────────────

/** 接口给的路径（"event.params.ma"、"time_range.from"、"horizons.1"）对到最近的输入框：取最长的前缀 */
export function fieldForIssue(path: string | null, fields: FieldName[]): FieldName | null {
  if (!path) return null;
  const parts = path.split(".");
  let best: FieldName | null = null;
  for (const field of fields) {
    const prefix = field.length <= parts.length && field.every((part, i) => part === parts[i]);
    if (prefix && (!best || field.length > best.length)) best = field;
  }
  return best;
}

export function issueText(issue: Issue): string {
  const where = issue.position === null ? "" : `第 ${issue.position + 1} 个字符：`;
  const allowed =
    issue.allowed && !issue.message.includes(issue.allowed) ? `（可选：${issue.allowed}）` : "";
  return `${where}${issue.message}${allowed}`;
}

/** 对得上输入框的挂到输入框下面，对不上的放到表单上方 */
export function splitIssues(
  issues: Issue[],
  fields: FieldName[],
): { fields: { name: FieldName; errors: string[] }[]; other: string[] } {
  const byField = new Map<string, { name: FieldName; errors: string[] }>();
  const other: string[] = [];
  for (const issue of issues) {
    const name = fieldForIssue(issue.path, fields);
    if (!name) {
      other.push(issue.path ? `${issue.path}：${issueText(issue)}` : issueText(issue));
      continue;
    }
    const key = name.join(".");
    const entry = byField.get(key) ?? { name, errors: [] };
    entry.errors.push(issueText(issue));
    byField.set(key, entry);
  }
  return { fields: [...byField.values()], other };
}
