// 全局常量集中管理：事件窗口、终态判定、连接节奏与上传限制。
// 之前同样的数字散落在 App.tsx / useDeepAgentSession.ts / 各组件里，改一处漏三处。

// 终态事件：出现即代表一轮对话结束，不再接续实时事件
export const TERMINAL_EVENTS = ["task_result", "task_cancelled", "error"];

// 实时事件滑窗上限：防止长任务把内存和渲染拖垮
export const MAX_EVENTS = 120;

// 历史回放窗口上限：关键事件分布在整个会话中，窗口过小会截掉早期轮次
// 及其产物与来源归属
export const HISTORY_MAX_EVENTS = 2000;

// WebSocket 重连：指数退避基数与上限（毫秒），实际延迟再乘随机抖动
export const RECONNECT_BASE_MS = 1000;
export const RECONNECT_MAX_MS = 30000;

// 心跳节奏（毫秒）：按此间隔发送 ping；超过两个周期没收到任何服务端消息
// 判定为半开连接，主动断开触发重连
export const HEARTBEAT_INTERVAL_MS = 25000;
export const HEARTBEAT_TIMEOUT_MS = 50000;

// 文件列表轮询节奏（毫秒）：运行中加快以跟进新产物，空闲放宽降低开销
export const POLL_ACTIVE_MS = 2500;
export const POLL_IDLE_MS = 6000;

// 聊天流贴底判定阈值（像素）：距底部小于该值视为"贴着底部"
export const SCROLL_STICK_THRESHOLD_PX = 60;

// 上传限制（与后端 /api/upload 的校验保持一致）
export const UPLOAD_ALLOWED_EXTENSIONS = [
  ".md",
  ".txt",
  ".docx",
  ".pdf",
  ".xlsx",
  ".xls",
  ".csv"
];
export const UPLOAD_MAX_FILE_SIZE_MB = 50;
export const UPLOAD_MAX_FILES = 10;
