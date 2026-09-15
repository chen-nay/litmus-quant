import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// 开发时把 /api 转给本机的后端（uv run python -m litmus serve）：页面和接口同源，不用配跨域
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
  test: { include: ["src/**/*.test.ts"] },
});
