import { Checkbox, Col, DatePicker, Form, InputNumber, Row, Select } from "antd";
import dayjs from "dayjs";
import { useEffect, useMemo } from "react";

import { useCatalog, useStatus } from "../context";
import { BASE_LABELS, excludeLabel } from "../format";
import {
  DEFAULT_EXCLUDE,
  type FieldName,
  type StockListValues,
  buildStockList,
  disabledDay,
  stockListValues,
} from "../specForm";
import type { StockListSpec } from "../types";
import { CONDITION_FIELDS, ConditionFields, type FormProps, SubmitButtons } from "./formParts";
import { useChecker } from "./useChecker";

const FIELDS: FieldName[] = [
  ...CONDITION_FIELDS,
  ["universe", "base"],
  ["universe", "industry"],
  ["universe", "board"],
  ["universe", "exclude"],
];

export function StockListForm({ initial, planId, issues, onChecked, onCancel }: FormProps<StockListSpec>) {
  const [form] = Form.useForm<StockListValues>();
  const status = useStatus().data?.status;
  const catalog = useCatalog();
  const checker = useChecker(form, FIELDS, { initial, planId, issues, onChecked });
  const initialValues = useMemo(() => stockListValues(initial ?? {}), [initial]);
  const last = status?.data_through;

  useEffect(() => {
    if (last && !form.getFieldValue("as_of")) form.setFieldValue("as_of", dayjs(last));
  }, [last, form]);

  return (
    <Form<StockListValues>
      form={form}
      layout="vertical"
      initialValues={initialValues}
      onFinish={(values) => checker.submit(buildStockList(values))}
    >
      {checker.alert}
      <Row gutter={12}>
        <Col span={6}>
          <Form.Item label="日期" name="as_of" rules={[{ required: true, message: "选一个交易日" }]}>
            <DatePicker style={{ width: "100%" }} disabledDate={disabledDay(status?.history_from, last)} />
          </Form.Item>
        </Col>
        <Col span={6}>
          <Form.Item label="取前几名" name="limit" rules={[{ required: true, message: "填 1~500" }]}>
            <InputNumber min={1} max={500} precision={0} style={{ width: "100%" }} />
          </Form.Item>
        </Col>
      </Row>
      <ConditionFields target="stock" />
      <Row gutter={12}>
        <Col span={5}>
          <Form.Item label="股票池" name={["universe", "base"]}>
            <Select options={Object.entries(BASE_LABELS).map(([value, label]) => ({ value, label }))} />
          </Form.Item>
        </Col>
        <Col span={5}>
          <Form.Item label="申万行业（可选）" name={["universe", "industry"]}>
            <Select
              allowClear
              showSearch
              placeholder="全部行业"
              options={(catalog.sw?.boards ?? []).map((board) => ({ value: board.name, label: board.name }))}
            />
          </Form.Item>
        </Col>
        <Col span={6}>
          <Form.Item
            label="概念板块（可选）"
            name={["universe", "board"]}
            extra={catalog.conceptError ?? "概念板块只有最近一次的成分：查以前的日子会按今天的名单算"}
          >
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder={catalog.concept ? "全部" : "不可用"}
              disabled={!catalog.concept}
              options={(catalog.concept?.boards ?? []).map((board) => ({
                value: board.code,
                label: board.name,
              }))}
            />
          </Form.Item>
        </Col>
        <Col span={8}>
          <Form.Item label="剔除" name={["universe", "exclude"]}>
            <Checkbox.Group
              options={DEFAULT_EXCLUDE.map((value) => ({ value, label: excludeLabel(value) }))}
            />
          </Form.Item>
        </Col>
      </Row>
      <SubmitButtons running={checker.running} ready={status?.ready} onCancel={onCancel} />
    </Form>
  );
}
