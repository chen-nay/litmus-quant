import dayjs from "dayjs";
import { describe, expect, it } from "vitest";

import {
  buildBoardList,
  buildHistory,
  buildStockList,
  disabledDay,
  fieldForIssue,
  issueText,
  splitIssues,
} from "./specForm";
import type { EventInfo } from "./types";

const BREAKOUT: EventInfo = {
  id: "breakout_ma",
  name: "突破均线",
  category: "均线",
  template: "Cross($close, Mean($close, {ma}))",
  label: "突破 {ma} 日均线",
  params: [
    {
      name: "ma",
      label: "均线天数",
      unit: "天",
      allowed: "5/10/20/60/120/250",
      default: 250,
      kind: "choice",
      choices: [5, 10, 20, 60, 120, 250],
      min: null,
      max: null,
    },
  ],
  constraints: [],
  example: { question: "", params: { ma: 250 } },
};

describe("表单 → 查询条件", () => {
  it("股票表：日期写成 YYYY-MM-DD，没填的条件是 null，文字数字转成数值", () => {
    const spec = buildStockList({
      as_of: dayjs("2026-09-11"),
      limit: "50",
      filter: { expr: "   " },
      sort: { by: " $amount ", order: "desc", label: "" },
      universe: { base: "all_a", industry: undefined, board: "880728.TDX", exclude: ["ST"] },
    });
    expect(spec).toEqual({
      shape: "stock_list",
      as_of: "2026-09-11",
      filter: null,
      sort: { by: "$amount", order: "desc", label: "" },
      limit: 50,
      universe: {
        base: "all_a",
        industry: null,
        board: { type: "concept", code: "880728.TDX" },
        exclude: ["ST"],
      },
    });
  });

  it("板块表", () => {
    const spec = buildBoardList({
      board_type: "concept",
      as_of: dayjs("2026-09-11"),
      limit: 3,
      filter: { expr: "$pct_chg > 1", label: "涨超 1%" },
    });
    expect(spec.filter).toEqual({ expr: "$pct_chg > 1", label: "涨超 1%" });
    expect(spec.sort).toBeNull();
  });

  it("个股回看：参数和持有天数转成数值，持有天数去重排序，代码转大写，没填的参数用默认值", () => {
    const spec = buildHistory(
      {
        target: { code: " 600519.sh " },
        event: { preset_id: "breakout_ma", params: {} },
        time_range: [dayjs("2025-01-01"), dayjs("2025-12-31")],
        horizons: ["20", 5, "5"],
        benchmark: "index:000300.SH",
        cost_bps: "30",
      },
      BREAKOUT,
    );
    expect(spec).toEqual({
      shape: "stock_history",
      target: { code: "600519.SH" },
      event: { preset_id: "breakout_ma", params: { ma: 250 } },
      time_range: { from: "2025-01-01", to: "2025-12-31" },
      horizons: [5, 20],
      benchmark: "index:000300.SH",
      cost_bps: 30,
    });
  });

  it("写错的数字发出去是 null，由接口报错", () => {
    const spec = buildBoardList({ board_type: "sw_industry", as_of: dayjs("2026-09-11"), limit: "" });
    expect(JSON.parse(JSON.stringify(spec)).limit).toBeNull();
  });

  it("日期选择：周末和本地数据之外的日子不能选", () => {
    const disabled = disabledDay("2016-01-04", "2026-09-11");
    expect(disabled(dayjs("2026-09-11"))).toBe(false);
    expect(disabled(dayjs("2026-09-12"))).toBe(true); // 星期六
    expect(disabled(dayjs("2026-09-14"))).toBe(true); // 本地数据之后
    expect(disabled(dayjs("2015-12-31"))).toBe(true);
  });
});

describe("问题 → 输入框", () => {
  const fields = [
    ["as_of"],
    ["filter", "expr"],
    ["time_range"],
    ["horizons"],
    ["universe", "board"],
    ["event", "params", "ma"],
  ];

  it("路径一样的直接对上", () => {
    expect(fieldForIssue("filter.expr", fields)).toEqual(["filter", "expr"]);
    expect(fieldForIssue("event.params.ma", fields)).toEqual(["event", "params", "ma"]);
  });

  it("更细的路径对到最近的输入框", () => {
    expect(fieldForIssue("time_range.from", fields)).toEqual(["time_range"]);
    expect(fieldForIssue("horizons.1", fields)).toEqual(["horizons"]);
    expect(fieldForIssue("universe.board.code", fields)).toEqual(["universe", "board"]);
  });

  it("对不上的放到表单上方", () => {
    const split = splitIssues(
      [
        { path: "filter.expr", message: "缺少右括号", allowed: null, position: 7 },
        { path: null, message: "本地股票日频缺 2020-03", allowed: null, position: null },
        { path: "colour", message: "不认识这一项", allowed: null, position: null },
      ],
      fields,
    );
    expect(split.fields).toEqual([{ name: ["filter", "expr"], errors: ["第 8 个字符：缺少右括号"] }]);
    expect(split.other).toEqual(["本地股票日频缺 2020-03", "colour：不认识这一项"]);
  });

  it("说明里没写可选范围的补上", () => {
    expect(
      issueText({ path: "benchmark", message: "对照写错了", allowed: "沪深300、中证500", position: null }),
    ).toBe("对照写错了（可选：沪深300、中证500）");
    expect(
      issueText({ path: "x", message: "可选 1.2~10，收到 30", allowed: "1.2~10", position: null }),
    ).toBe("可选 1.2~10，收到 30");
  });
});
