// 审批档位定义与本地持久化：前端三档与后端 APPROVAL_MODE_PRESETS 语义一一对应。
// 档位对"下一个任务"生效——进行中的任务不受切换影响，避免同一任务两套规则

export type ApprovalMode = "off" | "standard" | "strict";

export interface ApprovalModeOption {
  value: ApprovalMode;
  label: string;
  // 淡色解释文案：说明该档位拦什么、适合什么场景
  description: string;
}

const STORAGE_KEY = "insight-agents.approval_mode";

export const APPROVAL_MODE_OPTIONS: ApprovalModeOption[] = [
  {
    value: "off",
    label: "关闭审批",
    description: "工具直接执行，无需逐次确认，适合快速迭代"
  },
  {
    value: "standard",
    label: "标准审批",
    description: "生成文件 / 转 PDF 前暂停等待你确认（默认）"
  },
  {
    value: "strict",
    label: "严格审批",
    description: "文件、SQL 查询、网络搜索、知识库提问均需确认"
  }
];

export function isApprovalMode(value: unknown): value is ApprovalMode {
  return APPROVAL_MODE_OPTIONS.some((option) => option.value === value);
}

export function getStoredApprovalMode(): ApprovalMode {
  const stored = window.localStorage.getItem(STORAGE_KEY);
  return isApprovalMode(stored) ? stored : "standard";
}

export function storeApprovalMode(mode: ApprovalMode): void {
  window.localStorage.setItem(STORAGE_KEY, mode);
}
