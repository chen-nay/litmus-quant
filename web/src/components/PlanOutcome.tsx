/** 提问之后除了确认卡和卡以外的几种回答：追问、选候选、条件要改、回答不了、没能理解、数据不够（ARCHITECTURE §5.2）。 */

import { Alert, Button, Card, Input, Radio, Space, Typography } from "antd";
import { useState } from "react";
import { Link } from "react-router-dom";

import { composeAnswer, kindOf } from "../planFlow";
import type { Candidate, PlanResponse } from "../types";
import { Suggestions } from "./askParts";

interface Props {
  response: PlanResponse;
  busy: boolean;
  /** 没能理解时给用户点的示例问句 */
  examples: string[];
  onAnswer: (text: string) => void;
  /** 每组候选选中的那个，和 response.choices 一一对应 */
  onPick: (picked: Candidate[]) => void;
  onAsk: (text: string) => void;
  onEdit: () => void;
}

function candidateLabel(candidate: Candidate) {
  return (
    <>
      {candidate.name}（{candidate.code}）
      {candidate.note && <Typography.Text type="secondary"> · {candidate.note}</Typography.Text>}
    </>
  );
}

export function PlanOutcome({ response, busy, examples, onAnswer, onPick, onAsk, onEdit }: Props) {
  const [chosen, setChosen] = useState<Record<number, string>>({});
  const [picked, setPicked] = useState<Record<number, Candidate>>({});
  const [extra, setExtra] = useState("");
  const { status, choices } = response;

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

  // 只有一组候选：点一下就往下走；几组的话每组选一个，都选完了点「继续」
  if (choices.length === 1) {
    return (
      <Card size="small" title={choices[0].message}>
        <Space wrap>
          {choices[0].candidates.map((candidate) => (
            <Button key={candidate.code} disabled={busy} onClick={() => onPick([candidate])}>
              {candidateLabel(candidate)}
            </Button>
          ))}
        </Space>
      </Card>
    );
  }
  if (choices.length > 1) {
    const done = choices.every((_, index) => picked[index]);
    return (
      <Card size="small" title={response.message ?? "各选一个"}>
        {choices.map((choice, index) => (
          <div key={`${choice.slot}-${choice.mention}`} style={{ marginBottom: 12 }}>
            <div style={{ marginBottom: 6 }}>{choice.message}</div>
            <Radio.Group
              value={picked[index]?.code}
              onChange={(event) => {
                const candidate = choice.candidates.find((item) => item.code === event.target.value);
                if (candidate) setPicked({ ...picked, [index]: candidate });
              }}
            >
              <Space wrap>
                {choice.candidates.map((candidate) => (
                  <Radio.Button key={candidate.code} value={candidate.code}>
                    {candidateLabel(candidate)}
                  </Radio.Button>
                ))}
              </Space>
            </Radio.Group>
          </div>
        ))}
        <Button
          type="primary"
          disabled={!done || busy}
          loading={busy}
          onClick={() => onPick(choices.map((_, index) => picked[index]))}
        >
          继续
        </Button>
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

  // 条件没通过检查：表和统计可以打开表单改；卡没有表单，换个说法再问
  const editable = kindOf(response.spec) === "table" || kindOf(response.spec) === "event_study";
  return (
    <Card size="small">
      <Alert type="warning" showIcon title={response.message ?? "条件要改一下"} />
      {editable ? (
        <Button style={{ marginTop: 12 }} disabled={busy} onClick={onEdit}>
          打开表单修改
        </Button>
      ) : (
        <Typography.Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0 }}>
          换个说法再问一次
        </Typography.Paragraph>
      )}
    </Card>
  );
}
