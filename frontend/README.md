# Insight Agents Frontend

React 19 + Vite + Ant Design 前端，对接 Insight Agents FastAPI 后端。
对话式研究界面：轮次切分、研究过程时间线、回答流式输出、产物预览/下载、
人工审批卡片与会话回放。

## Run

```bash
pnpm install
pnpm dev
```

默认同源访问（dev 下由 Vite 代理 `/api` 与 `/ws` 到 `http://localhost:8000`）。
部署到远程后端或修改端口时，用 `.env.local` 覆盖：

```bash
VITE_API_BASE_URL=http://localhost:8000
VITE_WS_BASE_URL=ws://localhost:8000
```

## Backend Contract

- `POST /api/task`（可选 `approval_mode`：off / standard / strict）
- `POST /api/task/{thread_id}/cancel`
- `POST /api/task/{thread_id}/approval`
- `POST /api/upload`
- `GET /api/files`
- `GET /api/files/content`
- `GET /api/download`
- `POST /api/files/reveal`
- `GET /api/sessions`
- `GET /api/sessions/{thread_id}/events`
- `DELETE /api/sessions/{thread_id}`
- `GET /api/health`
- `WebSocket /ws/{thread_id}`
