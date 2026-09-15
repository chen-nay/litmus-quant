/** 每页顶部：数据截至哪天、历史补到哪天、同步进度；数据还不够时提示去同步。 */

import { Alert, Typography } from "antd";
import { Link } from "react-router-dom";

import { useStatus } from "../context";
import { stepLabel } from "../format";

export function StatusBar() {
  const { data, error } = useStatus();
  if (error) {
    return <Alert type="error" showIcon title={error} style={{ marginBottom: 16 }} />;
  }
  if (!data) return null;

  const { status, sync } = data;
  const syncing = sync.state === "running" || sync.state === "stopping";
  const progress = syncing
    ? `正在同步：${stepLabel(sync.step)}${
        sync.step === "daily" && sync.daily_total ? ` ${sync.daily_done}/${sync.daily_total} 个月` : ""
      }`
    : "";

  if (!status.ready) {
    return (
      <Alert
        type="warning"
        showIcon
        style={{ marginBottom: 16 }}
        title={`还不能查询：${status.reason}`}
        description={progress || undefined}
        action={<Link to="/sync">去同步</Link>}
      />
    );
  }

  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 24, marginBottom: 16, color: "#666" }}>
      <span>数据截至 {status.data_through}</span>
      <span>
        历史已补到 {status.history_from}
        {status.history_done ? "（已补完）" : "（仍在补）"}
      </span>
      {syncing && <Link to="/sync">{progress}</Link>}
      {sync.state === "failed" && (
        <Link to="/sync">
          <Typography.Text type="danger">上次同步没有全部成功，点这里看原因</Typography.Text>
        </Link>
      )}
    </div>
  );
}
