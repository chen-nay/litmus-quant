/** 结果页 /runs/<运行编号>：这个地址就是分享链接，打开时从运行记录取回，不重算。 */

import { Alert, Card, Descriptions, Result, Spin } from "antd";
import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { api, errorText } from "../api";
import { Assumptions } from "../components/Assumptions";
import { CardView } from "../components/CardView";
import { HistoryResultView } from "../components/HistoryResultView";
import { TableResultView } from "../components/TableResultView";
import { TraceView } from "../components/TraceView";
import { useCatalog } from "../context";
import { formatTime, kindTitle } from "../format";
import type { RunRecord } from "../types";

export function RunPage() {
  const { runId = "" } = useParams();
  const catalog = useCatalog();
  const [record, setRecord] = useState<RunRecord | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setRecord(null);
    setError(null);
    api.record(runId).then(
      (value) => {
        if (alive) setRecord(value);
      },
      (reason) => {
        if (alive) setError(errorText(reason));
      },
    );
    return () => {
      alive = false;
    };
  }, [runId]);

  if (error) {
    return <Result status="warning" title="打不开这条结果" subTitle={error} extra={<Link to="/">回到查询</Link>} />;
  }
  if (!record) {
    return (
      <div style={{ textAlign: "center", padding: 48 }}>
        <Spin />
      </div>
    );
  }

  const { spec, result } = record;
  // 旧版查询结构存下的记录：结果里没有 kind，这一版的结果页读不了
  if (record.status === "done" && !result?.kind) {
    return (
      <Result
        status="info"
        title="这是旧版查询结构的记录，打不开"
        subTitle={`运行编号 ${record.run_id}，${formatTime(record.created_at)}`}
        extra={<Link to="/">回到查询</Link>}
      />
    );
  }

  const meta = [
    { key: "run_id", label: "运行编号", children: record.run_id },
    { key: "created_at", label: "运行时间", children: formatTime(record.created_at) },
    { key: "data_through", label: "数据截至", children: record.data_through ?? "—" },
    {
      key: "duration",
      label: "耗时",
      children: record.duration_ms === null ? "—" : `${(record.duration_ms / 1000).toFixed(2)} 秒`,
    },
  ];
  const kind = result?.kind ?? spec.output.kind;
  // 打开分享链接的人看不到原来的问题，标题用确认卡上「理解成」的那句话；运行失败的没有结果，只写是什么
  const title = result?.understood || kindTitle(kind, spec.scope.target);

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <Card size="small" title={title} extra={<Link to="/">再问一个</Link>}>
        <Descriptions size="small" column={4} items={meta} />
        {result && result.kind !== "card" && result.assumptions.length > 0 && (
          <details style={{ marginTop: 8 }}>
            <summary style={{ cursor: "pointer", color: "#1677ff" }}>确认卡上的说明（{result.assumptions.length} 条）</summary>
            <div style={{ marginTop: 8 }}>
              <Assumptions items={result.assumptions} />
            </div>
          </details>
        )}
        {catalog.settings?.show_trace && <TraceView runId={record.run_id} planId={record.plan_id} />}
      </Card>
      {record.status === "failed" && (
        <Alert
          type="error"
          showIcon
          title="计算时出错了"
          description={`${record.error ?? ""}（运行编号 ${record.run_id}，详细错误在服务日志里）`}
        />
      )}
      {result?.kind === "card" && <CardView result={result} runId={record.run_id} narrative={record.narrative} />}
      {result?.kind === "table" && <TableResultView result={result} target={spec.scope.target} />}
      {result?.kind === "event_study" && <HistoryResultView result={result} />}
    </div>
  );
}
