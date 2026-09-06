import {
  DownOutlined,
  PaperClipOutlined,
  PlusOutlined,
  SafetyCertificateOutlined,
  SendOutlined,
  StopOutlined
} from "@ant-design/icons";
import { App as AntApp, Button, Dropdown, Tooltip, Upload } from "antd";
import type { UploadFile } from "antd";
import { UPLOAD_ALLOWED_EXTENSIONS, UPLOAD_MAX_FILE_SIZE_MB } from "../lib/constants";
import { APPROVAL_MODE_OPTIONS, type ApprovalMode } from "../lib/approvalMode";
import type { UploadedItem } from "../types";

interface ChatComposerProps {
  approvalMode: ApprovalMode;
  isCancelling: boolean;
  isRunning: boolean;
  isUploading: boolean;
  onApprovalModeChange: (mode: ApprovalMode) => void;
  onNewSession: () => void;
  onCancel: () => void;
  onQueryChange: (value: string) => void;
  onSubmit: () => void;
  onUpload: (items: UploadedItem[]) => Promise<void> | void;
  query: string;
  stagedItems: UploadedItem[];
  uploadedItems: UploadedItem[];
  onStagedItemsChange: (items: UploadedItem[]) => void;
}

function toUploadedItem(file: UploadFile): UploadedItem | null {
  if (!file.originFileObj) {
    return null;
  }

  return {
    uid: file.uid,
    name: file.name,
    size: file.size || 0,
    raw: file.originFileObj
  };
}

function uniqueUploadedItems(items: UploadedItem[]): UploadedItem[] {
  const names = new Set<string>();
  return items.filter((item) => {
    if (names.has(item.name)) {
      return false;
    }
    names.add(item.name);
    return true;
  });
}

