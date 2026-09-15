/**
 * 表单 ↔ 查询条件：把表单里的值整理成接口要的 spec，把接口返回的问题标到对应的输入框。
 *
 * 接口只收明确的类型（ARCHITECTURE §4.1）：日期写成 YYYY-MM-DD，数字要是数值——
 * 标签输入框、文本框给的是文字，在这里转好再发。
 */

import dayjs, { type Dayjs } from "dayjs";

import type {
  BoardListSpec,
  BoardType,
  Condition,
  EventInfo,
  Issue,
  Sort,
  SpecMeta,
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

// ── 查询条件 → 表单（确认卡上点「修改」时预填；什么都不给就是表单的默认值）────

export const DEFAULT_EXCLUDE = ["ST", "suspended", "new_listing_60d"];

export function stockListValues(spec: Partial<StockListSpec>): Partial<StockListValues> {
  return {
    as_of: spec.as_of ? dayjs(spec.as_of) : undefined,
    limit: spec.limit ?? 50,
    filter: { expr: spec.filter?.expr ?? "", label: spec.filter?.label ?? "" },
    sort: { by: spec.sort?.by ?? "", order: spec.sort?.order ?? "desc", label: spec.sort?.label ?? "" },
    universe: {
      base: spec.universe?.base ?? "all_a",
      industry: spec.universe?.industry ?? undefined,
      board: spec.universe?.board?.code ?? undefined,
      exclude: spec.universe?.exclude ?? DEFAULT_EXCLUDE,
    },
  };
}

export function boardListValues(spec: Partial<BoardListSpec>): Partial<BoardListValues> {
  return {
    board_type: spec.board_type ?? "sw_industry",
    as_of: spec.as_of ? dayjs(spec.as_of) : undefined,
    limit: spec.limit ?? 50,
    filter: { expr: spec.filter?.expr ?? "", label: spec.filter?.label ?? "" },
    sort: { by: spec.sort?.by ?? "", order: spec.sort?.order ?? "desc", label: spec.sort?.label ?? "" },
  };
}

export function historyValues(spec: Partial<StockHistorySpec>): Partial<HistoryValues> {
  return {
    target: { code: spec.target?.code ?? "" },
    event: { preset_id: spec.event?.preset_id ?? "breakout_ma", params: spec.event?.params ?? {} },
    time_range: spec.time_range
      ? [dayjs(spec.time_range.from), dayjs(spec.time_range.to)]
      : undefined,
    // 标签选择框的值要是文字（数字 antd 会报警告），提交时 buildHistory 再转回数值
    horizons: (spec.horizons ?? [5, 20, 60]).map(String),
    benchmark: spec.benchmark ?? "universe_equal_weight",
    cost_bps: spec.cost_bps ?? 30,
  };
}

/**
 * 确认卡上点「修改」再提交：表单把每一栏都填上带回来，接口只看请求里缺了哪些栏目来标默认值，「默认值」标记会全丢。
 * 原来就是默认值、这次没改的栏目去掉不发，接口补上的还是同一个值，照样标出来。没有默认值清单（手填、提问草稿）原样发。
 */
export function dropUntouchedDefaults<S extends object>(spec: S, initial: SpecMeta | undefined): S {
  const result = structuredClone(spec) as Record<string, unknown>;
  for (const path of initial?.defaults_used ?? []) {
    const keys = path.split(".");
    if (JSON.stringify(valueAt(result, keys)) !== JSON.stringify(valueAt(initial, keys))) continue;
    const parent = valueAt(result, keys.slice(0, -1));
    if (parent && typeof parent === "object") delete (parent as Record<string, unknown>)[keys[keys.length - 1]];
  }
  return result as S;
}

function valueAt(value: unknown, keys: string[]): unknown {
  return keys.reduce<unknown>(
    (current, key) => (current && typeof current === "object" ? (current as Record<string, unknown>)[key] : undefined),
    value,
  );
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
