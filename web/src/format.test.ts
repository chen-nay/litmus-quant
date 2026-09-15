import { describe, expect, it } from "vitest";

import {
  DOWN_COLOR,
  UP_COLOR,
  cellColor,
  columnTitle,
  compareCells,
  excludeLabel,
  fieldMap,
  formatBps,
  formatCell,
  formatDelay,
  formatFraction,
  formatTime,
  formatYuan,
  sortColumn,
  trendColor,
  visibleColumns,
} from "./format";

describe("三种单位", () => {
  it("字段单位是 % 的是百分数：1.41 就是 1.41%，涨跌类带正号", () => {
    expect(formatCell("pct_chg", 1.41, "%")).toBe("+1.41%");
    expect(formatCell("pct_chg", -0.27, "%")).toBe("-0.27%");
    expect(formatCell("turnover", 3.2, "%")).toBe("3.20%");
  });

  it("个股回看的涨跌是小数：-0.0078 就是 -0.78%", () => {
    expect(formatFraction(-0.0078)).toBe("-0.78%");
    expect(formatFraction(0.129)).toBe("+12.90%");
    expect(formatFraction(0.25, 0, false)).toBe("25%");
  });

  it("按 Pct(...) 排序时排序值是小数：0.1234 就是 +12.34%，也红涨绿跌", () => {
    const meta = sortColumn({ by: "Pct($close, 20)", label: "20 日涨幅" }, new Map());
    expect(meta).toEqual({ name: "", label: "20 日涨幅", unit: "小数" });
    expect(formatCell(meta.name, 0.1234, meta.unit)).toBe("+12.34%");
    expect(cellColor(meta.name, -0.02, meta.unit)).toBe(DOWN_COLOR);
    expect(sortColumn({ by: "Pct($close, 20) / Std($pct_chg, 20)", label: "" }, new Map()).unit).toBe("");
  });

  it("成本是基点：30 就是 0.30%", () => {
    expect(formatBps(30)).toBe("0.30%");
    expect(formatBps(5)).toBe("0.05%");
  });
});

describe("金额与空值", () => {
  it("金额按亿、万显示", () => {
    expect(formatCell("amount", 840_000_000, "元")).toBe("8.40亿");
    expect(formatCell("market_cap", 1.2e12, "元")).toBe("12000.00亿");
    expect(formatYuan(12_345)).toBe("1.23万");
    expect(formatYuan(999)).toBe("999.00");
    expect(formatYuan(-2e8)).toBe("-2.00亿");
  });

  it("价格保留两位，布尔值显示是否", () => {
    expect(formatCell("close_raw", 12.3, "元")).toBe("12.30");
    expect(formatCell("is_st", true, "布尔")).toBe("是");
    expect(formatCell("limit_up_num", 6, "个")).toBe("6");
  });

  it("空值、NaN 显示横线；四舍五入成 0 的不带负号", () => {
    expect(formatFraction(null)).toBe("—");
    expect(formatCell("close", Number.NaN, "元")).toBe("—");
    expect(formatCell("close", null, "元")).toBe("—");
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

  it("只有涨跌类字段上色", () => {
    expect(cellColor("pct_chg", -2)).toBe(DOWN_COLOR);
    expect(cellColor("close", 5)).toBeUndefined();
  });
});

describe("列名", () => {
  const fields = fieldMap([
    { name: "$close", label: "收盘价", unit: "元", type: "数值", note: "", time_series_ok: true },
    { name: "$amount", label: "成交额", unit: "元", type: "数值", note: "", time_series_ok: true },
    { name: "$pct_chg", label: "当日涨跌幅", unit: "%", type: "数值", note: "", time_series_ok: true },
    { name: "$pe_ttm", label: "市盈率TTM", unit: "倍", type: "数值", note: "", time_series_ok: true },
  ]);

  it("价格写单位；板块的价格是点位；金额、百分数不写", () => {
    expect(columnTitle(fields.get("close")!, false)).toBe("收盘价（元）");
    expect(columnTitle(fields.get("close")!, true)).toBe("收盘价（点）");
    expect(columnTitle(fields.get("amount")!, false)).toBe("成交额");
    expect(columnTitle(fields.get("pct_chg")!, false)).toBe("当日涨跌幅");
    expect(columnTitle(fields.get("pe_ttm")!, false)).toBe("市盈率TTM（倍）");
  });

  it("排序值那一列：没指定排序是成交额，单个字段用字段名，有名称用名称，表达式叫排序值", () => {
    expect(sortColumn(null, fields)).toEqual({ name: "amount", label: "成交额", unit: "元" });
    expect(sortColumn({ by: " $pct_chg ", label: "" }, fields).label).toBe("当日涨跌幅");
    expect(sortColumn({ by: "$amount / Mean($amount, 5)", label: "放大倍数" }, fields).label).toBe(
      "放大倍数",
    );
    expect(sortColumn({ by: "$amount / Mean($amount, 5)", label: "" }, fields)).toEqual({
      name: "",
      label: "排序值",
      unit: "",
    });
  });
});

describe("其他", () => {
  it("顺延说明", () => {
    expect(formatDelay({ days: 1, reason: "涨停" }, "买入")).toBe("买入顺延 1 天（涨停）");
    expect(formatDelay(null, "卖出")).toBe("");
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

describe("页面上看出来的问题", () => {
  it("名称里已经有括号的，单位并进去", () => {
    expect(columnTitle({ name: "close_raw", label: "收盘价（不复权）", unit: "元" }, false)).toBe(
      "收盘价（不复权，元）",
    );
  });

  it("按结果里已有的一列排序时，不重复显示排序值", () => {
    const columns = ["code", "name", "sort_value", "close", "pct_chg", "amount"];
    const bySameField = { name: "pct_chg", label: "当日涨跌幅", unit: "%" };
    const byExpression = { name: "", label: "放大倍数", unit: "" };
    expect(visibleColumns(columns, bySameField)).toEqual(["code", "name", "close", "pct_chg", "amount"]);
    expect(visibleColumns(columns, byExpression)).toEqual(columns);
  });
});
