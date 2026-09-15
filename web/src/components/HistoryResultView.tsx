/** 个股回看的结果：K 线图、按持有天数的汇总、每次触发的明细（ARCHITECTURE §4.3、§4.4）。 */

import { Alert, Card, Segmented, Table, Tag, Typography, type TableColumnsType } from "antd";
import { useMemo, useState } from "react";

import { benchmarkLabel, formatBps, formatDelay, formatFraction, trendColor } from "../format";
import { type Highlight, chartWindow } from "../kline";
import type { HistoryResult, HorizonSummary, TriggerRecord } from "../types";
import { KlineChart } from "./KlineChart";

type SummaryRow = HorizonSummary & { horizon: number };

const STATUS_COLORS: Record<string, string> = {
  完成: "default",
  退市: "orange",
  无法成交: "red",
  观察中: "blue",
};

function Change({ value }: { value: number | null | undefined }) {
  return <span style={{ color: trendColor(value) }}>{formatFraction(value)}</span>;
}

export function HistoryResultView({ result }: { result: HistoryResult }) {
  const horizons = useMemo(
    () => Object.keys(result.summary).map(Number).sort((a, b) => a - b),
    [result],
  );
  const [horizon, setHorizon] = useState(horizons[0] ?? 5);
  const [selected, setSelected] = useState<string | null>(null);
  const key = String(horizon);
  const view = useMemo(() => chartWindow(result), [result]);
  const triggerDays = useMemo(() => result.triggers.map((item) => item.trigger_date), [result]);
  const picked = result.triggers.find((item) => item.trigger_date === selected);
  const highlight = useMemo<Highlight | null>(
    () =>
      picked?.entry_date ? { start: picked.entry_date, end: picked.exit_date[key] ?? view.to } : null,
    [picked, key, view.to],
  );

  const summaryRows: SummaryRow[] = horizons.map((h) => ({ horizon: h, ...result.summary[String(h)] }));
  const change = (value: number | null) => <Change value={value} />;
  const summaryColumns: TableColumnsType<SummaryRow> = [
    { title: "持有", dataIndex: "horizon", render: (h: number) => `${h} 个交易日` },
    { title: "笔数", dataIndex: "n", align: "right" },
    { title: "平均涨跌", dataIndex: "mean_return", align: "right", render: change },
    { title: "扣成本后", dataIndex: "mean_return_after_cost", align: "right", render: change },
    { title: "同期对照", dataIndex: "mean_market_return", align: "right", render: change },
    { title: "这只股票平时", dataIndex: "mean_baseline_return", align: "right", render: change },
    {
      title: "跑赢对照的比例",
      dataIndex: "win_rate",
      align: "right",
      render: (value: number | null) => formatFraction(value, 0, false),
    },
    { title: "无法成交", dataIndex: "unfilled", align: "right" },
    { title: "观察中", dataIndex: "pending", align: "right" },
  ];

  const triggerColumns: TableColumnsType<TriggerRecord> = [
    { title: "触发日", dataIndex: "trigger_date", width: 110 },
    {
      title: "买入日",
      key: "entry",
      render: (_: unknown, item: TriggerRecord) => (
        <>
          {item.entry_date ?? "—"}
          {item.entry_delay && (
            <Typography.Text type="secondary"> {formatDelay(item.entry_delay, "买入")}</Typography.Text>
          )}
        </>
      ),
    },
    {
      title: "卖出日",
      key: "exit",
      render: (_: unknown, item: TriggerRecord) => (
        <>
          {item.exit_date[key] ?? "—"}
          {item.exit_delay[key] && (
            <Typography.Text type="secondary"> {formatDelay(item.exit_delay[key], "卖出")}</Typography.Text>
          )}
        </>
      ),
    },
    {
      title: `之后 ${horizon} 天涨跌`,
      key: "return",
      align: "right",
      render: (_: unknown, item: TriggerRecord) => <Change value={item.returns[key]} />,
    },
    {
      title: "同期对照",
      key: "market",
      align: "right",
      render: (_: unknown, item: TriggerRecord) => <Change value={item.market_returns[key]} />,
    },
    {
      title: "状态",
      key: "status",
      render: (_: unknown, item: TriggerRecord) =>
        item.status[key] ? <Tag color={STATUS_COLORS[item.status[key]] ?? "default"}>{item.status[key]}</Tag> : "—",
    },
    {
      title: "备注",
      key: "notes",
      render: (_: unknown, item: TriggerRecord) => {
        const excluded = item.market_excluded[key] ?? 0;
        const notes = [...item.notes, excluded > 0 ? `对照剔除 ${excluded} 只（卖出日停牌）` : ""];
        return notes.filter(Boolean).join("；");
      },
    },
  ];

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <Card size="small" title={`${result.name ?? result.code}（${result.code}）· ${result.event_label}`}>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 24, marginBottom: 12 }}>
          <span>
            统计区间 {result.range[0]} ~ {result.range[1]}
          </span>
          <span>触发 {result.triggers.length} 次</span>
          <span>同期对照：{benchmarkLabel(result.benchmark)}</span>
          <span>交易成本 {formatBps(result.cost_bps)}（买卖双边合计）</span>
        </div>
        {result.notes.length > 0 && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            title={result.notes.length === 1 ? result.notes[0] : "提示"}
            description={
              result.notes.length > 1 ? result.notes.map((note) => <div key={note}>{note}</div>) : undefined
            }
          />
        )}
        <KlineChart code={result.code} from={view.from} to={view.to} triggers={triggerDays} highlight={highlight} />
        <Typography.Text type="secondary">
          涨跌从买入日开盘算到卖出日收盘，不扣成本。点下面「每次触发」里的一行，图上高亮那一笔从买入到卖出。
        </Typography.Text>
      </Card>

      <Card size="small" title="汇总（只算完成、退市的笔数）">
        <Table<SummaryRow>
          size="small"
          rowKey="horizon"
          pagination={false}
          columns={summaryColumns}
          dataSource={summaryRows}
          scroll={{ x: "max-content" }}
        />
      </Card>

      <Card
        size="small"
        title="每次触发"
        extra={
          <Segmented
            value={horizon}
            onChange={(value) => setHorizon(Number(value))}
            options={horizons.map((h) => ({ value: h, label: `持有 ${h} 天` }))}
          />
        }
      >
        <Table<TriggerRecord>
          size="small"
          rowKey="trigger_date"
          columns={triggerColumns}
          dataSource={result.triggers}
          pagination={{ pageSize: 20, hideOnSinglePage: true }}
          scroll={{ x: "max-content" }}
          onRow={(item) => ({
            onClick: () => setSelected(item.trigger_date === selected ? null : item.trigger_date),
            style: {
              cursor: "pointer",
              background: item.trigger_date === selected ? "#e6f4ff" : undefined,
            },
          })}
        />
      </Card>
    </div>
  );
}
