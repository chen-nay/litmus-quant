/**
 * 表单 ↔ 查询条件：确认卡上点「打开表单」改现有条件时用（2026-09-18 定：只改现有的，不从空白开始填）。
 *
 * 表单栏目的名字和查询条件的路径一致（["output", "sort", "by"]），接口报的问题（"output.sort.by"）直接对到输入框。
 * 接口只收明确的类型（ARCHITECTURE §4.1）：日期写成 YYYY-MM-DD，数字要是数值——
 * 标签输入框、文本框给的是文字，在这里转好再发。
 */

import dayjs, { type Dayjs } from "dayjs";

import type { EventInfo, Issue, Scope, Spec, Target } from "./types";


export type FieldName = (string | number)[];

/** 表：看谁（算的范围）/ 看哪天 / 看哪些数 / 怎么出 */
export interface TableValues {
  scope: {
    target: Target;
    base: Scope["base"];
    industry?: string | null;
    board?: string | null; // 概念板块代码
    exclude?: string[];
  };
  when: { as_of: Dayjs };
  metrics: { name?: string; expr?: string }[];
  output: {
    filter?: { expr?: string; label?: string };
    sort?: { by?: string; order?: "asc" | "desc" };
    limit: number | string;
  };
}

/** 统计：看谁（点名一只）/ 什么事件 / 看哪段 / 怎么算 */
export interface StudyValues {
  subject: { codes?: string };
  when: { range: [Dayjs, Dayjs] };
  output: {
    event: { preset_id: string; params?: Record<string, number | string> };
    horizons: (number | string)[];
    benchmark: string;
    cost_bps: number | string;
  };
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

export const DEFAULT_EXCLUDE = ["ST", "suspended", "new_listing_60d"];

export function buildTable(values: TableValues): Spec {
  const { scope, output } = values;
  const stock = scope.target === "stock";
  const filterExpr = text(output.filter?.expr);
  const sortBy = text(output.sort?.by);
  return {
    scope: {
      target: scope.target,
      base: stock ? scope.base : "all_a",
      industry: stock ? scope.industry || null : null,
      board: stock && scope.board ? { type: "concept", code: scope.board } : null,
      exclude: stock ? (scope.exclude ?? []) : DEFAULT_EXCLUDE,
    },
    subject: { kind: "pool", mentions: [], codes: [] },
    when: { as_of: day(values.when.as_of), range: null },
    metrics: values.metrics
      .map((metric) => ({ name: text(metric.name), expr: text(metric.expr) }))
      .filter((metric) => metric.name || metric.expr),
    output: {
      kind: "table",
      filter: filterExpr ? { expr: filterExpr, label: text(output.filter?.label) } : null,
      sort: sortBy ? { by: sortBy, order: output.sort?.order ?? "desc" } : null,
      limit: toNumber(output.limit),
    },
  };
}

export function tableValues(spec: Spec): TableValues {
  const output = spec.output.kind === "table" ? spec.output : null;
  return {
    scope: {
      target: spec.scope.target,
      base: spec.scope.base,
      industry: spec.scope.industry ?? undefined,
      board: spec.scope.board?.code ?? undefined,
      exclude: spec.scope.exclude,
    },
    when: { as_of: dayjs(spec.when.as_of ?? undefined) },
    metrics: spec.metrics.map((metric) => ({ ...metric })),
    output: {
      filter: { expr: output?.filter?.expr ?? "", label: output?.filter?.label ?? "" },
      sort: { by: output?.sort?.by ?? "", order: output?.sort?.order ?? "desc" },
      limit: output?.limit ?? 50,
    },
  };
}

export function buildStudy(values: StudyValues, event: EventInfo | undefined, scope: Scope): Spec {
  const { output } = values;
  const given = output.event.params ?? {};
  const params = Object.fromEntries(
    (event?.params ?? []).map((param) => [param.name, toNumber(given[param.name] ?? param.default)]),
  );
  const code = text(values.subject.codes).toUpperCase();
  return {
    scope,
    subject: { kind: "codes", mentions: [], codes: code ? [code] : [] },
    when: { as_of: null, range: { from: day(values.when.range[0]), to: day(values.when.range[1]) } },
    metrics: [],
    output: {
      kind: "event_study",
      event: { preset_id: output.event.preset_id, params },
      horizons: [...new Set(output.horizons.map(toNumber))].sort((a, b) => a - b),
      benchmark: output.benchmark,
      cost_bps: toNumber(output.cost_bps),
    },
  };
}

export function studyValues(spec: Spec): StudyValues {
  const output = spec.output.kind === "event_study" ? spec.output : null;
  const range = spec.when.range;
  return {
    subject: { codes: spec.subject.codes[0] ?? "" },
    when: { range: [dayjs(range?.from), dayjs(range?.to)] },
    output: {
      event: { preset_id: output?.event.preset_id ?? "breakout_ma", params: output?.event.params ?? {} },
      // 标签选择框的值要是文字（数字 antd 会报警告），提交时 buildStudy 再转回数值
      horizons: (output?.horizons ?? [5, 20, 60]).map(String),
      benchmark: output?.benchmark ?? "universe_equal_weight",
      cost_bps: output?.cost_bps ?? 30,
    },
  };
}

/**
 * 确认卡上点「修改」再提交：表单把每一栏都填上带回来，接口只看请求里缺了哪些栏目来标默认值，「默认值」标记会全丢。
 * 原来就是默认值、这次没改的栏目去掉不发，接口补上的还是同一个值，照样标出来。没有默认值清单（手填、提问草稿）原样发。
 */
export function dropUntouchedDefaults<S extends object>(
  spec: S,
  initial: Pick<Spec, "defaults_used"> | undefined,
): S {
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

/**
 * 接口给的路径（"output.event.params.ma"、"when.range.from"、"output.horizons.1"）对到最近的输入框：取最长的前缀。
 * 指标的路径是「metrics.指标名」，换成它在列表里的位置：["metrics", 0, "expr"]
 */
export function fieldForIssue(
  path: string | null,
  fields: FieldName[],
  metricNames: string[] = [],
): FieldName | null {
  if (!path) return null;
  if (path.startsWith("metrics.")) {
    const index = metricNames.indexOf(path.slice("metrics.".length));
    return index >= 0 ? ["metrics", index, "expr"] : null;
  }
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
  metricNames: string[] = [],
): { fields: { name: FieldName; errors: string[] }[]; other: string[] } {
  const byField = new Map<string, { name: FieldName; errors: string[] }>();
  const other: string[] = [];
  for (const issue of issues) {
    const name = fieldForIssue(issue.path, fields, metricNames);
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
