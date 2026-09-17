/** 结果页顶部的条件说明：查询条件 → 几行中文。第 7 步的确认卡说明文字由后端模板生成，这里只是原样列出。 */

import {
  BASE_LABELS,
  BOARD_TYPE_LABELS,
  benchmarkLabel,
  excludeLabel,
  formatBps,
} from "./format";
import type { Condition, Sort, Spec } from "./types";

export interface SummaryItem {
  key: string;
  label: string;
  children: string;
}

export function runTitle(spec: Spec): string {
  if (spec.shape === "stock_list") return "股票表";
  if (spec.shape === "board_list") return `板块表 · ${BOARD_TYPE_LABELS[spec.board_type]}`;
  return "个股回看";
}

function describeCondition(condition: Condition | null): string {
  if (!condition) return "不筛选";
  return condition.label ? `${condition.label}：${condition.expr}` : condition.expr;
}

function describeSort(sort: Sort | null): string {
  if (!sort) return "成交额从高到低（没有指定排序）";
  const order = sort.order === "desc" ? "从高到低" : "从低到高";
  return sort.label ? `${sort.label}（${sort.by}）${order}` : `${sort.by} ${order}`;
}

export function summarize(spec: Spec, boardNames: Map<string, string> = new Map()): SummaryItem[] {
  if (spec.shape === "stock_history") {
    const params = Object.entries(spec.event.params)
      .map(([name, value]) => `${name}=${value}`)
      .join("，");
    return [
      { key: "code", label: "股票", children: spec.target.code },
      {
        key: "event",
        label: "事件",
        children: `${spec.event.label || spec.event.preset_id}${params ? `（${params}）` : ""}`,
      },
      { key: "range", label: "回看区间", children: `${spec.time_range.from} ~ ${spec.time_range.to}` },
      { key: "horizons", label: "持有天数", children: `${spec.horizons.join("、")} 个交易日` },
      { key: "benchmark", label: "同期对照", children: benchmarkLabel(spec.benchmark) },
      { key: "cost", label: "交易成本", children: `${formatBps(spec.cost_bps)}（买卖双边合计）` },
    ];
  }
  const items: SummaryItem[] = [
    { key: "as_of", label: "日期", children: spec.as_of },
    { key: "filter", label: "筛选", children: describeCondition(spec.filter) },
    { key: "sort", label: "排序", children: describeSort(spec.sort) },
    { key: "limit", label: "取前", children: `${spec.limit} 名` },
  ];
  if (spec.shape === "stock_list") {
    const { universe } = spec;
    const pool = [
      BASE_LABELS[universe.base] ?? universe.base,
      universe.industry ? `申万行业「${universe.industry}」` : "",
      universe.board
        ? `概念板块「${boardNames.get(universe.board.code) ?? universe.board.code}」`
        : "",
    ].filter(Boolean);
    items.push(
      { key: "universe", label: "股票池", children: pool.join(" · ") },
      {
        key: "exclude",
        label: "剔除",
        children: universe.exclude.map(excludeLabel).join("、") || "不剔除",
      },
    );
  }
  return items;
}
