/** 改统计的条件：看谁（点名一只）/ 什么事件 / 看哪段 / 怎么算。从确认卡点「打开表单」进来，每一栏都已经填好。 */

import { Col, DatePicker, Form, Input, InputNumber, Row, Select, Typography } from "antd";
import type { Dayjs } from "dayjs";
import { useEffect, useMemo, useRef } from "react";

import { useCatalog, useStatus } from "../context";
import { BENCHMARK_LABELS } from "../format";
import { type FieldName, type StudyValues, buildStudy, day, studyValues } from "../specForm";
import { type FormProps, Section, SubmitButtons } from "./formParts";
import { useChecker } from "./useChecker";

const BASE_FIELDS: FieldName[] = [
  ["subject", "codes"],
  ["output", "event", "preset_id"],
  ["when", "range"],
  ["output", "horizons"],
  ["output", "benchmark"],
  ["output", "cost_bps"],
];

const HORIZON_CHOICES = [5, 10, 20, 60, 120, 250];

export function StudyForm({ initial, planId, issues, onChecked, onCancel }: FormProps) {
  const [form] = Form.useForm<StudyValues>();
  const initialValues = useMemo(() => studyValues(initial), [initial]);
  const status = useStatus().data?.status;
  const catalog = useCatalog();
  const events = useMemo(() => catalog.events?.events ?? [], [catalog.events]);
  const presetId: string | undefined = Form.useWatch(["output", "event", "preset_id"], form);
  const event = events.find((item) => item.id === (presetId ?? initialValues.output.event.preset_id));
  const fields = useMemo(
    () => [...BASE_FIELDS, ...(event?.params ?? []).map((param) => ["output", "event", "params", param.name])],
    [event],
  );
  const checker = useChecker(form, fields, { initial, planId, issues, onChecked });
  const first = status?.history_from;
  const last = status?.data_through;

  // 用户换了事件才把参数重置成默认值；带进来的事件保留原来的参数
  const shownPreset = useRef<string | undefined>(initialValues.output.event.preset_id);
  useEffect(() => {
    if (!event) return;
    if (shownPreset.current !== event.id) {
      const defaults = Object.fromEntries(event.params.map((param) => [param.name, param.default]));
      form.setFieldValue(["output", "event", "params"], defaults);
    }
    shownPreset.current = event.id;
  }, [event, form]);

  const groups = [...new Set(events.map((item) => item.category))].map((category) => ({
    label: category,
    options: events.filter((item) => item.category === category).map((item) => ({ value: item.id, label: item.name })),
  }));
  const outside = (current: Dayjs) => {
    const value = day(current);
    return (!!first && value < first) || (!!last && value > last);
  };

  return (
    <Form<StudyValues>
      form={form}
      layout="vertical"
      initialValues={initialValues}
      onFinish={(values) => checker.submit(buildStudy(values, event, initial.scope))}
    >
      {checker.alert}
      <Section title="看谁" />
      <Row gutter={12}>
        <Col span={6}>
          <Form.Item
            label="股票代码"
            name={["subject", "codes"]}
            rules={[{ required: true, message: "填股票代码，如 600519.SH" }]}
            extra="要带交易所后缀：沪市 .SH、深市 .SZ"
          >
            <Input placeholder="600519.SH" />
          </Form.Item>
        </Col>
      </Row>

      <Section title="什么事件" />
      <Row gutter={12}>
        <Col span={6}>
          <Form.Item label="事件" name={["output", "event", "preset_id"]} rules={[{ required: true, message: "选一个事件" }]}>
            <Select showSearch optionFilterProp="label" options={groups} />
          </Form.Item>
        </Col>
        {event?.params.map((param) => (
          <Col span={6} key={param.name}>
            <Form.Item
              label={`${param.label}${param.unit ? `（${param.unit}）` : ""}`}
              name={["output", "event", "params", param.name]}
              extra={`可选 ${param.allowed}，默认 ${param.default}`}
              rules={[{ required: true, message: `填${param.label}` }]}
            >
              {param.kind === "choice" ? (
                <Select options={param.choices.map((choice) => ({ value: choice, label: String(choice) }))} />
              ) : (
                <InputNumber
                  min={param.min ?? undefined}
                  max={param.max ?? undefined}
                  step={param.kind === "int" ? 1 : 0.1}
                  precision={param.kind === "int" ? 0 : undefined}
                  style={{ width: "100%" }}
                />
              )}
            </Form.Item>
          </Col>
        ))}
      </Row>
      {event && event.constraints.length > 0 && (
        <Typography.Paragraph type="secondary">{event.constraints.join("；")}</Typography.Paragraph>
      )}

      <Section title="看哪段" />
      <Row gutter={12}>
        <Col span={8}>
          <Form.Item label="回看区间" name={["when", "range"]} rules={[{ required: true, message: "选回看区间" }]}>
            <DatePicker.RangePicker style={{ width: "100%" }} disabledDate={outside} />
          </Form.Item>
        </Col>
      </Row>

      <Section title="怎么算" />
      <Row gutter={12}>
        <Col span={8}>
          <Form.Item
            label="持有天数（交易日，可多选，也可以直接输入）"
            name={["output", "horizons"]}
            rules={[{ required: true, message: "至少选一档" }]}
          >
            <Select
              mode="tags"
              tokenSeparators={[",", "，", " "]}
              options={HORIZON_CHOICES.map((n) => ({ value: String(n), label: `${n} 天` }))}
            />
          </Form.Item>
        </Col>
        <Col span={6}>
          <Form.Item label="同期对照" name={["output", "benchmark"]}>
            <Select options={Object.entries(BENCHMARK_LABELS).map(([value, label]) => ({ value, label }))} />
          </Form.Item>
        </Col>
        <Col span={5}>
          <Form.Item label="交易成本（基点）" name={["output", "cost_bps"]} extra="30 基点 = 0.3%，买卖双边合计">
            <InputNumber min={0} max={500} style={{ width: "100%" }} />
          </Form.Item>
        </Col>
      </Row>
      <SubmitButtons running={checker.running} ready={status?.ready} onCancel={onCancel} />
    </Form>
  );
}
