export type ConnectionState = "connecting" | "connected" | "reconnecting" | "closed";

export type MonitorEventName =
  | "session_created"
  | "tool_start"
  | "tool_end"
  | "assistant_call"
  | "task_result"
  | "task_files"
  | "task_sources"
  | "task_cancelled"
  | "error"
  | string;

export interface MonitorMessage {
  type: "monitor_event";
  event: MonitorEventName;
  message: string;
  data: Record<string, unknown>;
  timestamp: string;
  // 会话内单调递增的事件序号：重连对账时用于回放/实时流去重；
  // 旧版本落盘的历史事件没有该字段
  seq?: number;
}

export interface PongMessage {
  type: "pong";
  message: string;
}

export type SocketMessage = MonitorMessage | PongMessage;

export interface TaskResponse {
  status: "started" | string;
  thread_id: string;
}

export interface CancelTaskResponse {
  status: "cancelled" | "cancelling" | string;
  thread_id: string;
  message?: string;
}

export interface UploadResponse {
  status: "uploaded" | string;
  files: string[];
}

export interface OutputFile {
  name: string;
  type: "file" | string;
  path: string;
  size: number;
  mtime: number;
}

// 网络来源：标题 + URL，来自 Tavily 检索结果
export interface WebSource {
  title: string;
  url: string;
}

// RAGFlow 文档来源：文档名 + 页码（页码可能为空）
export interface DocSource {
  doc: string;
  page: string;
}

// task_sources 事件携带的来源清单，结构与后端 source_registry.get_task_sources 一致
export interface SourceCollection {
  web: WebSource[];
  docs: DocSource[];
  sql: string[];
}

export interface FileListResponse {
  files?: OutputFile[];
}

export interface SessionSummary {
  thread_id: string;
  title: string;
  mtime: number;
  file_count: number;
  event_count: number;
}

export interface SessionListResponse {
  sessions?: SessionSummary[];
}

export interface SessionEventsResponse {
  events?: MonitorMessage[];
}

export interface UploadedItem {
  uid: string;
  name: string;
  size: number;
  raw: File;
}