export function ChatComposer({
  approvalMode,
  isCancelling,
  isRunning,
  isUploading,
  onApprovalModeChange,
  onCancel,
  onNewSession,
  onQueryChange,
  onStagedItemsChange,
  onSubmit,
  onUpload,
  query,
  stagedItems,
  uploadedItems
}: ChatComposerProps) {
  const { message } = AntApp.useApp();
  const hasStagedFiles = stagedItems.length > 0;
  const canSubmit = query.trim().length > 0;
  const currentMode =
    APPROVAL_MODE_OPTIONS.find((option) => option.value === approvalMode) ??
    APPROVAL_MODE_OPTIONS[1];

  // 前端先拦一层类型与大小（与后端 /api/upload 校验一致），
  // LIST_IGNORE 让违规文件不进入 fileList，直接无声过滤
  function handleBeforeUpload(file: UploadFile) {
    const lower = (file.name || "").toLowerCase();
    const allowed = UPLOAD_ALLOWED_EXTENSIONS.some((ext) => lower.endsWith(ext));
    if (!allowed) {
      message.error(`不支持的文件类型：${file.name}（允许 ${UPLOAD_ALLOWED_EXTENSIONS.join(" ")}）`);
      return Upload.LIST_IGNORE;
    }
    if ((file.size || 0) > UPLOAD_MAX_FILE_SIZE_MB * 1024 * 1024) {
      message.error(`文件过大（上限 ${UPLOAD_MAX_FILE_SIZE_MB}MB）：${file.name}`);
      return Upload.LIST_IGNORE;
    }
    // 返回 false 关闭 antd 自动上传，仍由 onChange 里的 handleAttachmentChange 手动上传
    return false;
  }

  function handleAttachmentChange(fileList: UploadFile[]) {
    const nextItems = uniqueUploadedItems(
      fileList
        .map(toUploadedItem)
        .filter((item): item is UploadedItem => Boolean(item))
    );

    if (nextItems.length === 0) {
      return;
    }

    onStagedItemsChange(nextItems);
    void Promise.resolve(onUpload(nextItems)).finally(() => {
      onStagedItemsChange([]);
    });
  }

  return (
    <section className="chat-composer" aria-label="发送研究任务">
      {uploadedItems.length > 0 ? (
        <div className="attachment-strip" aria-label="当前会话附件">
          {uploadedItems.map((item) => (
            <span className="attachment-pill" key={`${item.uid}-${item.name}`}>
              <PaperClipOutlined aria-hidden />
              {item.name}
            </span>
          ))}
        </div>
      ) : null}

      {hasStagedFiles ? (
        <div className="attachment-strip" aria-label="待上传附件">
          {stagedItems.map((item) => (
            <span className="attachment-pill attachment-pill--pending" key={item.uid}>
              <PaperClipOutlined aria-hidden />
              {item.name}
            </span>
          ))}
          {isUploading ? <span className="attachment-uploading">附着中...</span> : null}
        </div>
      ) : null}

      <div className="composer-shell">
        <textarea
          aria-label="研究任务"
          disabled={isRunning}
          onChange={(event) => onQueryChange(event.target.value)}
          onKeyDown={(event) => {
            // 中文输入法确认候选词的 Enter（isComposing / keyCode 229）
            // 不触发发送，否则拼音上屏会把未完成的输入直接提交
            if (event.nativeEvent.isComposing || event.keyCode === 229) {
              return;
            }
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              onSubmit();
            }
          }}
          placeholder="向 Insight Agents 发送任务..."
          value={query}
        />

        <div className="composer-toolbar">
          <div className="composer-left-actions">
            <Tooltip title="新建会话">
              <Button
                aria-label="新建会话"
                className="composer-icon-button"
                icon={<PlusOutlined />}
                onClick={onNewSession}
                shape="circle"
              />
            </Tooltip>
            <Upload
              accept={UPLOAD_ALLOWED_EXTENSIONS.join(",")}
              beforeUpload={handleBeforeUpload}
              fileList={[]}
              multiple
              onChange={(info) => {
                handleAttachmentChange(info.fileList.length > 0 ? info.fileList : [info.file]);
              }}
              showUploadList={false}
            >
              <Tooltip title="选择附件">
                <Button
                  aria-label="选择附件"
                  className="composer-icon-button"
                  disabled={isRunning || isUploading}
                  icon={<PaperClipOutlined />}
                  shape="circle"
                />
              </Tooltip>
            </Upload>

            <Dropdown
              menu={{
                items: APPROVAL_MODE_OPTIONS.map((option) => ({
                  key: option.value,
                  label: (
                    <div className="approval-mode-option">
                      <span className="approval-mode-option-label">{option.label}</span>
                      <span className="approval-mode-option-desc">{option.description}</span>
                    </div>
                  )
                })),
                onClick: ({ key }) => {
                  const mode = APPROVAL_MODE_OPTIONS.find(
                    (option) => option.value === key
                  );
                  if (mode) {
                    onApprovalModeChange(mode.value);
                    message.info(`审批档位已切换为「${mode.label}」，对下一个任务生效`);
                  }
                },
                selectable: true,
                selectedKeys: [approvalMode]
              }}
              placement="topLeft"
              trigger={["click"]}
            >
              <Button
                aria-label="选择审批档位"
                className="approval-mode-button"
                size="small"
              >
                <SafetyCertificateOutlined aria-hidden />
                <span>审批·{currentMode.label}</span>
                <DownOutlined className="approval-mode-caret" aria-hidden />
              </Button>
            </Dropdown>
          </div>

          <Tooltip title={isRunning ? "取消当前任务" : "发送任务"}>
            <Button
              aria-label={isRunning ? "取消当前任务" : "发送任务"}
              className={isRunning ? "send-button send-button--cancel" : "send-button"}
              disabled={isRunning ? isCancelling : !canSubmit || isUploading}
              icon={isRunning ? <StopOutlined /> : <SendOutlined />}
              loading={isCancelling}
              onClick={isRunning ? onCancel : onSubmit}
              shape="circle"
              type="primary"
            />
          </Tooltip>
        </div>
      </div>
    </section>
  );
}
