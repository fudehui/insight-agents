import {
  ApiOutlined,
  BranchesOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  CloseOutlined,
  CloudServerOutlined,
  DatabaseOutlined,
  DeleteOutlined,
  FileSearchOutlined,
  MenuOutlined,
  MessageOutlined,
  ToolOutlined
} from "@ant-design/icons";
import { Alert, App as AntApp, Button, Popconfirm } from "antd";
import { useCallback, useEffect, useRef, useState } from "react";
import { ChatComposer } from "./components/ChatComposer";
import { ChatScrollIndicator } from "./components/ChatScrollIndicator";
import type { TurnMark } from "./components/ChatScrollIndicator";
import { ConversationThread } from "./components/ConversationThread";
import type { ChatTurn } from "./components/ConversationThread";
import { API_BASE_URL, WS_BASE_URL } from "./lib/config";
import { TERMINAL_EVENTS } from "./lib/constants";
import { createThreadId } from "./lib/thread";
import { useDeepAgentSession } from "./hooks/useDeepAgentSession";
import type {
  ConnectionState,
  MonitorMessage,
  OutputFile,
  SourceCollection,
  UploadedItem
} from "./types";

function connectionLabel(state: ConnectionState): string {
  const labels: Record<ConnectionState, string> = {
    connecting: "连接中",
    connected: "已连接",
    reconnecting: "重连中",
    closed: "已关闭"
  };
  return labels[state];
}

function createTurn(content: string): ChatTurn {
  return {
    id: createThreadId(),
    content,
    events: [],
    files: [],
    sources: null,
    isRunning: true,
    result: "",
    timestamp: new Date().toISOString()
  };
}

// 从 task_files 事件中解析本轮生成的文件清单（结构与 /api/files 一致）
function parseTaskFiles(data: Record<string, unknown>): OutputFile[] {
  if (!Array.isArray(data.files)) {
    return [];
  }
  return data.files.filter(
    (item): item is OutputFile =>
      typeof item === "object" &&
      item !== null &&
      typeof (item as OutputFile).path === "string"
  );
}

// 从 task_sources 事件中解析本轮收集的来源清单（结构与后端 source_registry 一致）
function parseTaskSources(data: Record<string, unknown>): SourceCollection | null {
  const web = Array.isArray(data.web)
    ? data.web.filter(
        (item): item is SourceCollection["web"][number] =>
          typeof item === "object" &&
          item !== null &&
          typeof (item as { url?: unknown }).url === "string"
      )
    : [];
  const docs = Array.isArray(data.docs)
    ? data.docs.filter(
        (item): item is SourceCollection["docs"][number] =>
          typeof item === "object" &&
          item !== null &&
          typeof (item as { doc?: unknown }).doc === "string"
      )
    : [];
  const sql = Array.isArray(data.sql)
    ? data.sql.filter((item): item is string => typeof item === "string")
    : [];
  if (web.length === 0 && docs.length === 0 && sql.length === 0) {
    return null;
  }
  return { web, docs, sql };
}

