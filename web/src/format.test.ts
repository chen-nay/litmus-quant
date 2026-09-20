import { describe, expect, it } from "vitest";

import {
  DOWN_COLOR,
  PERCENTILE_UNIT,
  RATIO_UNIT,
  UP_COLOR,
  cellColor,
  columnTitle,
  compareCells,
  excludeLabel,
  formatBps,
  formatCell,
  formatDelay,
  formatFraction,
  formatTime,
  formatYuan,
  kindTitle,
  latestText,
  trendColor,
} from "./format";
import type { Column } from "./types";

const column = (unit: string, field: string | null = null, name = "列"): Column => ({ name, unit, field });

describe("几种单位", () => {
  it("字段单位是 % 的是百分数：1.41 就是 1.41%，涨跌类带正号", () => {
    expect(formatCell(1.41, column("%", "pct_chg"))).toBe("+1.41%");
    expect(formatCell(-0.27, column("%", "pct_chg"))).toBe("-0.27%");
    expect(formatCell(3.2, column("%", "turnover"))).toBe("3.20%");
  });

  it("小数百分比（Pct、PctSince、Rank 算出来的）：0.1234 就是 +12.34%，也红涨绿跌", () => {
    expect(formatCell(0.1234, column(RATIO_UNIT))).toBe("+12.34%");
    expect(cellColor(-0.02, column(RATIO_UNIT))).toBe(DOWN_COLOR);
    expect(formatFraction(-0.0078)).toBe("-0.78%");
    expect(formatFraction(0.25, 0, false)).toBe("25%");
  });

  it("分位也按百分比显示，但不带正负号、不上色", () => {
    expect(formatCell(0.0066, column(PERCENTILE_UNIT))).toBe("0.66%");
    expect(cellColor(0.0066, column(PERCENTILE_UNIT))).toBeUndefined();
  });

  it("成本是基点：30 就是 0.30%", () => {
    expect(formatBps(30)).toBe("0.30%");
    expect(formatBps(5)).toBe("0.05%");
  });
});

describe("金额与空值", () => {
  it("成交额、市值按亿、万显示；同样单位是元的价格不按", () => {
    expect(formatCell(840_000_000, column("元", "amount"))).toBe("8.40亿");
    expect(formatCell(1.2e12, column("元", "market_cap"))).toBe("12000.00亿");
    expect(formatCell(12_345.6, column("元", "close"))).toBe("12345.60");
    expect(formatYuan(12_345)).toBe("1.23万");
    expect(formatYuan(-2e8)).toBe("-2.00亿");
  });

  it("布尔值显示是否，个数、天数不要小数", () => {
    expect(formatCell(true, column("布尔", "is_st"))).toBe("是");
    expect(formatCell(6, column("个", "limit_up_num"))).toBe("6");
    expect(formatCell(31, column("天", "list_days"))).toBe("31");
  });

  it("空值、NaN 显示横线；四舍五入成 0 的不带负号", () => {
    expect(formatFraction(null)).toBe("—");
    expect(formatCell(Number.NaN, column("元", "close"))).toBe("—");
    expect(formatCell(null, column("元", "close"))).toBe("—");
    expect(formatFraction(-0.00001)).toBe("0.00%");
  });
});

describe("红涨绿跌", () => {
  it("正数红、负数绿、零和空值不上色", () => {
    expect(trendColor(1)).toBe(UP_COLOR);
    expect(trendColor(-1)).toBe(DOWN_COLOR);
    expect(trendColor(0)).toBeUndefined();
    expect(trendColor(null)).toBeUndefined();
  });

  it("只有涨跌类上色", () => {
    expect(cellColor(-2, column("%", "pct_chg"))).toBe(DOWN_COLOR);
    expect(cellColor(5, column("元", "close"))).toBeUndefined();
    expect(cellColor(5, column("倍", "pe_ttm"))).toBeUndefined();
  });
});

describe("列名", () => {
  it("价格、倍数写单位；板块的价格是点位；金额、百分数、小数百分比不写", () => {
    expect(columnTitle(column("元", "close", "收盘价"), false)).toBe("收盘价（元）");
    expect(columnTitle(column("元", "close", "收盘价"), true)).toBe("收盘价（点）");
    expect(columnTitle(column("元", "amount", "成交额"), false)).toBe("成交额");
    expect(columnTitle(column("%", "pct_chg", "当日涨跌幅"), false)).toBe("当日涨跌幅");
    expect(columnTitle(column(RATIO_UNIT, null, "今年以来涨幅"), false)).toBe("今年以来涨幅");
    expect(columnTitle(column("倍", "pe_ttm", "市盈率TTM"), false)).toBe("市盈率TTM（倍）");
  });

  it("名称里已经有括号的，单位并进去", () => {
    expect(columnTitle(column("元", "close_raw", "收盘价（不复权）"), false)).toBe("收盘价（不复权，元）");
  });

  it("结果的标题：股票表、板块表写明哪一类，卡、统计", () => {
    expect(kindTitle("table")).toBe("股票表");
    expect(kindTitle("table", "sw_industry_l2")).toBe("板块表 · 申万二级行业");
    expect(kindTitle("card")).toBe("卡");
    expect(kindTitle("event_study")).toBe("统计");
  });
});

describe("其他", () => {
  it("顺延说明", () => {
    expect(formatDelay({ days: 1, reason: "涨停" }, "买入")).toBe("买入顺延 1 天（涨停）");
    expect(formatDelay(null, "卖出")).toBe("");
  });

  it("各类数据截至哪天：同一天的并在一起", () => {
    expect(
      latestText([
        { key: "stock", label: "股票行情", date: "2026-09-14" },
        { key: "concept", label: "概念板块行情", date: "2026-09-11" },
        { key: "index", label: "指数行情", date: "2026-09-14" },
      ]),
    ).toBe("股票行情、指数行情 2026-09-14；概念板块行情 2026-09-11");
    expect(latestText(undefined)).toBe("");
  });

  it("时间取到秒，不换时区", () => {
    expect(formatTime("2026-09-14T21:59:43+08:00")).toBe("2026-09-14 21:59:43");
    expect(formatTime(null)).toBe("—");
  });

  it("排序比较：空值排最后", () => {
    expect([3, null, 1].sort(compareCells)).toEqual([1, 3, null]);
    expect(compareCells("银行", "电子")).not.toBe(0);
  });

  it("剔除项的中文名", () => {
    expect(excludeLabel("new_listing_60d")).toBe("上市不满 60 个交易日");
    expect(excludeLabel("suspended")).toBe("停牌");
  });
});
