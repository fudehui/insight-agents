import { useEffect, useMemo, useState } from "react";
import { Alert, Button, Drawer, Input, Space, Spin, message } from "antd";
import MDEditor from "@uiw/react-md-editor";
import { getFileContentUrl, patchReport } from "../lib/api";
import { ApiError } from "../lib/api";
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

// 会话目录名约定的路径段（output/session_{id}/...），PATCH 定位报告用
const SESSION_SEGMENT_RE = /[\\/]session_([^\\/]+)/;

async function sha256Hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(text)
  );
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

interface PreviewDrawerProps {
  file: OutputFile | null;
  onClose: () => void;
}

// 应用内预览抽屉：Markdown fetch 文本后渲染，PDF/图片走 iframe/img 内联展示，
// 其余类型提示下载。生成完报告不必再"下载后自行打开"。
// Markdown 支持编辑态（P0-3 修订反馈闭环的入口）：保存走 PATCH /api/reports，
// 修订暂存为待沉淀记忆。编辑器仅作文本输入，实时预览复用 MarkdownRenderer
// （react-markdown 不渲染原始 HTML），不引入编辑器自带的 HTML 预览分支。
export function PreviewDrawer({ file, onClose }: PreviewDrawerProps) {
  const [content, setContent] = useState("");
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState("");

  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [baseSha, setBaseSha] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");

  const kind = file ? previewKindOf(file.name) : null;
  const needsText = kind === "markdown" || kind === "text";

  const sessionId = useMemo(() => {
    const match = file?.path.match(SESSION_SEGMENT_RE);
    return match ? match[1] : "";
  }, [file]);

  useEffect(() => {
    setEditing(false);
    setSaveError("");
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
      .then(async (text) => {
        if (cancelled) {
          return;
        }
        setContent(text);
        setBaseSha(await sha256Hex(text));
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

  async function handleSave() {
    if (!file || !sessionId) {
      return;
    }
    setSaving(true);
    setSaveError("");
    try {
      const result = await patchReport({
        session_id: sessionId,
        filename: file.name,
        base_sha256: baseSha,
        content: draft,
      });
      setContent(draft);
      setBaseSha(result.new_sha256);
      setEditing(false);
      message.success(result.message);
    } catch (error) {
      setSaveError(
        error instanceof ApiError ? error.message : "保存失败，请稍后重试"
      );
    } finally {
      setSaving(false);
    }
  }

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
      extra={
        kind === "markdown" && !loading && !loadError ? (
          <Space>
            {editing ? (
              <>
                <Button onClick={() => setEditing(false)}>取消编辑</Button>
                <Button type="primary" loading={saving} onClick={handleSave}>
                  保存修订
                </Button>
              </>
            ) : (
              <Button
                onClick={() => {
                  setDraft(content);
                  setSaveError("");
                  setEditing(true);
                }}
              >
                编辑
              </Button>
            )}
          </Space>
        ) : undefined
      }
    >
      {kind === "pdf" ? (
        <Alert
          message="PDF 为导出产物，不支持在线编辑；请回到 Markdown 报告编辑后重新导出。"
          showIcon
          type="info"
          style={{ marginBottom: 12 }}
        />
      ) : null}

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

      {needsText && editing && kind === "markdown" ? (
        <div className="report-editor">
          <MDEditor
            value={draft}
            onChange={(value) => setDraft(value ?? "")}
            preview="edit"
            height={420}
            data-color-mode="light"
          />
          <div className="report-editor-live">
            <p className="report-editor-live-hint">实时预览（与 Web 端渲染一致）</p>
            <MarkdownRenderer content={draft} />
          </div>
          {saveError ? (
            <Alert
              message={saveError}
              showIcon
              type="error"
              style={{ marginTop: 12 }}
            />
          ) : null}
        </div>
      ) : null}

      {needsText && !editing ? (
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
