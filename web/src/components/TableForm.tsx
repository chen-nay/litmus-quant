/** 改表的条件：看谁（算的范围）/ 看哪天 / 看哪些数 / 怎么出。从确认卡点「打开表单」进来，每一栏都已经填好。 */

import { Button, Checkbox, Col, DatePicker, Form, Input, InputNumber, Row, Select, type SelectProps } from "antd";
import { useMemo } from "react";

import { type Catalog, useCatalog, useStatus } from "../context";
import { BASE_LABELS, BOARD_TYPE_LABELS, excludeLabel } from "../format";
import {
  DEFAULT_EXCLUDE,
  type FieldName,
  type TableValues,
  buildTable,
  disabledDay,
  tableValues,
} from "../specForm";
import type { Target } from "../types";
import { FieldReference, type FormProps, Section, SubmitButtons } from "./formParts";
import { useChecker } from "./useChecker";

const FIELDS: FieldName[] = [
  ["scope", "target"],
  ["scope", "base"],
  ["scope", "industry"],
  ["scope", "board"],
  ["scope", "exclude"],
  ["when", "as_of"],
  ["output", "filter", "expr"],
  ["output", "filter", "label"],
  ["output", "sort", "by"],
  ["output", "sort", "order"],
  ["output", "limit"],
];

/** 申万行业下拉框：一级、二级分组，二级后面写上所属一级 */
function industryOptions(catalog: Catalog): SelectProps["options"] {
  const level1 = (catalog.sw?.boards ?? []).map((board) => ({ value: board.name, label: board.name }));
  const level2 = (catalog.sw2?.boards ?? []).map((board) => ({
    value: board.name,
    label: board.parent ? `${board.name}（${board.parent}）` : board.name,
  }));
  return level2.length
    ? [
        { label: "一级", options: level1 },
        { label: "二级", options: level2 },
      ]
    : level1;
}