// 历史事件回放：按 task_start 切分对话轮次，重建与实时对话一致的 turns 结构
function rebuildTurnsFromHistory(events: MonitorMessage[]): ChatTurn[] {
  const turns: ChatTurn[] = [];
  let current: ChatTurn | null = null;

  for (const event of events) {
    if (event.event === "task_start") {
      if (current) {
        current.isRunning = false;
        turns.push(current);
      }
      current = {
        id: createThreadId(),
        content:
          typeof event.data.query === "string" ? event.data.query : event.message,
        events: [],
        files: [],
        sources: null,
        isRunning: true,
        result: "",
        timestamp: event.timestamp
      };
      continue;
    }

    if (!current) {
      // task_files / task_sources 由后端在 task_result 之后补发，到达时轮次已闭合
      // （current 为空）。它们属于刚结束的那一轮，归入上一轮，而不是创建
      // "历史会话恢复"孤儿轮
      if (
        (event.event === "task_files" || event.event === "task_sources") &&
        turns.length > 0
      ) {
        const lastTurn = turns[turns.length - 1];
        lastTurn.events.push(event);
        if (event.event === "task_files") {
          lastTurn.files = parseTaskFiles(event.data);
        } else {
          lastTurn.sources = parseTaskSources(event.data);
        }
        continue;
      }
      // 旧版本会话没有 task_start 事件，整段历史归入一个恢复轮次
      current = {
        id: createThreadId(),
        content: "（历史会话恢复）",
        events: [],
        files: [],
        sources: null,
        isRunning: false,
        result: "",
        timestamp: event.timestamp
      };
    }

    current.events.push(event);

    if (event.event === "session_created" && typeof event.data.path === "string") {
      current.files = [];
    }

    // 本轮任务的产物清单由后端 diff 得出，回放时每个轮次只显示自己生成的文件
    if (event.event === "task_files") {
      current.files = parseTaskFiles(event.data);
    }

    // 本轮任务的溯源清单同样由后端登记表统一补发
    if (event.event === "task_sources") {
      current.sources = parseTaskSources(event.data);
    }

    if (event.event === "task_result") {
      if (typeof event.data.result === "string") {
        current.result = event.data.result;
      } else {
        current.result = event.message;
      }
      current.isRunning = false;
      turns.push(current);
      current = null;
    } else if (event.event === "task_cancelled") {
      current.result = current.result || event.message;
      current.isRunning = false;
      turns.push(current);
      current = null;
    }
  }

  if (current) {
    current.isRunning = false;
    turns.push(current);
  }

  return turns;
}

function formatSessionTime(mtime: number): string {
  const date = new Date(mtime * 1000);
  if (Number.isNaN(date.getTime())) {
    return "--";
  }
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false
  });
}

