import { useCallback, useRef, useState } from "react";

export interface TurnMark {
  /** 对应 turn.id */
  id: string;
  /** 该轮顶部在全文中的位置比例（0-1），用于点击跳转和当前轮判定 */
  ratio: number;
  /** 该轮的用户提问 */
  label: string;
  /** 该轮 AI 回答的文本摘录 */
  answer: string;
}

interface ChatScrollIndicatorProps {
  scrollTop: number;
  scrollHeight: number;
  clientHeight: number;
  /** 每个对话轮次一个标记，按固定小间距紧凑堆叠 */
  marks: TurnMark[];
  /** 滚动到指定 scrollTop（px） */
  onSeekTo: (top: number) => void;
}

const MARK_HIT_RADIUS_PX = 12;
/** 刻度间距上限：轮次少时刻度紧密相邻，轮次多时自动压缩防止溢出 */
const MAX_PITCH_PX = 14;
/** 刻度簇允许占用的最大高度（相对聊天区视口高度），超出则压缩间距 */
const MAX_CLUSTER_SHARE = 0.6;
const QUESTION_MAX_CHARS = 40;
const ANSWER_MAX_CHARS = 140;
const TOOLTIP_ESTIMATED_HEIGHT = 110;

/** 压缩空白并按字数截断，超出补省略号 */
function truncate(text: string, max: number): string {
  const clean = text.replace(/\s+/g, " ").trim();
  return clean.length > max ? `${clean.slice(0, max)}…` : clean;
}

/**
 * 侧边栏与对话内容列之间留白区的轮次导航条（垂直居中）：
 * 每轮对话一条刻度，按固定间距紧密堆叠成一小簇（轮次多时间距自动压缩），
 * 整簇高度自适应并在聊天区内垂直居中；青色加亮为当前视口所处轮次。
 * 悬停刻度在右侧弹出该轮摘要（提问 + 回答摘录，均限长截断）；
 * 点击/拖拽刻度跳转该轮
 */
export function ChatScrollIndicator({
  scrollTop,
  scrollHeight,
  clientHeight,
  marks,
  onSeekTo
}: ChatScrollIndicatorProps) {
  const trackRef = useRef<HTMLDivElement | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [hovered, setHovered] = useState<{ mark: TurnMark; top: number } | null>(
    null
  );

  const scrollable = scrollHeight > clientHeight + 4;
  const scrollRange = Math.max(scrollHeight - clientHeight, 1);
  const count = marks.length;

  // 固定刻度间距；刻度簇超过聊天区高度 60% 时按比例压缩
  const stripHeight = Math.max(clientHeight * MAX_CLUSTER_SHARE, 120);
  const pitch =
    count <= 1 ? 0 : Math.min(MAX_PITCH_PX, stripHeight / (count - 1));
  // 刻度簇总高度：导航条按此高度垂直居中，而不是占满整个聊天区
  const clusterHeight =
    count <= 1 ? MAX_PITCH_PX : Math.round((count - 1) * pitch);

  // 当前视口所处轮次：视口上 35% 锚点落在某轮顶部之后，即认为正在该轮
  const viewportAnchor = scrollTop + clientHeight * 0.35;
  let activeMarkId: string | null = null;
  for (const mark of marks) {
    if (mark.ratio * scrollHeight <= viewportAnchor) {
      activeMarkId = mark.id;
    }
  }

  const seekProportional = useCallback(
    (clientY: number) => {
      const rect = trackRef.current?.getBoundingClientRect();
      if (!rect || rect.height === 0) {
        return;
      }
      const y = clientY - rect.top;
      const trackRatio = Math.min(Math.max(y / rect.height, 0), 1);
      onSeekTo(trackRatio * scrollRange);
    },
    [onSeekTo, scrollRange]
  );  // 指针位置靠近某条刻度（固定间距网格）则吸附到那一轮
  const hitTestMark = useCallback(
    (clientY: number): TurnMark | null => {
      const rect = trackRef.current?.getBoundingClientRect();
      if (!rect || rect.height === 0 || count === 0) {
        return null;
      }
      const y = clientY - rect.top;
      const index = Math.min(
        Math.max(Math.round(y / Math.max(pitch, 1)), 0),
        count - 1
      );
      const distance = Math.abs(y - index * pitch);
      const tolerance = Math.max(MARK_HIT_RADIUS_PX, pitch / 2);
      return distance <= tolerance ? marks[index] : null;
    },
    [count, marks, pitch]
  );

  // 摘要气泡贴着被悬停的刻度显示，上下越界时收回到条内
  function showTooltip(mark: TurnMark, markNode: HTMLSpanElement) {
    const track = trackRef.current;
    if (!track) {
      return;
    }
    const top = Math.min(
      Math.max(markNode.offsetTop - TOOLTIP_ESTIMATED_HEIGHT / 2, 0),
      Math.max(track.clientHeight - TOOLTIP_ESTIMATED_HEIGHT, 0)
    );
    setHovered({ mark, top });
  }

  if (!scrollable) {
    return null;
  }

  return (
    <nav
      aria-label="对话轮次导航"
      className={`chat-scrollbar${isDragging ? " chat-scrollbar--dragging" : ""}`}
      ref={trackRef}
      style={{ height: `${clusterHeight}px` }}
      onPointerDown={(event) => {
        event.preventDefault();
        event.currentTarget.setPointerCapture(event.pointerId);
        setIsDragging(true);
        setHovered(null);
        const mark = hitTestMark(event.clientY);
        if (mark) {
          onSeekTo(Math.max(mark.ratio * scrollHeight - 8, 0));
        } else {
          seekProportional(event.clientY);
        }
      }}
      onPointerMove={(event) => {
        if (isDragging) {
          const mark = hitTestMark(event.clientY);
          if (mark) {
            onSeekTo(Math.max(mark.ratio * scrollHeight - 8, 0));
          } else {
            seekProportional(event.clientY);
          }
        }
      }}
      onPointerUp={() => setIsDragging(false)}
      onPointerCancel={() => setIsDragging(false)}
    >
      {marks.map((mark, index) => {
        const isActive = mark.id === activeMarkId;
        const isHovered = hovered?.mark.id === mark.id;
        return (
          <span
            className={`chat-scrollbar-mark${
              isActive ? " chat-scrollbar-mark--active" : ""
            }${isHovered ? " chat-scrollbar-mark--hover" : ""}`}
            key={mark.id}
            style={{ top: `${index * pitch}px` }}
            onPointerEnter={(event) => showTooltip(mark, event.currentTarget)}
            onPointerLeave={() =>
              setHovered((current) =>
                current?.mark.id === mark.id ? null : current
              )
            }
          />
        );
      })}
      {hovered ? (
        <div
          aria-hidden
          className="chat-scrollbar-tooltip"
          style={{ top: hovered.top }}
        >
          <strong>
            {truncate(hovered.mark.label, QUESTION_MAX_CHARS) || "（无提问内容）"}
          </strong>
          {hovered.mark.answer ? (
            <p>{truncate(hovered.mark.answer, ANSWER_MAX_CHARS)}</p>
          ) : (
            <p>（该轮暂无文本回复）</p>
          )}
        </div>
      ) : null}
    </nav>
  );
}
