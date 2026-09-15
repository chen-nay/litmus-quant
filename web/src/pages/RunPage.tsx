/** 结果页 /runs/<运行编号>：这个地址就是分享链接，打开时从运行记录取回，不重算。 */

import { Alert, Card, Descriptions, Result, Spin } from "antd";
import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { api, errorText } from "../api";
import { HistoryResultView } from "../components/HistoryResultView";
import { ListResultView } from "../components/ListResultView";
import { useCatalog } from "../context";
import { formatTime } from "../format";
import { runTitle, summarize } from "../specSummary";
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

  const boardNames = useMemo(
    () => new Map((catalog.concept?.boards ?? []).map((board) => [board.code, board.name])),
    [catalog.concept],
  );

  if (error) {
    return (
      <Result status="warning" title="打不开这条结果" subTitle={error} extra={<Link to="/">回到查询</Link>} />
    );
  }
  if (!record) {
    return (
      <div style={{ textAlign: "center", padding: 48 }}>
        <Spin />
      </div>
    );
  }

  const { spec, result } = record;
  const items = [
    ...summarize(spec, boardNames),
    { key: "run_id", label: "运行编号", children: record.run_id },
    { key: "created_at", label: "运行时间", children: formatTime(record.created_at) },
    { key: "data_through", label: "数据截至", children: record.data_through ?? "—" },
    {
      key: "duration",
      label: "耗时",
      children: record.duration_ms === null ? "—" : `${(record.duration_ms / 1000).toFixed(2)} 秒`,
    },
  ];

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <Card size="small" title={runTitle(spec)} extra={<Link to="/">再查一个</Link>}>
        <Descriptions size="small" column={3} items={items} />
        {spec.assumptions?.length ? (
          <details style={{ marginTop: 8 }}>
            <summary style={{ cursor: "pointer", color: "#1677ff" }}>
              确认卡上的说明（{spec.assumptions.length} 条）
            </summary>
            <ul style={{ paddingLeft: 20, marginBottom: 0 }}>
              {spec.assumptions.map((text) => (
                <li key={text}>{text}</li>
              ))}
            </ul>
          </details>
        ) : null}
      </Card>
      {record.status === "failed" && (
        <Alert
          type="error"
          showIcon
          title="计算时出错了"
          description={`${record.error ?? ""}（运行编号 ${record.run_id}，详细错误在服务日志里）`}
        />
      )}
      {result?.shape === "stock_history" && <HistoryResultView result={result} />}
      {result && result.shape !== "stock_history" && spec.shape !== "stock_history" && (
        <ListResultView result={result} spec={spec} />
      )}
    </div>
  );
}