export default function App() {
  const { message } = AntApp.useApp();
  const [query, setQuery] = useState("");
  // ≤980px 时侧栏改为抽屉：记录开合状态，宽屏下该状态不参与布局
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [stagedItems, setStagedItems] = useState<UploadedItem[]>([]);
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const streamRef = useRef<HTMLElement | null>(null);
  const session = useDeepAgentSession();
  // 当前"进行中"轮次的 id，以及已并入 turns 的事件数（增量同步游标）
  const liveTurnIdRef = useRef<string | null>(null);
  const consumedEventsRef = useRef(0);
  // 本轮开始前会话中已存在的文件路径：运行中据此从轮询列表里排除历史文件，
  // task_files 事件到达后由后端 diff 的权威清单接管
  const baselineFilesRef = useRef<Set<string>>(new Set());
  // 用户翻离底部后停止自动跟随，翻回底部（或点击"回到底部"）再恢复
  const stickToBottomRef = useRef(true);
  const scrollRafRef = useRef(0);
  const [scrollState, setScrollState] = useState({
    top: 0,
    height: 0,
    client: 0
  });
  // 每个对话轮次在滚动区中的位置比例，供右侧轮次导航渲染标记
  const [turnMarks, setTurnMarks] = useState<TurnMark[]>([]);

  // 历史事件加载完成（挂载恢复或切换会话）时，用回放重建整段对话。
  // 只依赖 historyVersion：实时事件只改 events 不改 version，不会触发重建
  useEffect(() => {
    if (session.historyVersion === 0 || session.events.length === 0) {
      return;
    }

    const rebuilt = rebuildTurnsFromHistory(session.events);
    if (rebuilt.length === 0) {
      return;
    }
    setTurns(rebuilt);

    // 刷新时任务可能仍在后端执行：最后一轮没有终态事件就接续为进行中的轮次，
    // 之后到达的实时事件会增量追加到这一轮，而不是重新灌入整段历史
    const lastTurn = rebuilt[rebuilt.length - 1];
    const hasTerminalEvent = lastTurn.events.some((event) =>
      TERMINAL_EVENTS.includes(event.event)
    );
    if (!hasTerminalEvent) {
      liveTurnIdRef.current = lastTurn.id;
      consumedEventsRef.current = session.events.length;
      // 刷新恢复时任务已在执行，无从得知本轮开始前的文件基线；
      // 把当前已列出的文件全部视为基线，恢复期间不把历史文件算进本轮，
      // 任务结束时的 task_files 权威清单会最终接管
      baselineFilesRef.current = new Set(session.files.map((file) => file.path));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session.historyVersion]);

  // 实时同步：只把新产生的事件增量追加到"进行中"的那一轮。
  // 没有进行中的轮次（例如刚回放完历史）时绝不改动 turns，
  // 避免把整段 session.events 灌进最后一轮（刷新后对话全量重复的根因）
  useEffect(() => {
    const liveId = liveTurnIdRef.current;
    if (!liveId) {
      return;
    }

    const newEvents = session.events.slice(consumedEventsRef.current);
    consumedEventsRef.current = session.events.length;

    setTurns((previous) => {
      const index = previous.findIndex((turn) => turn.id === liveId);
      if (index === -1) {
        return previous;
      }
      const liveTurn = previous[index];
      // 本轮文件展示优先级：task_files 权威清单 > 运行中差集（轮询列表减基线） > 维持现状。
      // 回放恢复的轮次 events 里已带 task_files：没有新的权威清单时绝不降级为全量差集，
      // 否则历史文件和事件日志会覆盖掉本轮的真实产物
      const taskFilesEvent = [...newEvents]
        .reverse()
        .find((event) => event.event === "task_files");
      const taskSourcesEvent = [...newEvents]
        .reverse()
        .find((event) => event.event === "task_sources");
      const hasAuthoritativeFiles = liveTurn.events.some(
        (event) => event.event === "task_files"
      );
      let nextFiles = liveTurn.files;
      if (taskFilesEvent) {
        nextFiles = parseTaskFiles(taskFilesEvent.data);
      } else if (session.isRunning && !hasAuthoritativeFiles) {
        nextFiles = session.files.filter(
          (file) => !baselineFilesRef.current.has(file.path)
        );
      }
      const nextSources = taskSourcesEvent
        ? parseTaskSources(taskSourcesEvent.data)
        : null;
      const next = [...previous];
      const nextTurn = {
        ...liveTurn,
        events: [...liveTurn.events, ...newEvents],
        files: nextFiles,
        sources: nextSources ?? liveTurn.sources,
        isRunning: session.isRunning,
        result: session.result,
        // 流式增量先行展示，task_result 权威结果到达后覆盖
        streamingAnswer: session.streamingText
      };
      // 文件轮询等触发源会在没有新事件时反复进入本 effect：内容没有实际
      // 变化时直接返回旧引用，避免 6 秒一次的轮询引发整棵对话树重渲染
      if (
        newEvents.length === 0 &&
        nextFiles === liveTurn.files &&
        nextSources === null &&
        liveTurn.isRunning === session.isRunning &&
        liveTurn.result === session.result &&
        (liveTurn.streamingAnswer ?? "") === session.streamingText
      ) {
        return previous;
      }
      next[index] = nextTurn;
      return next;
    });

    // 终态事件到达后不再立即注销 liveId：task_files / task_sources 由后端在
    // 任务收尾（task_result 之后）补发，必须继续并入这一轮；liveId 会在
    // 下一轮任务提交或切换会话时被替换
  }, [session.events, session.files, session.isRunning, session.result, session.streamingText]);

  // 无进行中任务时（如历史回放后），轮询刷新到的文件列表跟随到最后一轮展示；
  // 最后一轮已有自己的 task_files 清单时不要用全量列表覆盖，旧会话无该事件则保持原行为。
  // 依赖中加入 historyVersion：挂载时轮询可能先于 turns 重建完成，重建后立刻补一次
  // 同步，避免错过那次 session.files 更新导致输出文件面板长时间为空
  useEffect(() => {
    if (liveTurnIdRef.current) {
      return;
    }
    setTurns((previous) => {
      if (previous.length === 0) {
        return previous;
      }
      const latest = previous[previous.length - 1];
      if (latest.events.some((event) => event.event === "task_files")) {
        return previous;
      }
      if (latest.files.length === session.files.length) {
        return previous;
      }
      const next = [...previous];
      next[next.length - 1] = { ...latest, files: session.files };
      return next;
    });
  }, [session.files, session.historyVersion]);

  // 测量各轮次在滚动区中的位置，换算成 0-1 比例；内容高度随事件流变化，需反复测量
  const measureTurnMarks = useCallback(() => {
    const node = streamRef.current;
    if (!node || node.scrollHeight <= 0) {
      setTurnMarks([]);
      return;
    }
    const containerTop = node.getBoundingClientRect().top;
    const turnEls = Array.from(
      node.querySelectorAll<HTMLElement>("[data-turn-id]")
    );
    setTurnMarks(
      turnEls.map((el) => {
        const turn = turns.find((item) => item.id === el.dataset.turnId);
        return {
          id: el.dataset.turnId || "",
          ratio: Math.min(
            Math.max(
              (el.getBoundingClientRect().top - containerTop + node.scrollTop) /
                node.scrollHeight,
              0
            ),
            1
          ),
          label: turn?.content || "",
          answer: turn?.result || ""
        };
      })
    );
  }, [turns]);

  // 用户滚动时更新贴底标记和指示器状态（rAF 节流，避免高频 setState）
  function handleStreamScroll() {
    if (scrollRafRef.current) {
      return;
    }
    scrollRafRef.current = window.requestAnimationFrame(() => {
      scrollRafRef.current = 0;
      const node = streamRef.current;
      if (!node) {
        return;
      }
      const distance = node.scrollHeight - node.scrollTop - node.clientHeight;
      stickToBottomRef.current = distance < 60;
      setScrollState({
        top: node.scrollTop,
        height: node.scrollHeight,
        client: node.clientHeight
      });
      measureTurnMarks();
    });
  }

  // 只在用户贴底时跟随新内容；上翻阅读历史时不再自动拽回底部
  useEffect(() => {
    const node = streamRef.current;
    if (!node) {
      return;
    }

    window.requestAnimationFrame(() => {
      if (stickToBottomRef.current) {
        node.scrollTo({ top: node.scrollHeight, behavior: "smooth" });
      }
      setScrollState({
        top: node.scrollTop,
        height: node.scrollHeight,
        client: node.clientHeight
      });
      measureTurnMarks();
    });
  }, [turns, measureTurnMarks]);

  // 启动一轮任务：创建轮次、登记实时同步游标与文件基线，然后调用后端。
  // 输入框发送与"重试"共用这一条路径
  async function startTurn(cleanQuery: string) {
    const nextTurn = createTurn(cleanQuery);
    setTurns((previous) => [...previous, nextTurn]);
    // 从这一轮开始进入实时同步：事件游标归零（submitTask 会清空 events）
    liveTurnIdRef.current = nextTurn.id;
    consumedEventsRef.current = 0;
    // 记录本轮开始前已存在的文件，运行中只把新增文件展示给本轮
    baselineFilesRef.current = new Set(session.files.map((file) => file.path));

    try {
      await session.submitTask(cleanQuery);
      message.success("任务已启动，执行过程会显示在对话中");
    } catch (error) {
      // 启动失败时由这里直接写入错误信息，并结束该轮的实时同步
      liveTurnIdRef.current = null;
      setTurns((previous) =>
        previous.map((turn) =>
          turn.id === nextTurn.id
            ? {
                ...turn,
                isRunning: false,
                result: error instanceof Error ? error.message : "任务启动失败"
              }
            : turn
        )
      );
      message.error(error instanceof Error ? error.message : "任务启动失败");
    }
  }

  async function handleSubmit() {
    const cleanQuery = query.trim();
    if (!cleanQuery) {
      message.warning("请输入研究任务");
      return;
    }

    setQuery("");
    await startTurn(cleanQuery);
  }

  // 失败轮次的"重试"：以原问题新开一轮，不影响输入框当前内容
  function handleRetry(prompt: string) {
    void startTurn(prompt);
  }

  async function handleCancel() {
    try {
      const response = await session.cancelCurrentTask();
      message.info(response.status === "cancelling" ? "取消请求已发送，正在等待当前调用结束" : "任务已取消");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "取消任务失败");
    }
  }

  async function handleUpload(items: UploadedItem[]) {
    try {
      const response = await session.uploadFiles(items);
      setStagedItems([]);
      message.success(`已上传 ${response.files.length} 个文件`);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "上传失败");
    }
  }

  function handleNewSession() {
    liveTurnIdRef.current = null;
    consumedEventsRef.current = 0;
    stickToBottomRef.current = true;
    session.resetSession();
    setTurns([]);
    setQuery("");
    setStagedItems([]);
  }

  // 删除会话：同时清理该会话的运行任务、产物目录和上传附件；
  // 删除当前会话时后端数据没了但 thread_id 仍指向它，必须换新会话避免事件写回
  async function handleDeleteSession(targetThreadId: string) {
    const title =
      session.sessions.find((item) => item.thread_id === targetThreadId)?.title ||
      `会话 ${targetThreadId.slice(0, 8)}`;
    try {
      await session.removeSession(targetThreadId);
      liveTurnIdRef.current = null;
      consumedEventsRef.current = 0;
      if (targetThreadId === session.threadId) {
        setTurns([]);
        setQuery("");
        setStagedItems([]);
      }
      message.success(`已删除会话：${title.slice(0, 30)}`);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "删除会话失败");
    }
  }

  // 切换历史会话：先清空当前对话，等回放数据到达后由重建 effect 填充
  function handleSwitchSession(targetThreadId: string) {
    if (targetThreadId === session.threadId) {
      return;
    }
    liveTurnIdRef.current = null;
    consumedEventsRef.current = 0;
    stickToBottomRef.current = true;
    setTurns([]);
    setQuery("");
    setStagedItems([]);
    session.switchSession(targetThreadId);
  }

  // 轮次导航：点击/拖拽定位到指定 scrollTop，随后的 scroll 事件会重新计算贴底状态
  function handleScrollSeek(top: number) {
    const node = streamRef.current;
    if (!node) {
      return;
    }
    node.scrollTo({ top, behavior: "auto" });
  }

  const online = session.connectionState === "connected";

  return (
    <div className="chat-app-shell min-h-dvh">
      {/* 窄屏抽屉打开时的背景遮罩：点击即收起 */}
      {drawerOpen ? (
        <div
          aria-hidden
          className="sidebar-drawer-mask"
          onClick={() => setDrawerOpen(false)}
        />
      ) : null}
      <aside
        aria-label="会话信息"
        className={`chat-sidebar ${drawerOpen ? "chat-sidebar--open" : ""}`}
      >
        <div className="sidebar-brand">
          <div>
            <span className="panel-kicker">INSIGHT AGENTS</span>
            <h1>慧研</h1>
            <p>对话式多智能体研究台</p>
          </div>
          <Button
            aria-label="关闭会话信息"
            className="sidebar-drawer-close"
            onClick={() => setDrawerOpen(false)}
            type="text"
          >
            <CloseOutlined aria-hidden />
          </Button>
        </div>

        <Button
          className="new-chat-button"
          block
          onClick={() => {
            handleNewSession();
            setDrawerOpen(false);
          }}
        >
          新建研究
        </Button>

        <div className="sidebar-section sidebar-sessions">
          <span className="sidebar-label">HISTORY</span>
          {session.sessions.length === 0 ? (
            <p className="session-empty">暂无历史会话</p>
          ) : (
            <ul className="session-list" aria-label="历史会话列表">
              {session.sessions.map((item) => (
                <li key={item.thread_id}>
                  <button
                    className={`session-item ${
                      item.thread_id === session.threadId ? "session-item--active" : ""
                    }`}
                    onClick={() => {
                      handleSwitchSession(item.thread_id);
                      setDrawerOpen(false);
                    }}
                    type="button"
                    title={item.title || item.thread_id}
                  >
                    <span className="session-item-icon" aria-hidden>
                      <MessageOutlined />
                    </span>
                    <span className="session-item-copy">
                      <strong>{item.title || `会话 ${item.thread_id.slice(0, 8)}`}</strong>
                      <small>
                        {formatSessionTime(item.mtime)} · {item.file_count} 个文件
                      </small>
                    </span>
                  </button>
                  <Popconfirm
                    title="删除会话"
                    description="将同时删除该会话的生成文件与上传附件，且不可恢复。"
                    okText="删除"
                    okButtonProps={{ danger: true }}
                    cancelText="取消"
                    onConfirm={() => void handleDeleteSession(item.thread_id)}
                  >
                    <button
                      className="session-item-delete"
                      type="button"
                      aria-label={`删除会话 ${item.title || item.thread_id.slice(0, 8)}`}
                      title="删除会话"
                    >
                      <DeleteOutlined />
                    </button>
                  </Popconfirm>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="sidebar-section">
          <span className="sidebar-label">THREAD</span>
          <strong className="thread-id" title={session.threadId}>
            {session.threadId.slice(0, 8)}
          </strong>
        </div>

        <div className="sidebar-status-list">
          <div className={`sidebar-status ${online ? "sidebar-status--online" : "sidebar-status--warn"}`}>
            <ApiOutlined aria-hidden />
            <span>WebSocket</span>
            <strong>{connectionLabel(session.connectionState)}</strong>
          </div>
          <div className="sidebar-status">
            <BranchesOutlined aria-hidden />
            <span>助手调度</span>
            <strong>{session.stats.assistantEvents}</strong>
          </div>
          <div className="sidebar-status">
            <ToolOutlined aria-hidden />
            <span>工具调用</span>
            <strong>{session.stats.toolEvents}</strong>
          </div>
          <div className={session.stats.errorEvents > 0 ? "sidebar-status sidebar-status--error" : "sidebar-status"}>
            <CloseCircleOutlined aria-hidden />
            <span>异常</span>
            <strong>{session.stats.errorEvents}</strong>
          </div>
        </div>

        <div className="sidebar-section">
          <span className="sidebar-label">AGENTS</span>
          <ul className="agent-mini-list">
            <li>
              <CloudServerOutlined aria-hidden />
              网络搜索助手
            </li>
            <li>
              <DatabaseOutlined aria-hidden />
              数据库查询助手
            </li>
            <li>
              <FileSearchOutlined aria-hidden />
              RAGFlow 助手
            </li>
          </ul>
        </div>

        <div className="sidebar-section sidebar-endpoints">
          <span className="sidebar-label">ENDPOINTS</span>
          <code>{API_BASE_URL || "(同源)"}</code>
          <code>{WS_BASE_URL}</code>
        </div>
      </aside>

      <main className="chat-main">
        <header className="chat-topbar">
          <div className="chat-topbar-title">
            <Button
              aria-label="打开会话信息"
              className="sidebar-drawer-toggle"
              onClick={() => setDrawerOpen(true)}
              type="text"
            >
              <MenuOutlined aria-hidden />
            </Button>
            <div>
              <span className="panel-kicker">CHAT WORKSPACE</span>
              <h2>慧研对话</h2>
            </div>
          </div>
          <div className={`run-indicator ${session.isRunning ? "run-indicator--live" : ""}`}>
            {session.isRunning ? <BranchesOutlined aria-hidden /> : <CheckCircleOutlined aria-hidden />}
            {session.isRunning ? "研究中" : "待命"}
          </div>
        </header>

        {session.lastError ? (
          <Alert
            className="chat-alert"
            message={session.lastError}
            showIcon
            type="error"
          />
        ) : null}

        <div className="chat-stream-wrap">
          <section
            className="chat-stream-panel"
            onScroll={handleStreamScroll}
            ref={streamRef}
          >
            <ConversationThread
              onRetry={handleRetry}
              onUseExample={setQuery}
              turns={turns}
            />
          </section>
          <ChatScrollIndicator
            clientHeight={scrollState.client}
            marks={turnMarks}
            onSeekTo={handleScrollSeek}
            scrollHeight={scrollState.height}
            scrollTop={scrollState.top}
          />
        </div>

        <ChatComposer
          isCancelling={session.isCancelling}
          isRunning={session.isRunning}
          isUploading={session.isUploading}
          onCancel={handleCancel}
          onNewSession={handleNewSession}
          onQueryChange={setQuery}
          onStagedItemsChange={setStagedItems}
          onSubmit={handleSubmit}
          onUpload={handleUpload}
          query={query}
          stagedItems={stagedItems}
          uploadedItems={session.uploadedItems}
        />
      </main>
    </div>
  );
}
