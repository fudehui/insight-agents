// 默认同源相对路径：本地 dev 走 vite 的 /api、/ws 代理，Docker 部署走 nginx 同源反代；
// 置空可保证前端部署到远程服务器时不会指向浏览者自己的 localhost
const DEFAULT_API_BASE_URL = "";

function stripTrailingSlash(value: string): string {
  return value.replace(/\/+$/, "");
}

function deriveWsBaseUrl(apiBaseUrl: string): string {
  if (import.meta.env.VITE_WS_BASE_URL) {
    return stripTrailingSlash(import.meta.env.VITE_WS_BASE_URL);
  }

  if (apiBaseUrl.startsWith("https://")) {
    return apiBaseUrl.replace(/^https:\/\//, "wss://");
  }

  if (apiBaseUrl.startsWith("http://")) {
    return apiBaseUrl.replace(/^http:\/\//, "ws://");
  }

  return `${window.location.protocol === "https:" ? "wss" : "ws"}://${window.location.host}`;
}

export const API_BASE_URL = stripTrailingSlash(
  import.meta.env.VITE_API_BASE_URL || DEFAULT_API_BASE_URL
);

export const WS_BASE_URL = deriveWsBaseUrl(API_BASE_URL);
