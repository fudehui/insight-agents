import { useEffect, useMemo, useRef, isValidElement, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import * as echarts from "echarts";

interface MarkdownRendererProps {
  content: string;
}

const ECHARTS_BLOCK_HEIGHT = 320;

/**
 * 报告内嵌交互图表：```echarts 围栏内容为 ECharts option JSON。
 * 后端 generate_chart 只放行纯 JSON option（禁止函数/脚本字符串），
 * 前端再做一次 JSON.parse 兜底——解析失败降级为普通代码块展示
 * （约定见 docs/upgrade-plan.md W7）。
 */
function EChartsBlock({ optionText }: { optionText: string }) {
  const containerRef = useRef<HTMLDivElement>(null);

  const parseError = useMemo(() => {
    try {
      JSON.parse(optionText);
      return false;
    } catch {
      return true;
    }
  }, [optionText]);

  useEffect(() => {
    if (parseError || !containerRef.current) return;
    const chart = echarts.init(containerRef.current);
    chart.setOption(JSON.parse(optionText));
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(containerRef.current);
    return () => {
      observer.disconnect();
      chart.dispose();
    };
  }, [optionText, parseError]);

  if (parseError) {
    return (
      <pre className="echarts-fallback">
        <code>{optionText}</code>
      </pre>
    );
  }
  return (
    <div
      ref={containerRef}
      className="echarts-block"
      style={{ width: "100%", height: ECHARTS_BLOCK_HEIGHT }}
    />
  );
}

interface CodeElementProps {
  className?: string;
  children?: ReactNode;
}

export function MarkdownRenderer({ content }: MarkdownRendererProps) {
  return (
    <div className="markdown-body">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a({ children, href, ...props }) {
            return (
              <a href={href} rel="noreferrer" target="_blank" {...props}>
                {children}
              </a>
            );
          },
          pre({ children }) {
            const child = Array.isArray(children) ? children[0] : children;
            if (isValidElement<CodeElementProps>(child)) {
              const className = child.props.className ?? "";
              if (className.includes("language-echarts")) {
                const optionText = String(child.props.children ?? "").trim();
                return <EChartsBlock optionText={optionText} />;
              }
            }
            return <pre>{children}</pre>;
          }
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
