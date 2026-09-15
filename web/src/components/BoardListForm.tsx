import { Alert, Col, DatePicker, Form, InputNumber, Radio, Row } from "antd";
import dayjs from "dayjs";
import { useEffect, useMemo } from "react";

import { useCatalog, useStatus } from "../context";
import { BOARD_TYPE_LABELS } from "../format";
import {
  type BoardListValues,
  type FieldName,
  boardListValues,
  buildBoardList,
  disabledDay,
} from "../specForm";
import type { BoardListSpec, BoardType } from "../types";
import { CONDITION_FIELDS, ConditionFields, type FormProps, SubmitButtons } from "./formParts";
import { useChecker } from "./useChecker";

const FIELDS: FieldName[] = [...CONDITION_FIELDS, ["board_type"]];

export function BoardListForm({ initial, planId, issues, onChecked, onCancel }: FormProps<BoardListSpec>) {
  const [form] = Form.useForm<BoardListValues>();
  const initialValues = useMemo(() => boardListValues(initial ?? {}), [initial]);
  const boardType: BoardType = Form.useWatch("board_type", form) ?? initialValues.board_type ?? "sw_industry";
  const catalog = useCatalog();
  const status = useStatus().data?.status;
  const checker = useChecker(form, FIELDS, { initial, planId, issues, onChecked });
  const boards = boardType === "concept" ? catalog.concept : catalog.sw;
  const range = boards?.range;

  useEffect(() => {
    if (range && !form.getFieldValue("as_of")) form.setFieldValue("as_of", dayjs(range[1]));
  }, [range, form]);

  // §2.7：页面上必须标明用的是哪种口径、数据从哪天起
  const scope = range
    ? `${BOARD_TYPE_LABELS[boardType]}：数据从 ${range[0]} 到 ${range[1]}，共 ${boards?.boards.length} 个${
        boardType === "concept" ? "。只含现在还在的板块，已经撤销的不在里面" : ""
      }`
    : `${BOARD_TYPE_LABELS[boardType]}：${boardType === "concept" ? (catalog.conceptError ?? "加载中") : "加载中"}`;

  return (
    <Form<BoardListValues>
      form={form}
      layout="vertical"
      initialValues={initialValues}
      onFinish={(values) => checker.submit(buildBoardList(values))}
    >
      {checker.alert}
      <Form.Item label="板块口径" name="board_type">
        <Radio.Group
          optionType="button"
          options={[
            { value: "sw_industry", label: BOARD_TYPE_LABELS.sw_industry },
            { value: "concept", label: BOARD_TYPE_LABELS.concept, disabled: !catalog.concept },
          ]}
        />
      </Form.Item>
      <Alert type="info" showIcon style={{ marginBottom: 16 }} title={scope} />
      <Row gutter={12}>
        <Col span={6}>
          <Form.Item label="日期" name="as_of" rules={[{ required: true, message: "选一个交易日" }]}>
            <DatePicker style={{ width: "100%" }} disabledDate={disabledDay(range?.[0], range?.[1])} />
          </Form.Item>
        </Col>
        <Col span={6}>
          <Form.Item label="取前几名" name="limit" rules={[{ required: true, message: "填 1~500" }]}>
            <InputNumber min={1} max={500} precision={0} style={{ width: "100%" }} />
          </Form.Item>
        </Col>
      </Row>
      <ConditionFields target={boardType} />
      <SubmitButtons running={checker.running} ready={status?.ready} onCancel={onCancel} />
    </Form>
  );
}
