/**
 * 个股回看的 K 线图配置（ECharts）。价格是前复权（2026-09-15 定，ARCHITECTURE §6）：
 * 最新价就是真实价格，涨跌幅和结果表（后复权算的）完全一样；悬停时同时给当天的真实成交价。
 * 量柱用成交额：成交量随复权调整过，画出来会失真。
 */

import type { EChartsCoreOption } from "echarts/core";

import { DOWN_COLOR, UP_COLOR, formatNumber, formatPercentValue, formatYuan } from "./format";
import type { HistoryResult, KlineResponse, KlineRow } from "./types";

export const TRIGGER_COLOR = "#1677ff";

export interface Highlight {
  start: string; // 买入日
  end: string; // 卖出日
}

/** 图的范围：统计区间起点到最后一笔卖出日；还没走完的笔数没有卖出日，至少画到统计区间终点 */
export function chartWindow(result: HistoryResult): { from: string; to: string } {
  const exits = result.triggers
    .flatMap((trigger) => Object.values(trigger.exit_date))
    .filter((value): value is string => !!value);
  const to = exits.reduce((latest, value) => (value > latest ? value : latest), result.range[1]);
  return { from: result.range[0], to };
}

export function klineTooltip(row: KlineRow): string {
  const prices = (open: number, high: number, low: number, close: number) =>
    `开 ${formatNumber(open)}　高 ${formatNumber(high)}　低 ${formatNumber(low)}　收 ${formatNumber(close)}`;
  return [
    row.date,
    `前复权　${prices(row.open, row.high, row.low, row.close)}`,
    `真实价　${prices(row.open_raw, row.high_raw, row.low_raw, row.close_raw)}`,
    `涨跌幅　${formatPercentValue(row.pct_chg, 2, true)}`,
    `成交额　${formatYuan(row.amount)}`,
  ].join("<br/>");
}

export function buildKlineOption(
  data: KlineResponse,
  triggers: string[],
  highlight: Highlight | null,
): EChartsCoreOption {
  const dates = data.rows.map((row) => row.date);
  const rows = new Map(data.rows.map((row) => [row.date, row]));
  const marks = triggers
    .filter((day) => rows.has(day))
    .map((day) => ({ name: "触发", coord: [day, rows.get(day)!.high], value: "触发" }));
  const area =
    highlight && rows.has(highlight.start)
      ? [[{ xAxis: highlight.start }, { xAxis: rows.has(highlight.end) ? highlight.end : dates.at(-1) }]]
      : [];

  return {
    animation: false,
    title: {
      text: `前复权（以 ${data.base_date ?? "—"} 为基准）· 蓝色三角是触发日`,
      left: 0,
      top: 0,
      textStyle: { fontSize: 12, fontWeight: "normal", color: "#888" },
    },
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "cross" },
      formatter: (params: unknown) => {
        const first = (Array.isArray(params) ? params[0] : params) as { dataIndex?: number };
        const row = first?.dataIndex === undefined ? undefined : data.rows[first.dataIndex];
        return row ? klineTooltip(row) : "";
      },
    },
    axisPointer: { link: [{ xAxisIndex: "all" }] },
    grid: [
      { left: 64, right: 24, top: 32, height: "56%" },
      { left: 64, right: 24, top: "72%", height: "14%" },
    ],
    xAxis: [
      { type: "category", data: dates, boundaryGap: true, axisLine: { onZero: false } },
      { type: "category", gridIndex: 1, data: dates, axisLabel: { show: false }, axisTick: { show: false } },
    ],
    yAxis: [
      { scale: true },
      {
        gridIndex: 1,
        splitNumber: 2,
        axisLabel: { formatter: (value: number) => formatYuan(value) },
      },
    ],
    dataZoom: [
      { type: "inside", xAxisIndex: [0, 1] },
      { type: "slider", xAxisIndex: [0, 1], bottom: 8 },
    ],
    series: [
      {
        name: "K线",
        type: "candlestick",
        // ECharts 的顺序是 开、收、低、高
        data: data.rows.map((row) => [row.open, row.close, row.low, row.high]),
        itemStyle: {
          color: UP_COLOR,
          color0: DOWN_COLOR,
          borderColor: UP_COLOR,
          borderColor0: DOWN_COLOR,
        },
        markPoint: {
          symbol: "triangle",
          symbolSize: 10,
          symbolRotate: 180,
          symbolOffset: [0, -12],
          itemStyle: { color: TRIGGER_COLOR },
          label: { show: false },
          data: marks,
        },
        markArea: { itemStyle: { color: "rgba(22, 119, 255, 0.10)" }, data: area },
      },
      {
        name: "成交额",
        type: "bar",
        xAxisIndex: 1,
        yAxisIndex: 1,
        data: data.rows.map((row) => ({
          value: row.amount,
          itemStyle: { color: row.close >= row.open ? UP_COLOR : DOWN_COLOR },
        })),
      },
    ],
  };
}
