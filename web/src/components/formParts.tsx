/** 股票表、板块表共用的输入框：筛选、排序、字段参考；三张表单共用的提交按钮和参数。 */

import { Button, Col, Form, Input, Row, Select, Space, Table, Typography } from "antd";

import { useCatalog } from "../context";
import type { FieldName } from "../specForm";
import type { CheckResponse, FieldInfo, Issue, Target } from "../types";

/** 三张表单的参数：不给 initial 就是空白表单；从确认卡点「修改」进来时带着查询条件、提问编号 */
export interface FormProps<S> {
  initial?: Partial<S>;
  planId?: string | null;
  issues?: Issue[];
  onChecked: (response: CheckResponse) => void;
  onCancel?: () => void;
}

export const CONDITION_FIELDS: FieldName[] = [
  ["as_of"],
  ["limit"],
  ["filter", "expr"],
  ["filter", "label"],
  ["sort", "by"],
  ["sort", "order"],
  ["sort", "label"],
];

export function ConditionFields({ target }: { target: Target }) {
  return (
    <>
      <Row gutter={12}>
        <Col span={16}>
          <Form.Item
            label="筛选条件（表达式，可以不填）"
            name={["filter", "expr"]}
            extra="例：$amount > Mean(Ref($amount, 1), 5) * 1.4。多个条件用 & 连接，比较要加括号：($pct_chg > 5) & ($pe_ttm < 30)"
          >
            <Input.TextArea autoSize={{ minRows: 2, maxRows: 6 }} placeholder="$pct_chg > 9" />
          </Form.Item>
        </Col>
        <Col span={8}>
          <Form.Item label="条件名称（可选）" name={["filter", "label"]}>
            <Input placeholder="涨幅超过 9%" />
          </Form.Item>
        </Col>
      </Row>
      <Row gutter={12}>
        <Col span={12}>
          <Form.Item
            label="排序依据（表达式，不填按成交额）"
            name={["sort", "by"]}
            extra="例：$amount / Mean(Ref($amount, 1), 20)"
          >
            <Input placeholder="$pct_chg" />
          </Form.Item>
        </Col>
        <Col span={4}>
          <Form.Item label="顺序" name={["sort", "order"]}>
            <Select
              options={[
                { value: "desc", label: "从高到低" },
                { value: "asc", label: "从低到高" },
              ]}
            />
          </Form.Item>
        </Col>
        <Col span={8}>
          <Form.Item label="排序列名称（可选，显示成表头）" name={["sort", "label"]}>
            <Input placeholder="放大倍数" />
          </Form.Item>
        </Col>
      </Row>
      <FieldReference target={target} />
    </>
  );
}

export function FieldReference({ target }: { target: Target }) {
  const { fields } = useCatalog();
  const rows = fields?.fields[target] ?? [];
  return (
    <details style={{ marginBottom: 16 }}>
      <summary style={{ cursor: "pointer", color: "#1677ff" }}>能用哪些字段（{rows.length} 个）</summary>
      <Table<FieldInfo>
        style={{ marginTop: 8 }}
        size="small"
        rowKey="name"
        pagination={false}
        dataSource={rows}
        columns={[
          {
            title: "写法",
            dataIndex: "name",
            width: 150,
            render: (value: string) => <Typography.Text code>{value}</Typography.Text>,
          },
          { title: "含义", dataIndex: "label", width: 180 },
          { title: "单位", dataIndex: "unit", width: 70 },
          { title: "类型", dataIndex: "type", width: 70 },
          { title: "说明", dataIndex: "note" },
        ]}
      />
      <Typography.Paragraph type="secondary" style={{ marginTop: 8 }}>
        常用算子：Mean(x, n) n 日均值、Ref(x, n) n 天前、Max / Min(x, n) n 日最高 / 最低、Rank(x)
        当天在股票池里的分位、Cross(a, b) a 上穿 b。条件之间用 &（且）、|（或）、~（非）。
      </Typography.Paragraph>
    </details>
  );
}

/** ready 为 undefined 表示状态还没取到，先放行：真不够时接口会返回 data_not_ready */
export function SubmitButtons({
  running,
  ready,
  onCancel,
}: {
  running: boolean;
  ready: boolean | undefined;
  onCancel?: () => void;
}) {
  const blocked = ready === false;
  return (
    <Form.Item style={{ marginBottom: 0 }}>
      <Space>
        <Button type="primary" htmlType="submit" loading={running} disabled={blocked}>
          {blocked ? "本地数据还不够，先去同步" : "下一步：确认条件"}
        </Button>
        {onCancel && <Button onClick={onCancel}>取消修改</Button>}
      </Space>
    </Form.Item>
  );
}
