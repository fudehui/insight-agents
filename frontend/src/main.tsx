import "antd/dist/reset.css";
import { App as AntApp, ConfigProvider, theme } from "antd";
import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./styles.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ConfigProvider
      theme={{
        algorithm: theme.defaultAlgorithm,
        token: {
          colorPrimary: "#0891b2",
          colorSuccess: "#059669",
          colorWarning: "#b45309",
          colorError: "#dc2626",
          colorInfo: "#0891b2",
          colorBgBase: "#f6f8fb",
          colorBgContainer: "#ffffff",
          colorBorder: "rgba(15, 42, 67, 0.14)",
          borderRadius: 8,
          fontFamily:
            "'IBM Plex Sans', 'PingFang SC', 'Microsoft YaHei', system-ui, sans-serif",
          fontFamilyCode:
            "'JetBrains Mono', 'SFMono-Regular', Consolas, 'Liberation Mono', monospace"
        },
        components: {
          Button: {
            controlHeightLG: 46,
            primaryShadow: "0 4px 16px rgba(8, 145, 178, 0.22)"
          },
          Input: {
            activeBorderColor: "#0891b2",
            hoverBorderColor: "#059669"
          }
        }
      }}
    >
      <AntApp>
        <App />
      </AntApp>
    </ConfigProvider>
  </React.StrictMode>
);
