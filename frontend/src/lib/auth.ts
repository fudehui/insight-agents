// 访问令牌存取：后端配置 APP_ACCESS_TOKEN 后，前端首次访问会弹窗补录，
// 令牌保存在 localStorage，之后每个 REST 请求带 X-Access-Token 头、
// WebSocket/下载/预览链接拼 access_token 查询参数

const STORAGE_KEY = "insight-agents.access_token";

export function getAccessToken(): string {
  return window.localStorage.getItem(STORAGE_KEY) || "";
}

export function setAccessToken(token: string): void {
  if (token) {
    window.localStorage.setItem(STORAGE_KEY, token);
  } else {
    window.localStorage.removeItem(STORAGE_KEY);
  }
}