export function TableForm({ initial, planId, issues, onChecked, onCancel }: FormProps) {
  const [form] = Form.useForm<TableValues>();
  const status = useStatus().data?.status;
  const catalog = useCatalog();
  const checker = useChecker(form, FIELDS, { initial, planId, issues, onChecked });
  const initialValues = useMemo(() => tableValues(initial), [initial]);
  const target: Target = Form.useWatch(["scope", "target"], form) ?? initial.scope.target;
  const metrics: { name?: string }[] = Form.useWatch("metrics", form) ?? initialValues.metrics;
  const names = metrics.map((metric) => metric?.name?.trim()).filter((name): name is string => !!name);
  const stock = target === "stock";
  const targets: Target[] = [
    "stock",
    "sw_industry",
    ...(catalog.sw2 ? (["sw_industry_l2"] as const) : []),
    ...(catalog.concept ? (["concept"] as const) : []),
  ];

  return (
    <Form<TableValues>
      form={form}
      layout="vertical"
      initialValues={initialValues}
      onFinish={(values) => checker.submit(buildTable(values))}
    >
      {checker.alert}
      <Section title="看谁" />
      <Row gutter={12}>
        <Col span={5}>
          <Form.Item label="排什么" name={["scope", "target"]}>
            <Select
              options={targets.map((value) => ({
                value,
                label: value === "stock" ? "股票" : BOARD_TYPE_LABELS[value],
              }))}
            />
          </Form.Item>
        </Col>
        {stock && (
          <>
            <Col span={5}>
              <Form.Item label="股票池" name={["scope", "base"]}>
                <Select options={Object.entries(BASE_LABELS).map(([value, label]) => ({ value, label }))} />
              </Form.Item>
            </Col>
            <Col span={5}>
              <Form.Item label="申万行业（可选）" name={["scope", "industry"]}>
                <Select allowClear showSearch optionFilterProp="label" placeholder="全部行业" options={industryOptions(catalog)} />
              </Form.Item>
            </Col>
            <Col span={5}>
              <Form.Item
                label="概念板块（可选）"
                name={["scope", "board"]}
                extra={catalog.conceptError ?? "只有最近一次的成分：查以前的日子会按今天的名单算"}
              >
                <Select
                  allowClear
                  showSearch
                  optionFilterProp="label"
                  placeholder={catalog.concept ? "全部" : "不可用"}
                  disabled={!catalog.concept}
                  options={(catalog.concept?.boards ?? []).map((board) => ({ value: board.code, label: board.name }))}
                />
              </Form.Item>
            </Col>
            <Col span={24}>
              <Form.Item label="剔除" name={["scope", "exclude"]}>
                <Checkbox.Group options={DEFAULT_EXCLUDE.map((value) => ({ value, label: excludeLabel(value) }))} />
              </Form.Item>
            </Col>
          </>
        )}
      </Row>

      <Section title="看哪天" />
      <Row gutter={12}>
        <Col span={6}>
          <Form.Item label="日期" name={["when", "as_of"]} rules={[{ required: true, message: "选一个交易日" }]}>
            <DatePicker style={{ width: "100%" }} disabledDate={disabledDay(status?.history_from, status?.data_through)} />
          </Form.Item>
        </Col>
      </Row>

      <Section title="看哪些数" />
      <Form.List name="metrics">
        {(rows, { add, remove }) => (
          <>
            {rows.map((row) => (
              <Row gutter={12} key={row.key} align="top">
                <Col span={6}>
                  <Form.Item name={[row.name, "name"]} rules={[{ required: true, message: "起个名字" }]}>
                    <Input placeholder="名字，比如 今年以来涨幅" />
                  </Form.Item>
                </Col>
                <Col span={16}>
                  <Form.Item name={[row.name, "expr"]} rules={[{ required: true, message: "写公式" }]}>
                    <Input placeholder="公式，比如 PctSince($close, 20251231)" />
                  </Form.Item>
                </Col>
                <Col span={2}>
                  <Button type="link" onClick={() => remove(row.name)}>
                    删掉
                  </Button>
                </Col>
              </Row>
            ))}
            <Button type="dashed" onClick={() => add({ name: "", expr: "" })} style={{ marginBottom: 12 }}>
              + 加一个指标
            </Button>
          </>
        )}
      </Form.List>
      <FieldReference target={target} />

      <Section title="怎么出" />
      <Row gutter={12}>
        <Col span={14}>
          <Form.Item
            label="先筛（可以不填）"
            name={["output", "filter", "expr"]}
            extra="条件是真假：$pct_chg > 9、$market_cap < 30亿；多个条件用 & 连接"
          >
            <Input placeholder="$pct_chg > 9" />
          </Form.Item>
        </Col>
        <Col span={10}>
          <Form.Item label="条件名称（可选）" name={["output", "filter", "label"]}>
            <Input placeholder="涨幅超过 9%" />
          </Form.Item>
        </Col>
        <Col span={8}>
          <Form.Item label="按哪个指标排" name={["output", "sort", "by"]} extra="不选按成交额从高到低">
            <Select allowClear placeholder="成交额" options={names.map((name) => ({ value: name, label: name }))} />
          </Form.Item>
        </Col>
        <Col span={5}>
          <Form.Item label="顺序" name={["output", "sort", "order"]}>
            <Select
              options={[
                { value: "desc", label: "从高到低" },
                { value: "asc", label: "从低到高" },
              ]}
            />
          </Form.Item>
        </Col>
        <Col span={5}>
          <Form.Item label="取前几个" name={["output", "limit"]} rules={[{ required: true, message: "填 1~500" }]}>
            <InputNumber min={1} max={500} precision={0} style={{ width: "100%" }} />
          </Form.Item>
        </Col>
      </Row>
      <SubmitButtons running={checker.running} ready={status?.ready} onCancel={onCancel} />
    </Form>
  );
}
