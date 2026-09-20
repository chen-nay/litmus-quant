import { Layout, Menu } from "antd";
import { Link, Route, Routes, useLocation } from "react-router-dom";

import { StatusBar } from "./components/StatusBar";
import { CatalogProvider, StatusProvider } from "./context";
import { QueryPage } from "./pages/QueryPage";
import { RunPage } from "./pages/RunPage";
import { SyncPage } from "./pages/SyncPage";

export function App() {
  const { pathname } = useLocation();
  const selected = pathname.startsWith("/sync") ? "sync" : "query";

  return (
    <StatusProvider>
      <CatalogProvider>
        <Layout style={{ minHeight: "100vh" }}>
          <Layout.Header style={{ display: "flex", alignItems: "center", gap: 32 }}>
            <Link to="/" style={{ color: "#fff", fontSize: 18, fontWeight: 600 }}>
              Litmus 石蕊
            </Link>
            <Menu
              theme="dark"
              mode="horizontal"
              selectedKeys={[selected]}
              style={{ flex: 1, minWidth: 0 }}
              items={[
                { key: "query", label: <Link to="/">查询</Link> },
                { key: "sync", label: <Link to="/sync">数据同步</Link> },
              ]}
            />
          </Layout.Header>
          <Layout.Content
            style={{ padding: "16px 24px", maxWidth: 1400, width: "100%", margin: "0 auto" }}
          >
            <StatusBar />
            <Routes>
              <Route path="/sync" element={<SyncPage />} />
              <Route path="/runs/:runId" element={<RunPage />} />
              <Route path="*" element={<QueryPage />} />
            </Routes>
          </Layout.Content>
        </Layout>
      </CatalogProvider>
    </StatusProvider>
  );
}
