/** 提问之后除了确认卡以外的几种回答：追问、选候选、条件要改、回答不了、没能理解、数据不够（ARCHITECTURE §5.2）。 */

import { Alert, Button, Card, Input, Radio, Space, Typography } from "antd";
import { useState } from "react";
import { Link } from "react-router-dom";

import { composeAnswer } from "../planFlow";
import type { Candidate, PlanResponse } from "../types";
import { Suggestions } from "./askParts";

interface Props {
  response: PlanResponse;
  busy: boolean;
  /** 没能理解时给用户点的示例问句 */
  examples: string[];
  onAnswer: (text: string) => void;
  onPick: (kind: "stock" | "board", candidate: Candidate) => void;
  onAsk: (text: string) => void;
  onEdit: () => void;
}

export function PlanOutcome({ response, busy, examples, onAnswer, onPick, onAsk, onEdit }: Props) {
  const [chosen, setChosen] = useState<Record<number, string>>({});
  const [extra, setExtra] = useState("");
  const { status } = response;

  if (status === "data_not_ready") {
    return (
      <Alert
        type="warning"
        showIcon
        title={`本地数据还不够：${response.message ?? ""}`}
        action={<Link to="/sync">去同步</Link>}
      />
    );
  }
  if (status === "failed") {
    return (
      <Card size="small">
        <Alert type="error" showIcon title={response.message ?? "没能理解这个问题"} />
        <Suggestions title="换个问法试试：" items={examples} disabled={busy} onAsk={onAsk} />
      </Card>
    );
  }
  if (status === "unsupported" || status === "not_an_event") {
    const items = response.alternatives.length ? response.alternatives : examples;
    return (
      <Card size="small" title={status === "not_an_event" ? "这个条件不是事件" : "这个问题回答不了"}>
        <Alert type="info" showIcon title={response.message ?? ""} />
        <Suggestions title="可以这样问：" items={items} disabled={busy} onAsk={onAsk} />
      </Card>
    );
  }

  const candidates = response.stock_candidates.length ? response.stock_candidates : response.board_candidates;
  if (candidates.length) {
    const kind = response.stock_candidates.length ? "stock" : "board";
    return (
      <Card size="small" title={response.message ?? "选一个"}>
        <Space wrap>
          {candidates.map((candidate) => (
            <Button key={candidate.code} disabled={busy} onClick={() => onPick(kind, candidate)}>
              {candidate.name}（{candidate.code}）
              {candidate.note && <Typography.Text type="secondary"> · {candidate.note}</Typography.Text>}
            </Button>
          ))}
        </Space>
      </Card>
    );
  }

  if (response.questions.length) {
    const answer = composeAnswer(response.questions, chosen, extra);
    return (
      <Card size="small" title="还要问清楚几件事">
        {response.questions.map((question, index) => (
          <div key={question.question} style={{ marginBottom: 12 }}>
            <div style={{ marginBottom: 6 }}>{question.question}</div>
            <Radio.Group
              optionType="button"
              value={chosen[index]}
              onChange={(event) => setChosen({ ...chosen, [index]: event.target.value })}
              options={question.options.map((option) => ({ value: option, label: option }))}
            />
          </div>
        ))}
        <Input
          style={{ marginBottom: 12 }}
          placeholder="也可以直接补充一句"
          value={extra}
          onChange={(event) => setExtra(event.target.value)}
        />
        <Button type="primary" disabled={!answer} loading={busy} onClick={() => onAnswer(answer)}>
          回答
        </Button>
      </Card>
    );
  }

  return (
    <Card size="small">
      <Alert type="warning" showIcon title={response.message ?? "条件要改一下"} />
      <Button style={{ marginTop: 12 }} disabled={busy} onClick={onEdit}>
        打开表单修改
      </Button>
    </Card>
  );
}
