import { API_BASE_URL } from "./config";
import { getAccessToken } from "./auth";
import type {
  ApprovalDecisionPayload,
  ApprovalResponse,
  CancelTaskResponse,
  FileListResponse,
  SessionEventsResponse,
  SessionListResponse,
  TaskResponse,
  UploadResponse
} from "../types";

// 带 HTTP 状态码的错误：调用方据此识别 401（需要访问令牌）等场景
export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

function apiUrl(path: string): string {
  return `${API_BASE_URL}${path}`;
}

// 同源部署时 API_BASE_URL 是空串：new URL(相对路径) 缺少 base 会直接抛
// "Invalid URL"——下载按钮在渲染期取 href，一旦抛异常整棵 React 树都会
// 崩溃白屏。带查询参数的接口地址统一用字符串拼接 + URLSearchParams 编码，
// 同源（相对路径）与显式绝对地址两种配置都安全
function apiUrlWithParams(path: string, params: Record<string, string>): string {
  const query = new URLSearchParams(params).toString();
  return query ? `${API_BASE_URL}${path}?${query}` : `${API_BASE_URL}${path}`;
}

async function requestJson<T>(input: RequestInfo | URL, init?: RequestInit): Promise<T> {
  // 配置了访问令牌时自动附带请求头；调用方已给的 header（如 Content-Type）保留
  const headers = new Headers(init?.headers);
  const token = getAccessToken();
  if (token) {
    headers.set("X-Access-Token", token);
  }

  const response = await fetch(input, { ...init, headers });
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json")
    ? await response.json()
    : await response.text();

  if (!response.ok) {
    const message =
      typeof payload === "object" && payload && "detail" in payload
        ? String(payload.detail)
        : `HTTP ${response.status}`;
    throw new ApiError(response.status, message);
  }

  return payload as T;
}

export async function startTask(
  query: string,
  threadId: string,
  approvalMode?: string
): Promise<TaskResponse> {
  return requestJson<TaskResponse>(apiUrl("/api/task"), {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      query,
      thread_id: threadId,
      ...(approvalMode ? { approval_mode: approvalMode } : {})
    })
  });
}

export async function cancelTask(threadId: string): Promise<CancelTaskResponse> {
  return requestJson<CancelTaskResponse>(apiUrl(`/api/task/${encodeURIComponent(threadId)}/cancel`), {
    method: "POST"
  });
}

export async function uploadSessionFiles(
  files: File[],
  threadId: string
): Promise<UploadResponse> {
  const formData = new FormData();
  formData.append("thread_id", threadId);
  files.forEach((file) => formData.append("files", file));

  return requestJson<UploadResponse>(apiUrl("/api/upload"), {
    method: "POST",
    body: formData
  });
}

export async function listSessionFiles(path: string): Promise<FileListResponse> {
  return requestJson<FileListResponse>(apiUrlWithParams("/api/files", { path }));
}

export async function listSessions(): Promise<SessionListResponse> {
  return requestJson<SessionListResponse>(apiUrl("/api/sessions"));
}

export async function getSessionEvents(threadId: string): Promise<SessionEventsResponse> {
  return requestJson<SessionEventsResponse>(
    apiUrl(`/api/sessions/${encodeURIComponent(threadId)}/events`)
  );
}

export interface DeleteSessionResponse {
  status: "deleted" | string;
  thread_id: string;
}

export async function deleteSession(threadId: string): Promise<DeleteSessionResponse> {
  return requestJson<DeleteSessionResponse>(
    apiUrl(`/api/sessions/${encodeURIComponent(threadId)}`),
    { method: "DELETE" }
  );
}

// <a href> / iframe 场景带不了请求头，令牌以查询参数附加
function appendTokenQuery(url: string): string {
  const token = getAccessToken();
  return token ? `${url}&access_token=${encodeURIComponent(token)}` : url;
}

export function getDownloadUrl(path: string): string {
  return appendTokenQuery(apiUrlWithParams("/api/download", { path }));
}

export interface RevealResponse {
  status?: "revealed" | string;
  path?: string;
}

export async function revealInFolder(path: string): Promise<RevealResponse> {
  return requestJson<RevealResponse>(
    apiUrlWithParams("/api/files/reveal", { path }),
    { method: "POST" }
  );
}

export async function submitApproval(
  threadId: string,
  decisions: ApprovalDecisionPayload[]
): Promise<ApprovalResponse> {
  return requestJson<ApprovalResponse>(
    apiUrl(`/api/task/${encodeURIComponent(threadId)}/approval`),
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({ decisions })
    }
  );
}

// 应用内预览：/api/files/content 以 inline 方式返回文件内容，
// Markdown 可 fetch 成文本渲染，PDF 与图片可直接进 iframe/img
export function getFileContentUrl(path: string): string {
  return appendTokenQuery(apiUrlWithParams("/api/files/content", { path }));
}
