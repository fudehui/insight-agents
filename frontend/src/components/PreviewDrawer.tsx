import { useEffect, useState } from "react";
import { Alert, Drawer, Spin } from "antd";
import { getFileContentUrl } from "../lib/api";
import { MarkdownRenderer } from "./MarkdownRenderer";
import type { OutputFile } from "../types";

type PreviewKind = "markdown" | "text" | "pdf" | "image" | "unsupported";

function previewKindOf(name: string): PreviewKind {
  const lower = name.toLowerCase();
  if (lower.endsWith(".md") || lower.endsWith(".markdown")) {
    return "markdown";
  }
  if (lower.endsWith(".pdf")) {
    return "pdf";
  }
  if (/\.(png|jpe?g|gif|webp)$/.test(lower)) {
    return "image";
  }
  if (lower.endsWith(".txt") || lower.endsWith(".csv") || lower.endsWith(".json")) {
    return "text";
  }
  return "unsupported";
}

interface PreviewDrawerProps {
  file: OutputFile | null;
  onClose: () => void;
}

// 应用内预览抽屉：Markdown fetch 文本后渲染，PDF/图片走 iframe/img 内联展示，
// 其余类型提示下载。生成完报告不必再"下载后自行打开"
export function PreviewDrawer({ file, onClose }: PreviewDrawerProps) {
  const [content, setContent] = useState("");
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState("");

  const kind = file ? previewKindOf(file.name) : null;
  const needsText = kind === "markdown" || kind === "text";

  useEffect(() => {
    if (!file || !needsText) {
      setContent("");
      setLoadError("");
      return;
    }

    let cancelled = false;
    setLoading(true);
    setLoadError("");
    fetch(getFileContentUrl(file.path))
      .then(async (response) => {
        if (!response.ok) {
          throw new Error(`HTTP ${response.status}`);
        }
        return response.text();
      })
      .then((text) => {
        if (!cancelled) {
          setContent(text);
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setLoadError(error instanceof Error ? error.message : "预览加载失败");
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [file, needsText]);

  if (!file || !kind) {
    return <Drawer onClose={onClose} open={false} />;
  }

  return (
    <Drawer
      className="preview-drawer"
      onClose={onClose}
      open
      title={file.name}
      width="min(760px, 92vw)"
    >
      {kind === "pdf" ? (
        <iframe
          className="preview-frame"
          src={getFileContentUrl(file.path)}
          title={file.name}
        />
      ) : null}

      {kind === "image" ? (
        <img
          alt={file.name}
          className="preview-image"
          src={getFileContentUrl(file.path)}
        />
      ) : null}

      {needsText ? (
        loading ? (
          <div className="preview-loading">
            <Spin />
          </div>
        ) : loadError ? (
          <Alert message={loadError} showIcon type="error" />
        ) : kind === "markdown" ? (
          <MarkdownRenderer content={content} />
        ) : (
          <pre className="preview-text">{content}</pre>
        )
      ) : null}

      {kind === "unsupported" ? (
        <Alert
          message="该文件类型不支持应用内预览，请下载后查看。"
          showIcon
          type="info"
        />
      ) : null}
    </Drawer>
  );
}
