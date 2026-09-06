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
import {
  HEARTBEAT_INTERVAL_MS,
  HEARTBEAT_TIMEOUT_MS,
  HISTORY_MAX_EVENTS,
  MAX_EVENTS,
  POLL_ACTIVE_MS,
  POLL_IDLE_MS,
  RECONNECT_BASE_MS,
  RECONNECT_MAX_MS,
  TERMINAL_EVENTS
} from "../lib/constants";
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

function extractString(data: Record<string, unknown>, key: string): string | null {
  const value = data[key];
  return typeof value === "string" ? value : null;
}

// 旧版本落盘事件没有 seq 序号时的去重指纹：时间戳 + 事件名 + 消息内容
function eventFingerprint(event: MonitorMessage): string {
  return `${event.timestamp}|${event.event}|${event.message}`;
}

function seqOf(event: MonitorMessage): number | null {
  return typeof event.seq === "number" ? event.seq : null;
}

export function useDeepAgentSession() {
  const socketRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<number | undefined>(undefined);
  const heartbeatTimerRef = useRef<number | undefined>(undefined);
  const uploadedNameSetRef = useRef<Set<string>>(new Set());
  // 连接健康度：连续重连次数（指数退避）与最近一次收到服务端消息的时间（半开检测）
  const reconnectAttemptsRef = useRef(0);
  const lastServerMessageAtRef = useRef(0);
  // 事件去重游标：seq 单调序号（新事件）+ 指纹集合（旧版无 seq 的事件）
  const lastSeqRef = useRef(0);
  const seenFingerprintsRef = useRef<Set<string>>(new Set());
  const [threadId, setThreadId] = useState(getStoredThreadId);
  const [connectionState, setConnectionState] = useState<ConnectionState>("connecting");
  const [events, setEvents] = useState<MonitorMessage[]>([]);
  const [files, setFiles] = useState<OutputFile[]>([]);
  const [sessionPath, setSessionPath] = useState("");
  const [result, setResult] = useState("");
  // 流式回答增量走独立状态而不进 events 数组：高频 delta 会把 MAX_EVENTS
  // 滑窗里的关键事件（tool_start / task_files / task_sources）挤出去
  const [streamingText, setStreamingText] = useState("");
  const [lastError, setLastError] = useState("");
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
    setStreamingText("");
    setLastError("");
    setUploadedItems([]);
    uploadedNameSetRef.current.clear();
    lastSeqRef.current = 0;
    seenFingerprintsRef.current = new Set();
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
      setSessions(response.sessions || []);
    } catch (error) {
      // 会话列表是次要信息，失败不阻断主流程，但要留下可见痕迹；
      // WebSocket 连上时会清除 lastError，后端恢复后提示自动消失
      setLastError(
        error instanceof Error ? `会话列表加载失败: ${error.message}` : "会话列表加载失败"
      );
    }
  }, []);

  const loadSessionHistory = useCallback(async (targetThreadId: string) => {
    try {
      const response = await getSessionEvents(targetThreadId);
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

      // 重建去重游标：seq 事件取历史中的最大序号，旧版无 seq 事件重建指纹集合。
      // 拉取历史期间 WebSocket 可能还在投递新事件（尤其重连对账场景），应用历史时
      // 保留 seq 大于历史最大序号的在途事件，避免这毫秒级窗口内的事件丢失
      const historyMaxSeq = history.reduce(
        (max, item) => Math.max(max, seqOf(item) ?? 0),
        0
      );
      lastSeqRef.current = historyMaxSeq;
      seenFingerprintsRef.current = new Set(
        history.filter((item) => seqOf(item) === null).map(eventFingerprint)
      );
      setEvents((previous) => {
        const inFlight = previous.filter((item) => {
          const seq = seqOf(item);
          return seq !== null && seq > historyMaxSeq;
        });
        return [...history.slice(-HISTORY_MAX_EVENTS), ...inFlight].slice(
          -HISTORY_MAX_EVENTS
        );
      });
      // 只在确实解析到路径时写入：无条件写入会在 WebSocket 重连补发的
      // session_created 已恢复路径之后，把路径覆写成空串，导致文件轮询
      // 永不启动、输出文件面板始终为空
      if (sessionPathFromHistory) {
        setSessionPath(sessionPathFromHistory);
      }
      // 末轮未结束时不能把历史里上一轮的 task_result 填给进行中的轮次，
      // 否则刷新后"研究中"的轮次会显示上一轮的旧回答；空结果让加载动画接管
      setResult(isUnfinished ? "" : resultFromHistory);
      setIsRunning(isUnfinished);
      setIsCancelling(false);
      setHistoryVersion((version) => version + 1);
    } catch (error) {
      setLastError(
        error instanceof Error ? `会话历史加载失败: ${error.message}` : "会话历史加载失败"
      );
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
      setStreamingText("");
      setLastError("");
      setUploadedItems([]);
      uploadedNameSetRef.current.clear();
      lastSeqRef.current = 0;
      seenFingerprintsRef.current = new Set();
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
        // 先判断再清零：重连成功需要对账断线窗口内丢失的事件
        const isReconnect = reconnectAttemptsRef.current > 0;
        reconnectAttemptsRef.current = 0;
        lastServerMessageAtRef.current = Date.now();
        setConnectionState("connected");
        setLastError("");
        if (isReconnect) {
          // 断线期间的 tool_start / task_result 等事件已永久错过，
          // 重新拉取落盘历史对账：游标重建后新事件继续增量并入
          void loadSessionHistory(threadId);
        }
        heartbeatTimerRef.current = window.setInterval(() => {
          if (socket.readyState !== WebSocket.OPEN) {
            return;
          }
          // 半开连接检测：超过两个心跳周期没有收到任何服务端消息，
          // 主动断开（触发 onclose 重连），避免永远显示"已连接"
          if (Date.now() - lastServerMessageAtRef.current > HEARTBEAT_TIMEOUT_MS) {
            socket.close();
            return;
          }
          socket.send("ping");
        }, HEARTBEAT_INTERVAL_MS);
      };

      socket.onmessage = (event) => {
        if (socketRef.current !== socket) {
          return;
        }
        // 任何一帧服务端消息都能证明连接活着（不只 pong）
        lastServerMessageAtRef.current = Date.now();
        try {
          const payload = JSON.parse(event.data) as SocketMessage;
          if (payload.type === "pong") {
            return;
          }

          if (payload.type !== "monitor_event") {
            return;
          }

          // seq 去重：重连对账时回放与实时流会短暂交叠，同一事件只并入一次；
          // 旧版无 seq 的事件退化为指纹比较
          const seq = seqOf(payload);
          if (seq !== null) {
            if (seq <= lastSeqRef.current) {
              return;
            }
            lastSeqRef.current = seq;
          } else {
            const fingerprint = eventFingerprint(payload);
            if (seenFingerprintsRef.current.has(fingerprint)) {
              return;
            }
            seenFingerprintsRef.current.add(fingerprint);
          }

          // 流式增量走独立状态，不进 events 滑窗（理由见 streamingText 定义）
          if (payload.event === "task_delta") {
            const delta = extractString(payload.data, "delta");
            if (delta) {
              setStreamingText((previous) => previous + delta);
            }
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
            setStreamingText("");
            setIsRunning(false);
            setIsCancelling(false);
            // 任务结束后会话摘要（标题/时间/文件数）已变化，刷新侧边栏列表
            void refreshSessions();
          }

          if (payload.event === "task_cancelled") {
            setResult((previous) => previous || payload.message);
            setStreamingText("");
            setIsRunning(false);
            setIsCancelling(false);
          }

          if (payload.event === "error") {
            setLastError(payload.message);
            setStreamingText("");
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
        // 指数退避 + 随机抖动：后端宕机时不再以固定 2 秒无限打点，
        // 恢复后最多等一个周期即可重连
        const delay =
          Math.min(
            RECONNECT_MAX_MS,
            RECONNECT_BASE_MS * 2 ** reconnectAttemptsRef.current
          ) *
          (0.75 + Math.random() * 0.5);
        reconnectAttemptsRef.current += 1;
        reconnectTimerRef.current = window.setTimeout(connect, delay);
      };
    }

    connect();

    return () => {
      disposed = true;
      clearSocketTimers();
      socketRef.current?.close();
    };
  }, [clearSocketTimers, loadSessionHistory, refreshSessions, threadId]);

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
    }, isRunning ? POLL_ACTIVE_MS : POLL_IDLE_MS);

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
      setStreamingText("");
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

  // 只暴露 App 实际消费的状态：sessionPath / 去重游标等内部状态不再外泄
  return {
    cancelCurrentTask,
    connectionState,
    events,
    files,
    historyVersion,
    isCancelling,
    isRunning,
    isUploading,
    lastError,
    removeSession,
    resetSession,
    result,
    sessions,
    stats,
    streamingText,
    submitTask,
    switchSession,
    threadId,
    uploadFiles,
    uploadedItems
  };
}
