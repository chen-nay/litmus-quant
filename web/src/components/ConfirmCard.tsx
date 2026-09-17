/** 确认卡：把条件翻译成明确的定义，确认后才运行（README「交互：两步出结果」、ARCHITECTURE §5.4）。 */

import { Alert, Button, Card, Input, Space, Tag, Typography } from "antd";
import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { api, errorText } from "../api";
import { issueText } from "../specForm";
import { runTitle } from "../specSummary";
import type { AssumptionItem, Spec } from "../types";
import { Thinking } from "./askParts";

export interface Confirm {
  spec: Spec;
  /** 从提问来的带着提问编号；手填表单来的为空 */
  planId: string | null;
  assumptions: AssumptionItem[];
}

interface Problem {
  type: "warning" | "error";
  title: string;
  lines: string[];
}

export function ConfirmCard({
  confirm,
  revisingSince,
  onRevise,
  onEdit,
  onClose,
}: {
  confirm: Confirm;
  /** 正在让大模型改条件：从什么时候开始等，没在改就是 null */
  revisingSince: number | null;
  onRevise: (change: string) => void;
  onEdit: () => void;
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const [running, setRunning] = useState(false);
  const [change, setChange] = useState("");
  const [problem, setProblem] = useState<Problem | null>(null);
  const revising = revisingSince !== null;
  const busy = running || revising;

  async function run() {
    setRunning(true);
    setProblem(null);
    try {
      const response = await api.run(confirm.spec, confirm.planId);
      if (response.status === "done" && response.run_id) {
        navigate(`/runs/${response.run_id}`);
        return;
      }
      if (response.status === "needs_revision") {
        setProblem({ type: "warning", title: "条件要改一下", lines: response.issues.map(issueText) });
      } else {
        setProblem({
          type: response.status === "failed" ? "error" : "warning",
          title: response.message ?? "没能算出结果",
          lines: [],
        });
      }
    } catch (reason) {
      setProblem({ type: "error", title: errorText(reason), lines: [] });
    } finally {
      setRunning(false);
    }
  }

  function submitChange() {
    const text = change.trim();
    if (!text || busy) return;
    setProblem(null);
    onRevise(text);
  }

  return (
    <Card
      size="small"
      title={confirm.planId ? "我把你的问题理解成：" : "确认查询条件"}
      extra={
        <Button type="link" size="small" onClick={onClose} disabled={busy}>
          关闭
        </Button>
      }
    >
      <Typography.Text strong>{runTitle(confirm.spec)}</Typography.Text>
      <ul style={{ paddingLeft: 20, margin: "8px 0 16px" }}>
        {confirm.assumptions.map((item, index) => (
          <li key={`${index}-${item.text}`} style={{ marginBottom: 4 }}>
            {item.text}
            {item.default && (
              <Tag color="orange" style={{ marginLeft: 8 }}>
                默认值，可修改
              </Tag>
            )}
          </li>
        ))}
      </ul>
      {problem && (
        <Alert
          style={{ marginBottom: 12 }}
          type={problem.type}
          showIcon
          title={problem.title}
          description={problem.lines.length ? problem.lines.map((line) => <div key={line}>{line}</div>) : undefined}
        />
      )}
      <Space.Compact style={{ width: "100%", marginBottom: 12 }}>
        <Input
          value={change}
          disabled={busy}
          maxLength={200}
          placeholder="修改条件"
          onChange={(event) => setChange(event.target.value)}
          onPressEnter={submitChange}
        />
        <Button loading={revising} disabled={running || !change.trim()} onClick={submitChange}>
          改
        </Button>
      </Space.Compact>
      <Space>
        <Button type="primary" loading={running} disabled={revising} onClick={run}>
          确认并运行
        </Button>
        <Button onClick={onEdit} disabled={busy}>
          打开表单
        </Button>
        {running && <Typography.Text type="secondary">正在计算，个股回看要几秒</Typography.Text>}
      </Space>
      {revisingSince !== null && <Thinking startedAt={revisingSince} what="你要改的地方" />}
    </Card>
  );
}
