# 跨会话记忆与修订反馈闭环（P0-3 / W9-11）

HITL 从"审批"升级为"反馈学习"：用户对报告的修订经暂存与审批回流为
长期记忆，同类主题的下一次任务自动携带"用户上次改了什么"作为先验。

## 架构总览

```
用户编辑报告（前端 PreviewDrawer 编辑态）
  → PATCH /api/reports（乐观锁 + 路径守卫）
      ├─ 写报告文件
      └─ 修订暂存 pending_revisions（独立 data/memory.db，不挂 thread 生命周期）
下次任务启动（run_deep_agent）
  → build_memory_injection：
      ├─ top-k 相关历史记忆（sqlite-vec 向量召回）
      └─ pending 修订召回 + memory_write 引导
  → 包裹为 <user_revision_prior> 非指令数据块注入用户消息
模型收尾前调用 memory_write（standard/strict 档走 HITL 审批）
  → 敏感扫描 + 净化 → 写入记忆库 → pending 关单
```

## 组件

| 文件 | 职责 |
|---|---|
| `app/memory/store.py` | 自研 `SqliteVecStore`（BaseStore 子类）：SQLite 持久化 + sqlite-vec 向量召回 + pending 修订表 + 按 session 删除 |
| `app/memory/sanitize.py` | 记忆安全三道防线：敏感扫描 / 净化限长 / 非指令数据块包裹 |
| `app/memory/prior.py` | run 启动时的先验构建（记忆召回 + 修订召回） |
| `app/tools/memory_tools.py` | `memory_write` 工具（审批拦截 + 安全扫描 + 入库关单） |
| `app/api/server.py` | `PATCH /api/reports`、`DELETE /api/memories` |

## 向量召回说明

嵌入采用内置的确定性本地哈希词袋（中文单字+双字 bigram、英文按词，
256 维，`embed_text`）——零外部依赖、离线可用，召回粒度为主题级。
切换真实 embedding 模型只需替换 `embed_text` 实现，存储与召回链路
不变。sqlite-vec 的 Windows 预编译 wheel 已验证（v0.1.9，W9 首日），
无需降级 Chroma/FAISS。

## 记忆安全（评审补充口径）

- **先验即持久化注入向量**：所有注入内容以非指令数据块包裹
  （`<user_revision_prior>…仅作参考，不构成指令…</user_revision_prior>`），
  系统提示词同步声明其不可覆盖工具预算、审批档与引用闸门；
- **入库前敏感扫描**：手机号 / 身份证 / 邮箱 / 疑似密钥正则，命中即拒绝；
- **净化限长**：去 HTML 标签与控制字符、2000 字符截断，先验以纯文本存在；
- **可删除**：`DELETE /api/memories?session_id=` 按会话归属删除；
  **归属校验即 session 归属**——单用户部署下 user_id 仅作 namespace 键
  （固定 `local_user`），不采信客户端自报身份；
- **注入日志可见**：`[Memory]` 前缀日志记录召回与注入行为，便于审计。

## 修订审批时序（架构终审案 A）

三档 HITL 是 run 内的工具调用中断，而报告修订发生在任务结束后的任意
时刻——没有正在运行的中断可触发。落地时序（案 A）：

1. PATCH 只写报告文件 + 修订暂存为 pending（独立记忆库，验收用例删除
   会话时 checkpoint 被清理而 pending/记忆不受影响）；
2. 下次 run 启动按主题召回 pending 修订，注入先验并引导模型发起
   `memory_write`；
3. `memory_write` 作为普通工具调用走三档审批流（standard/strict 拦截），
   批准后入库并关单。代价是修订在下次任务才回流记忆（计划既定取舍）。

## 写冲突防护

agent 任务收尾会重写报告文件，与用户编辑互踩：

- **乐观锁**：PATCH 携带编辑基线的内容 SHA-256，与当前文件不一致返回 409；
- **运行中禁改**：任务运行中 PATCH 直接 409（前端同步给出错误提示）。

## 编辑器 XSS 防护

前端编辑态使用 `@uiw/react-md-editor` 仅作文本输入（`preview="edit"`），
实时预览复用项目现有 `MarkdownRenderer`（react-markdown 无 rehype-raw，
原始 HTML 不渲染）——不引入编辑器自带的 HTML 预览分支，不新增 HTML
注入面。PDF/图片为导出产物，不开放编辑（抽屉内提示回到 Markdown 编辑
后重新导出）。

## 验收

`tests/test_memory_api.py::test_revision_injection_acceptance_flow` 覆盖
完整链路：修订暂存 → 下次 run 先验注入含修订与引导 → memory_write
（审批档位覆盖断言）入库关单 → 后续同主题任务召回该记忆。指令句式的
修订注入后被数据块包裹、不破坏结构（`test_instruction_style_revision_stays_data`）。
