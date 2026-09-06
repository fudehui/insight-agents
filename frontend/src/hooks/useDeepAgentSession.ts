import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  cancelTask,
  deleteSession,
  getSessionEvents,
  listSessionFiles,
  listSessions,
  startTask,
  uploadSessionFiles
} from "../lib/api";
import { WS_BASE_URL } from "../lib/config";
import { createThreadId, getStoredThreadId, storeThreadId } from "../lib/thread";
import type {
  ConnectionState,
  MonitorMessage,
  OutputFile,
  SessionSummary,
  SocketMessage,
  UploadedItem
} from "../types";

const MAX_EVENTS = 120;

// 历史回放使用远大于实时流的窗口：task_start/task_files/task_sources 等
// 关键事件分布在整个会话中，窗口过小会截掉早期轮次及其产物与来源归属
const HISTORY_MAX_EVENTS = 2000;

// 终态事件：历史末尾没有它，说明刷新时任务仍在后端执行，需要恢复"研究中"状态
const TERMINAL_EVENTS = ["task_result", "task_cancelled", "error"];

function extractString(data: Record<string, unknown>, key: string): string | null {
  const value = data[key];
  return typeof value === "string" ? value : null;
}

export function useDeepAgentSession() {
  const socketRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<number | undefined>(undefined);
  const heartbeatTimerRef = useRef<number | undefined>(undefined);
  const uploadedNameSetRef = useRef<Set<string>>(new Set());
  const [threadId, setThreadId] = useState(getStoredThreadId);
  const [connectionState, setConnectionState] = useState<ConnectionState>("connecting");
  const [events, setEvents] = useState<MonitorMessage[]>([]);
  const [files, setFiles] = useState<OutputFile[]>([]);
  const [sessionPath, setSessionPath] = useState("");
  const [result, setResult] = useState("");
  const [lastError, setLastError] = useState("");
  const [lastPongAt, setLastPongAt] = useState("");
  const [isRunning, setIsRunning] = useState(false);
  const [isCancelling, setIsCancelling] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const [uploadedItems, setUploadedItems] = useState<UploadedItem[]>([]);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  // 历史加载完成后递增；App 层只依赖它触发一次 turns 重建，避免与实时事件流互相覆盖
  const [historyVersion, setHistoryVersion] = useState(0);

  const clearSocketTimers = useCallback(() => {
    if (reconnectTimerRef.current) {
      window.clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = undefined;
    }
    if (heartbeatTimerRef.current) {
      window.clearInterval(heartbeatTimerRef.current);
      heartbeatTimerRef.current = undefined;
    }
  }, []);

  const resetSession = useCallback(() => {
    const nextThreadId = createThreadId();
    storeThreadId(nextThreadId);
    setThreadId(nextThreadId);
    setEvents([]);
    setFiles([]);
    setSessionPath("");
    setResult("");
    setLastError("");
    setUploadedItems([]);
    uploadedNameSetRef.current.clear();
    setIsRunning(false);
    setIsCancelling(false);
    // 空会话不进历史列表：只有后端真正记录过事件（发生过任务）的会话才有条目，
    // 避免"新建研究"点击即在侧栏堆积"0 个文件"的空条目；任务启动后由 refreshSessions 拉回
    setSessions((previous) =>
      previous.filter((item) => item.thread_id !== nextThreadId)
    );
  }, []);

  const refreshSessions = useCallback(async () => {
    try {
      const response = await listSessions();
      if (response.error) {
        return;
      }
      setSessions(response.sessions || []);
    } catch {
      // 后端未启动时静默降级：会话列表为空即可，不打断主流程
    }
  }, []);

  const loadSessionHistory = useCallback(async (targetThreadId: string) => {
    try {
      const response = await getSessionEvents(targetThreadId);
      if (response.error) {
        return;
      }
      const history = response.events || [];

      // 从历史事件中恢复会话目录，文件面板才能重新列出旧会话产物
      const sessionCreated = history.find(
        (item) => item.event === "session_created"
      );
      const sessionPathFromHistory =
        sessionCreated && typeof sessionCreated.data.path === "string"
          ? sessionCreated.data.path
          : "";
      const lastResult = [...history]
        .reverse()
        .find((item) => item.event === "task_result");
      const resultFromHistory =
        lastResult && typeof lastResult.data.result === "string"
          ? lastResult.data.result
          : "";
      // 末尾没有终态事件 => 后端任务大概率仍在执行，恢复运行态让实时事件继续追上。
      // task_files / task_sources 是任务收尾时在终态事件之后补发的产物与来源清单，
      // 判断时跳过它们，否则已结束的会话会被误判为"研究中"
      const lastMeaningfulEvent = [...history]
        .reverse()
        .find(
          (item) =>
            item.event !== "task_files" && item.event !== "task_sources"
        );
      const isUnfinished = Boolean(
        lastMeaningfulEvent && !TERMINAL_EVENTS.includes(lastMeaningfulEvent.event)
      );

      setEvents(history.slice(-HISTORY_MAX_EVENTS));
      // 只在确实解析到路径时写入：无条件写入会在 WebSocket 重连补发的
      // session_created 已恢复路径之后，把路径覆写成空串，导致文件轮询
      // 永不启动、输出文件面板始终为空
      if (sessionPathFromHistory) {
        setSessionPath(sessionPathFromHistory);
      }
      setResult(resultFromHistory);
      setIsRunning(isUnfinished);
      setIsCancelling(false);
      setHistoryVersion((version) => version + 1);
    } catch {
      // 无历史（新会话）或后端不可达时保持空白界面
    }
  }, []);

  const switchSession = useCallback(
    (targetThreadId: string) => {
      if (!targetThreadId || targetThreadId === threadId) {
        return;
      }
      storeThreadId(targetThreadId);
      setThreadId(targetThreadId);
      setEvents([]);
      setFiles([]);
      setSessionPath("");
      setResult("");
      setLastError("");
      setUploadedItems([]);
      uploadedNameSetRef.current.clear();
      setIsRunning(false);
      setIsCancelling(false);
      void loadSessionHistory(targetThreadId);
    },
    [loadSessionHistory, threadId]
  );

  // 删除会话：后端会先取消该会话运行中的任务，再清理 output/updated 目录。
  // 删除的是当前会话时切换到全新 thread_id（界面回到空白新对话状态）
  const removeSession = useCallback(
    async (targetThreadId: string) => {
      const response = await deleteSession(targetThreadId);

      // 无论删除的是否是当前会话，侧栏条目都要立即移除；
      // 只删当前会话时此前不会过滤，导致已删条目残留到下次刷新
      setSessions((previous) =>
        previous.filter((item) => item.thread_id !== targetThreadId)
      );
      if (targetThreadId === threadId) {
        resetSession();
      }
      return response;
    },
    [resetSession, threadId]
  );

  const refreshFiles = useCallback(async () => {
    if (!sessionPath) {
      return;
    }

    const response = await listSessionFiles(sessionPath);
    if (response.error) {
      throw new Error(response.error);
    }
    setFiles(response.files || []);
  }, [sessionPath]);

  // 挂载时拉取会话列表，并尝试恢复 localStorage 中上次会话的历史对话
  useEffect(() => {
    void refreshSessions();
    void loadSessionHistory(getStoredThreadId());
    // 仅在挂载时执行一次
  }, [refreshSessions, loadSessionHistory]);

  useEffect(() => {
    let disposed = false;

    function connect() {
      clearSocketTimers();
      const hadSocket = Boolean(socketRef.current);
      socketRef.current?.close();
      setConnectionState(hadSocket ? "reconnecting" : "connecting");

      const socket = new WebSocket(`${WS_BASE_URL}/ws/${encodeURIComponent(threadId)}`);
      socketRef.current = socket;

      socket.onopen = () => {
        if (disposed) {
          return;
        }
        setConnectionState("connected");
        setLastError("");
        heartbeatTimerRef.current = window.setInterval(() => {
          if (socket.readyState === WebSocket.OPEN) {
            socket.send("ping");
          }
        }, 25000);
      };

      socket.onmessage = (event) => {
        if (socketRef.current !== socket) {
          return;
        }
        try {
          const payload = JSON.parse(event.data) as SocketMessage;
          if (payload.type === "pong") {
            setLastPongAt(new Date().toISOString());
            return;
          }

          if (payload.type !== "monitor_event") {
            return;
          }

          setEvents((previous) => [...previous, payload].slice(-MAX_EVENTS));

          if (payload.event === "session_created") {
            const path = extractString(payload.data, "path");
            if (path) {
              setSessionPath(path);
            }
          }

      if (payload.event === "task_result") {
        const finalResult = extractString(payload.data, "result");
        setResult(finalResult || payload.message);
        setIsRunning(false);
        setIsCancelling(false);
        // 任务结束后会话摘要（标题/时间/文件数）已变化，刷新侧边栏列表
        void refreshSessions();
      }

          if (payload.event === "task_cancelled") {
            setResult((previous) => previous || payload.message);
            setIsRunning(false);
            setIsCancelling(false);
          }

          if (payload.event === "error") {
            setLastError(payload.message);
            setIsRunning(false);
            setIsCancelling(false);
          }
        } catch (error) {
          setLastError(error instanceof Error ? error.message : "WebSocket 消息解析失败");
        }
      };

      socket.onerror = () => {
        if (!disposed && socketRef.current === socket) {
          setLastError("WebSocket 连接异常，请确认后端服务已启动");
        }
      };

      socket.onclose = () => {
        if (socketRef.current !== socket) {
          return;
        }
        clearSocketTimers();
        if (disposed) {
          setConnectionState("closed");
          return;
        }
        setConnectionState("reconnecting");
        reconnectTimerRef.current = window.setTimeout(connect, 2000);
      };
    }

    connect();

    return () => {
      disposed = true;
      clearSocketTimers();
      socketRef.current?.close();
    };
  }, [clearSocketTimers, threadId]);

  useEffect(() => {
    if (!sessionPath) {
      return;
    }

    refreshFiles().catch((error: unknown) => {
      setLastError(error instanceof Error ? error.message : "文件列表刷新失败");
    });

    const timer = window.setInterval(() => {
      refreshFiles().catch((error: unknown) => {
        setLastError(error instanceof Error ? error.message : "文件列表刷新失败");
      });
    }, isRunning ? 2500 : 6000);

    return () => window.clearInterval(timer);
  }, [isRunning, refreshFiles, sessionPath]);

  const submitTask = useCallback(
    async (query: string) => {
      const cleanQuery = query.trim();
      if (!cleanQuery) {
        throw new Error("请输入研究任务");
      }

      setIsRunning(true);
      setIsCancelling(false);
      setEvents([]);
      setResult("");
      setLastError("");
      try {
        const response = await startTask(cleanQuery, threadId);
        if (response.thread_id && response.thread_id !== threadId) {
          storeThreadId(response.thread_id);
          setThreadId(response.thread_id);
        }
        return response;
      } catch (error) {
        setIsRunning(false);
        setIsCancelling(false);
        throw error;
      }
    },
    [threadId]
  );

  const cancelCurrentTask = useCallback(async () => {
    if (!isRunning) {
      throw new Error("当前没有正在执行的任务");
    }

    setIsCancelling(true);
    setLastError("");
    try {
      const response = await cancelTask(threadId);
      if (response.status === "cancelled") {
        setIsRunning(false);
        setIsCancelling(false);
        setResult((previous) => previous || "任务已取消");
      }
      return response;
    } catch (error) {
      setIsCancelling(false);
      throw error;
    }
  }, [isRunning, threadId]);

  const uploadFiles = useCallback(
    async (items: UploadedItem[]) => {
      if (items.length === 0) {
        throw new Error("请选择要上传的文件");
      }

      const nextItems = items.filter((item) => !uploadedNameSetRef.current.has(item.name));

      if (nextItems.length === 0) {
        return {
          status: "uploaded",
          files: Array.from(uploadedNameSetRef.current)
        };
      }

      setIsUploading(true);
      setLastError("");
      try {
        const response = await uploadSessionFiles(
          nextItems.map((item) => item.raw),
          threadId
        );
        setUploadedItems((previous) => {
          const names = new Set(previous.map((item) => item.name));
          const next = [...previous];
          nextItems.forEach((item) => {
            if (!names.has(item.name)) {
              names.add(item.name);
              uploadedNameSetRef.current.add(item.name);
              next.push(item);
            }
          });
          return next;
        });
        return response;
      } finally {
        setIsUploading(false);
      }
    },
    [threadId]
  );

  const stats = useMemo(() => {
    const toolEvents = events.filter((event) => event.event === "tool_start").length;
    const assistantEvents = events.filter((event) => event.event === "assistant_call").length;
    const errorEvents = events.filter((event) => event.event === "error").length;

    return {
      toolEvents,
      assistantEvents,
      errorEvents,
      fileCount: files.length
    };
  }, [events, files.length]);

  return {
    connectionState,
    events,
    files,
    historyVersion,
    isCancelling,
    isRunning,
    isUploading,
    lastError,
    lastPongAt,
    refreshFiles,
    refreshSessions,
    removeSession,
    resetSession,
    result,
    sessionPath,
    sessions,
    stats,
    cancelCurrentTask,
    loadSessionHistory,
    submitTask,
    switchSession,
    threadId,
    uploadFiles,
    uploadedItems
  };
}
