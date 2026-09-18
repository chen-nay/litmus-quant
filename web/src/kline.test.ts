import { describe, expect, it } from "vitest";

import { DOWN_COLOR, UP_COLOR } from "./format";
import { buildKlineOption, chartWindow, klineTooltip } from "./kline";
import type { HistoryResult, KlineResponse } from "./types";

const DATA: KlineResponse = {
  code: "000034.SZ",
  name: "神州数码",
  adjust: "前复权",
  base_date: "2026-05-20",
  range: ["2026-05-18", "2026-05-20"],
  rows: [
    { date: "2026-05-18", open: 10, high: 11, low: 9, close: 10.5, open_raw: 14, high_raw: 15.4, low_raw: 12.6, close_raw: 14.7, amount: 1e8, pct_chg: 1 },
    { date: "2026-05-19", open: 10.5, high: 11, low: 10, close: 10.2, open_raw: 10.5, high_raw: 11, low_raw: 10, close_raw: 10.2, amount: 2e8, pct_chg: -2.86 },
    { date: "2026-05-20", open: 10.2, high: 10.8, low: 10.1, close: 10.6, open_raw: 10.2, high_raw: 10.8, low_raw: 10.1, close_raw: 10.6, amount: 3e8, pct_chg: 3.92 },
  ],
}; // prettier-ignore

interface Series {
  data: unknown[];
  itemStyle?: Record<string, string>;
  markPoint?: { data: { coord: [string, number] }[] };
  markArea?: { data: unknown[] };
}

function series(option: ReturnType<typeof buildKlineOption>): Series[] {
  return option.series as Series[];
}

describe("K 线配置", () => {
  it("每根 K 线按 ECharts 的顺序：开、收、低、高；红涨绿跌", () => {
    const [candles, volume] = series(buildKlineOption(DATA, [], null));
    expect(candles.data[0]).toEqual([10, 10.5, 9, 11]);
    expect(candles.itemStyle).toMatchObject({ color: UP_COLOR, color0: DOWN_COLOR });
    expect(volume.data.map((item) => (item as { itemStyle: { color: string } }).itemStyle.color)).toEqual(
      [UP_COLOR, DOWN_COLOR, UP_COLOR],
    );
  });

  it("只标图里有行情的触发日，标在当天最高价上", () => {
    const [candles] = series(buildKlineOption(DATA, ["2026-05-19", "2026-01-05"], null));
    expect(candles.markPoint?.data.map((item) => item.coord)).toEqual([["2026-05-19", 11]]);
  });

  it("高亮买入日到卖出日；卖出日不在图里时画到最后一天；没选就不画", () => {
    const pick = (highlight: { start: string; end: string } | null) =>
      series(buildKlineOption(DATA, [], highlight))[0].markArea?.data;
    expect(pick({ start: "2026-05-18", end: "2026-05-19" })).toEqual([
      [{ xAxis: "2026-05-18" }, { xAxis: "2026-05-19" }],
    ]);
    expect(pick({ start: "2026-05-18", end: "2026-06-30" })).toEqual([
      [{ xAxis: "2026-05-18" }, { xAxis: "2026-05-20" }],
    ]);
    expect(pick(null)).toEqual([]);
  });

  it("悬停说明同时给前复权和真实价", () => {
    const text = klineTooltip(DATA.rows[0]);
    expect(text).toContain("前复权　开 10.00　高 11.00　低 9.00　收 10.50");
    expect(text).toContain("真实价　开 14.00");
    expect(text).toContain("涨跌幅　+1.00%");
    expect(text).toContain("成交额　1.00亿");
  });
});

describe("图的范围", () => {
  const result = (exits: (string | null)[]): HistoryResult => ({
    kind: "event_study",
    understood: "平安银行历史上每次涨停之后，接下来 5 个交易日涨跌多少",
    assumptions: [],
    code: "000001.SZ",
    name: "平安银行",
    event_label: "涨停",
    range: ["2025-01-02", "2025-12-31"],
    benchmark: "universe_equal_weight",
    cost_bps: 30,
    summary: {},
    notes: [],
    triggers: [
      {
        trigger_date: "2025-12-20",
        entry_date: "2025-12-22",
        entry_delay: null,
        status: {},
        exit_date: Object.fromEntries(exits.map((exit, i) => [String(i), exit])),
        exit_delay: {},
        returns: {},
        market_returns: {},
        market_excluded: {},
        notes: [],
      },
    ],
  });

  it("画到最后一笔卖出日", () => {
    expect(chartWindow(result(["2025-12-29", "2026-03-20"]))).toEqual({ from: "2025-01-02", to: "2026-03-20" });
  });

  it("还没走完的没有卖出日，画到统计区间终点", () => {
    expect(chartWindow(result([null]))).toEqual({ from: "2025-01-02", to: "2025-12-31" });
  });
});
