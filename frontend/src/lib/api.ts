import { API_BASE_URL } from "./config";
import type {
  CancelTaskResponse,
  FileListResponse,
  SessionEventsResponse,
  SessionListResponse,
  TaskResponse,
  UploadResponse
} from "../types";

function apiUrl(path: string): string {
  return `${API_BASE_URL}${path}`;
}

async function requestJson<T>(input: RequestInfo | URL, init?: RequestInit): Promise<T> {
  const response = await fetch(input, init);
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json")
    ? await response.json()
    : await response.text();

  if (!response.ok) {
    const message =
      typeof payload === "object" && payload && "detail" in payload
        ? String(payload.detail)
        : `HTTP ${response.status}`;
    throw new Error(message);
  }

  return payload as T;
}

export async function startTask(query: string, threadId: string): Promise<TaskResponse> {
  return requestJson<TaskResponse>(apiUrl("/api/task"), {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      query,
      thread_id: threadId
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
  const url = new URL(apiUrl("/api/files"));
  url.searchParams.set("path", path);
  return requestJson<FileListResponse>(url);
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

export function getDownloadUrl(path: string): string {
  const url = new URL(apiUrl("/api/download"));
  url.searchParams.set("path", path);
  return url.toString();
}

export interface RevealResponse {
  status?: "revealed" | string;
  path?: string;
  error?: string;
}

export async function revealInFolder(path: string): Promise<RevealResponse> {
  const url = new URL(apiUrl("/api/files/reveal"));
  url.searchParams.set("path", path);
  return requestJson<RevealResponse>(url, { method: "POST" });
}
