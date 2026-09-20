/** 改条件的两张表单（表、统计）共用的零件：分块标题、字段参考、提交按钮。 */

import { Button, Divider, Form, Space, Table, Typography } from "antd";

import { useCatalog } from "../context";
import type { CheckResponse, FieldInfo, Issue, Spec, Target } from "../types";

/** 改条件的表单的参数：从确认卡点「打开表单」进来，带着现在的条件和提问编号 */
export interface FormProps {
  initial: Spec;
  planId?: string | null;
  issues?: Issue[];
  onChecked: (response: CheckResponse) => void;
  onCancel: () => void;
}

/** 表单里的一块：「看谁」「看哪天」…… */
export function Section({ title }: { title: string }) {
  return (
    <Divider titlePlacement="start" style={{ margin: "8px 0 12px" }}>
      <Typography.Text type="secondary">{title}</Typography.Text>
    </Divider>
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
  onCancel: () => void;
}) {
  const blocked = ready === false;
  return (
    <Form.Item style={{ marginBottom: 0, marginTop: 8 }}>
      <Space>
        <Button type="primary" htmlType="submit" loading={running} disabled={blocked}>
          {blocked ? "本地数据还不够，先去同步" : "下一步：确认条件"}
        </Button>
        <Button onClick={onCancel}>取消修改</Button>
      </Space>
    </Form.Item>
  );
}
