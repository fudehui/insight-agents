# MCP 双向接入（P0-4 / W8）

慧研的内部工具已封装为标准 MCP Server，外部 Agent 宿主（Claude Code 等）
可以直接调用；同时项目用 `langchain-mcp-adapters` 验证了反向互操作。

## 工具暴露白名单（最小暴露原则）

默认只暴露 4 个工具（`app/mcp_server.py` 的 `_EXPOSED_TOOLS`）：

| 工具 | 说明 |
|---|---|
| `internet_search` | Tavily 互联网检索 |
| `list_sql_tables` | 列出业务库全部表名 |
| `get_table_data` | 预览表结构与样例数据 |
| `execute_sql_query` | 只读 SQL 查询，返回 CSV |

RAGFlow 与文件交付类（generate_markdown / convert_md_to_pdf）默认关闭、
显式开启；`read_file_content` 与沙箱 `run_code` 不封装（文件系统与代码
执行面不对外暴露）。需要调整时改 `_EXPOSED_TOOLS` 并同步更新
`tests/test_mcp_server.py` 的白名单断言。

## 两种传输

### SSE（外部宿主接入，推荐）

in-process 挂载进现有 FastAPI（架构终审案 A），与主服务共享令牌配置、
审计与事件流。**配置了 `APP_ACCESS_TOKEN` 时自动挂载于 `/mcp`**；
未配置时拒绝挂载（不继承 REST/WS"未配置即放行"的本地开发语义），
docker-compose 也不映射 MCP 端口。

鉴权接受两种形态（与 `verify_access` 同一令牌值）：

- `Authorization: Bearer <APP_ACCESS_TOKEN>`
- `?access_token=<APP_ACCESS_TOKEN>`（EventSource 等不支持自定义头的客户端）

docker-compose 场景（端口不映射到宿主时）外部宿主直连容器内网络，
或按需显式映射后再接。

### stdio（本地调试）

```bash
python -m app.mcp_server
```

Claude Code 等 MCP 客户端按 stdio 方式拉起该命令即可。注意：stdio 是
独立进程——**配额为进程内独立实例，不与主服务共享；内部任务的 thread
级预算与 ContextVar 会话上下文在此模式不适用**（4 个白名单工具均不依赖
会话目录，功能可用）。

## 配额与审计

- MCP 来源调用走 `consume_mcp_quota` 独立配额命名空间（`app/api/budget.py`），
  限额表与内部工具共用（如 `execute_sql_query` 15 次），**不做参数去重**
  （外部客户端重复调用是正常使用模式）；
- 每次调用打 `caller=mcp` 审计标记（monitor 事件），与内部会话的评测
  数据可区分，不污染内部口径。

## 依赖版本组合（重要）

```
fastmcp==3.4.7（锁 <4）
mcp==1.30.0
langchain-mcp-adapters==0.3.2
```

fastmcp 4.x 要求 `mcp>=2`，而 langchain-mcp-adapters 0.3.x 要求 `mcp<2`
（0.3.1 在 mcp 2.2 下直接 ImportError：`RequestContext` 已被移除）。
**fastmcp 3.x 是当前唯一同时满足两端互操作的版本线**，升级任一端前先
核对这张版本表。

## 互操作验证

`tests/test_mcp_server.py::test_langchain_adapters_interop_over_stdio`
经 `langchain-mcp-adapters` 以 stdio 拉起本 server，取到白名单工具并
完成一次真实 SQL 调用——外部宿主的接入方式与此完全一致。
