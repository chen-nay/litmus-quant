import { describe, expect, it } from "vitest";

import { runTitle, summarize } from "./specSummary";

describe("结果页的条件说明", () => {
  it("股票表：没指定排序说明按成交额，股票池和剔除项用中文", () => {
    const items = summarize(
      {
        shape: "stock_list",
        as_of: "2026-09-11",
        filter: { expr: "$pct_chg > 9", label: "涨超 9%" },
        sort: null,
        limit: 5,
        universe: {
          base: "all_a",
          industry: null,
          board: { type: "concept", code: "880728.TDX" },
          exclude: ["ST", "new_listing_60d"],
        },
      },
      new Map([["880728.TDX", "航运概念"]]),
    );
    const text = Object.fromEntries(items.map((item) => [item.label, item.children]));
    expect(text).toEqual({
      日期: "2026-09-11",
      筛选: "涨超 9%：$pct_chg > 9",
      排序: "成交额从高到低（没有指定排序）",
      取前: "5 名",
      股票池: "沪深A股 · 概念板块「航运概念」",
      剔除: "ST / *ST、上市不满 60 个交易日",
    });
  });

  it("个股回看：成本按百分比显示，标题分形状", () => {
    const spec = {
      shape: "stock_history" as const,
      target: { code: "000001.SZ" },
      event: { preset_id: "volume_surge", params: { volume_ratio: 3 }, label: "单日放量 3 倍" },
      time_range: { from: "2025-01-01", to: "2025-12-31" },
      horizons: [5, 20],
      benchmark: "universe_equal_weight",
      cost_bps: 30,
    };
    const text = Object.fromEntries(summarize(spec).map((item) => [item.label, item.children]));
    expect(text["事件"]).toBe("单日放量 3 倍（volume_ratio=3）");
    expect(text["交易成本"]).toBe("0.30%（买卖双边合计）");
    expect(text["同期对照"]).toBe("买入日全A等权");
    expect(runTitle(spec)).toBe("个股回看");
  });
});
