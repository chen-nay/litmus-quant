/** 同步页：本地数据状态、开始 / 停止同步、同步进度（ARCHITECTURE §2.5）。 */

import {
  Alert,
  App as AntApp,
  Button,
  Card,
  Descriptions,
  Popconfirm,
  Progress,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
} from "antd";
import { useState } from "react";

import { api, errorText } from "../api";
import { useStatus } from "../context";
import { SYNC_STATE_LABELS, formatTime, stepLabel } from "../format";
import type { SyncState } from "../types";

const STATE_COLORS: Record<SyncState, string> = {
  idle: "default",
  running: "processing",
  stopping: "warning",
  stopped: "default",
  failed: "error",
  done: "success",
};

export function SyncPage() {
  const { data, error, refresh } = useStatus();
  const { message } = AntApp.useApp();
  const [acting, setActing] = useState(false);

  if (error) return <Alert type="error" showIcon title={error} />;
  if (!data) return <Spin />;

  const { status, sync } = data;
  const syncing = sync.state === "running" || sync.state === "stopping";

  async function act(kind: "start" | "stop") {
    setActing(true);
    try {
      if (kind === "start") {
        const response = await api.startSync();
        if (!response.started) message.info("已经在同步了");
      } else {
        const response = await api.stopSync();
        message.info(response.stopped ? "正在停止：这一步或这个月跑完就停" : "现在没有在同步");
      }
      await refresh();
    } catch (reason) {
      message.error(errorText(reason));
    } finally {
      setActing(false);
    }
  }

  const unavailable = Object.entries(status.unavailable);
  const syncedAt = Object.entries(status.synced_at)
    .map(([name, at]) => ({ name, at }))
    .sort((a, b) => a.name.localeCompare(b.name));

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <Card
        size="small"
        title="同步"
        extra={
          <Space>
            <Popconfirm
              title="开始同步"
              description="会真实调用 Tushare：数据齐全时补最新几天约 1000 次请求、6 分钟左右；第一次同步要一个多小时。"
              okText="开始"
              cancelText="取消"
              disabled={syncing}
              onConfirm={() => act("start")}
            >
              <Button type="primary" disabled={syncing} loading={acting && !syncing}>
                开始同步
              </Button>
            </Popconfirm>
            <Button danger disabled={sync.state !== "running"} onClick={() => act("stop")}>
              停止同步
            </Button>
          </Space>
        }
      >
        <Descriptions
          size="small"
          column={2}
          items={[
            {
              key: "state",
              label: "状态",
              children: <Tag color={STATE_COLORS[sync.state]}>{SYNC_STATE_LABELS[sync.state]}</Tag>,
            },
            { key: "step", label: "正在跑", children: syncing ? stepLabel(sync.step) : "—" },
            {
              key: "done",
              label: "已完成的步骤",
              children: sync.steps_done.map(stepLabel).join("、") || "—",
            },
            {
              key: "failed",
              label: "出错的步骤",
              children: sync.steps_failed.map(stepLabel).join("、") || "—",
            },
            { key: "started", label: "开始时间", children: formatTime(sync.started_at) },
            { key: "finished", label: "结束时间", children: formatTime(sync.finished_at) },
          ]}
        />
        {sync.daily_total > 0 && (
          <div style={{ marginTop: 12 }}>
            <Typography.Text>股票日频：最近落盘的月份 {sync.daily_month}</Typography.Text>
            <Progress
              percent={Math.round((sync.daily_done / sync.daily_total) * 100)}
              format={() => `${sync.daily_done}/${sync.daily_total} 个月`}
            />
          </div>
        )}
        {sync.error && (
          <Alert
            style={{ marginTop: 12 }}
            type="error"
            showIcon
            title="同步出错"
            description={`${sync.error}。已经下载完的不受影响，再点一次开始同步会从断点接着来。`}
          />
        )}
        <Typography.Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0 }}>
          每一步之间、股票日频每个月之间可以停下；已经下载完的不受影响，下次同步从断点接着来。
        </Typography.Paragraph>
      </Card>

      <Card size="small" title="本地数据">
        <Descriptions
          size="small"
          column={1}
          items={[
            {
              key: "ready",
              label: "能否查询",
              children: status.ready ? (
                <Tag color="success">能查询</Tag>
              ) : (
                <Tag color="warning">还不能：{status.reason}</Tag>
              ),
            },
            { key: "through", label: "数据截至", children: status.data_through ?? "还没有数据" },
            {
              key: "history",
              label: "历史已补到",
              children: status.history_from
                ? `${status.history_from}${status.history_done ? "（已补到 2016 年）" : "（仍在补）"}`
                : "还没有数据",
            },
            {
              key: "missing",
              label: "最近两年还缺的月份",
              children: status.unlock_missing.join("、") || "不缺",
            },
            {
              key: "unavailable",
              label: "不可用的数据",
              children: unavailable.length
                ? unavailable.map(([name, reason]) => `${name}：${reason}`).join("；")
                : "无",
            },
          ]}
        />
      </Card>

      <Card size="small" title="各项数据最近同步时间">
        <Table
          size="small"
          rowKey="name"
          pagination={false}
          dataSource={syncedAt}
          columns={[
            { title: "数据", dataIndex: "name" },
            { title: "同步时间", dataIndex: "at", render: (value: string) => formatTime(value) },
          ]}
        />
      </Card>
    </div>
  );
}
