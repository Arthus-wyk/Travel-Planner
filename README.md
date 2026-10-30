# Travel Planner · 智能旅行规划助手

基于 **LangGraph + 多模型服务 + MCP + MySQL + React** 的旅行规划项目。用户通过自然语言创建或调整旅行，Agent 调用工具保存需求、安排每日活动、搜索地点与酒店、计算交通路线、维护候选推荐和预算，并检查行程是否合理。

提供三种使用入口：命令行对话、React 网页、供 MCP 客户端连接的 stdio 工具服务。

## Agent 执行方式

HTTP 与 CLI 的对话流程均调用 `agent.runner.run_turn(AgentRequest, AgentContext)`，共用一张对话 LangGraph 和一条工具执行链。表单规划由另一张 LangGraph 的主 Agent 协调专业节点。旧 `service/agent/` 及双执行器已移出运行路径，不再使用临时切换环境变量。

节点顺序：准备记忆 → 理解请求 → 筛选工具 → 模型 → 单个工具 → 结果校验 → 模型 → 公开回复。历史、模型工作上下文和本轮新消息分别管理，压缩不会导致回复漏存。

代码职责和中文阅读顺序见 [Agent 核心导读](docs/agent-migration-guide.md)。表单规划另有 LangGraph 主 Agent／专业节点工作流。当前没有启用审批、跨进程恢复或多用户隔离。

## 快速开始

### 1. 安装依赖

准备 Python 3.10+、uv 和可连接的 MySQL。网页端另需满足 Vite 7 要求的 Node.js（20.19+ 或 22.12+）及 npm。在项目根目录运行：

```powershell
uv sync
```

### 2. 配置服务

在启动后端的 PowerShell 窗口中设置：

```powershell
$env:TRAVEL_DB_HOST = "127.0.0.1"
$env:TRAVEL_DB_PORT = "3306"
$env:TRAVEL_DB_USER = "root"
$env:TRAVEL_DB_PASSWORD = "your-password"
$env:TRAVEL_DB_NAME = "custom_travel_planner"
$env:AMAP_API_KEY = "your-amap-web-service-key"
```

数据库主机、端口、用户、库名的默认值如上；密码请显式设置为自己的 MySQL 密码。MCP 服务启动时自动创建数据库及 `travel_plans`、`conversations`、`messages` 表，数据库账户需要相应建库、建表权限。

模型配置写在项目根目录 `.env`，CLI 与 HTTP 共用，修改后重启后端。已有 `.env` 请保留原内容；新安装可复制 `.env.example`。

```dotenv
LLM_PROVIDER=ollama
LLM_MODEL=gpt-oss:120b-cloud
LLM_BASE_URL=https://ollama.com
LLM_API_KEY=your-ollama-api-key
```

`LLM_PROVIDER` 支持 `ollama`、`openai`、`deepseek`、`qwen`、`openai-compatible`。后三种服务及 OpenAI 使用 Chat Completions 兼容协议；不是每个模型都支持工具调用、JSON 模式或 temperature 参数，请选择支持这些能力的模型。

| 服务 | LLM_PROVIDER | LLM_BASE_URL |
| --- | --- | --- |
| Ollama Cloud / 本地 | `ollama` | `https://ollama.com` / `http://localhost:11434` |
| OpenAI | `openai` | `https://api.openai.com/v1` |
| DeepSeek | `deepseek` | `https://api.deepseek.com` |
| 通义千问 | `qwen` | 从百炼控制台复制对应地域的 OpenAI 兼容地址，末尾通常为 `/compatible-mode/v1` |
| 其他兼容服务 | `openai-compatible` | 服务商提供的 API 基础地址 |

例如切换 DeepSeek，修改 `.env`：

```dotenv
LLM_PROVIDER=deepseek
LLM_MODEL=deepseek-flash
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=your-deepseek-api-key
```

地址填写基础路径，不要追加 `/chat/completions`。所有服务均须显式填写地址，没有默认服务商、地址或模型。通义的密钥和地址须属于同一地域。协议与地址参考 [DeepSeek 文档](https://api-docs.deepseek.com/) 和 [通义文档](https://www.alibabacloud.com/help/en/model-studio/compatibility-of-openai-with-dashscope)。

相同变量名的系统环境变量优先于 `.env`。CLI 和 HTTP 入口均只从环境配置读取模型参数，不再接受 `--model`；`AGENT_MODEL`、`OLLAMA_HOST` 和 `OLLAMA_API_KEY` 已移除。`LLM_PROVIDER`、`LLM_MODEL`、`LLM_BASE_URL` 必填，缺失时报错，不会回退到 Ollama 或其他服务。OpenAI 兼容服务还必须填写 `LLM_API_KEY`；本地 Ollama 无需认证时可以留空。修改后重启后端。`.env` 已被 Git 忽略，请勿提交真实密钥。

地图功能使用高德 Web 服务 API：POI 搜索、地理编码、静态地图和路径规划。项目根目录 `.env` 已预留 `AMAP_API_KEY=`，填入高德开放平台申请的 **Web 服务类型 Key** 后重启服务；环境变量优先于 `.env`。`AMAP_TIMEOUT_SECONDS=10` 可在 `.env` 中调整请求超时。服务端密钥不应配置到前端。

### 3. 选择入口

**命令行对话**：

```powershell
uv run python client.py
# 指定日志会话标识（模型在 .env 中配置）
uv run python client.py --session-id demo-trip
```

输入 `exit` / `quit` 退出，输入 `/usage` 查看本地日志统计。`--session-id` 用于复用日志标识，不会自动恢复此前的命令行聊天上下文。

**网页端**：在已配置环境变量的窗口启动 HTTP API，另开窗口启动前端。

```powershell
# 项目根目录：后端
uv run python api_server.py
```

```powershell
# 另一个窗口：前端
cd frontend
npm ci
npm run dev
```

后端默认地址为 `http://127.0.0.1:8787`；打开 Vite 输出的网页地址（通常为 `http://127.0.0.1:5173`），点击“新建行程”填写目的地、出发地、出发日期、旅行天数、人数和预算，返回日期会自动计算。随后选择飞机／火车与酒店候选，上传攻略图片识别文字或手动添加地点，选择活动后生成逐日路线并查看审核。审核页可拖动景点调整当天顺序或移至其他天，并修改游玩时长、门票信息与注意事项；保存后重新计算路线和预算，家／酒店及交通锚点保持固定。审核通过后可打开行程单、打印或保存为 PDF，并记录机酒和门票的已购买状态与实际总价。已有行程仍可在详情页创建关联对话继续修改；聊天框支持 Ctrl+Enter / Cmd+Enter 发送。

机票、火车和酒店价格是**稳定的模拟价格**，仅供比较与预算估算，不代表余票、可预订价格或支付结果。新向导统一使用人民币；餐饮和市内交通按表单填写的每人每日预算估算，聊天新增费用也会计入并提示核对重复项。酒店和活动地点优先使用 高德 POI 候选；没有地图密钥或查询失败时，酒店显示明确标注的模拟方案，活动提供默认不选的主题建议，用户需补充具体地点，也可从攻略文字提取或手动添加。攻略图片先通过本机 RapidOCR 识别文字，再由已配置的模型按 [攻略识别 skill](.agents/skills/guide-recognition/SKILL.md) 提取地点和提示；原图不保存，文字与地点会显示供用户核对。模型不可用时提取操作会明确失败。地点间路线在地图服务可用时使用 高德地图，否则按临时通勤估算并在审核中提示。高严重程度问题会阻止确认行程。

后端地址改变时，在启动前端前设置 `VITE_API_BASE`：

```powershell
$env:VITE_API_BASE = "http://127.0.0.1:8788"
npm run dev
```

对应后端可用 `uv run python api_server.py --port 8788` 启动。构建前端使用 `npm run build`。

**MCP 工具服务**：

```powershell
uv run python server.py
```

这是 stdio 服务，需要由 MCP 客户端通过子进程连接。客户端配置可参考以下结构，替换为自己的项目路径，并向子进程传入前面的服务环境变量：

```json
{
  "mcpServers": {
    "travel-planner": {
      "command": "uv",
      "args": ["--directory", "D:/Python_Program/Travel-Planner", "run", "python", "server.py"]
    }
  }
}
```

项目还声明了 `travel-agent`、`travel-mcp`、`travel-api` 三个脚本入口；上面的 Python 文件命令可直接用于当前仓库。

## 重点模块：做什么、亮点与使用方式

### 1. Agent 核心与执行护栏

代码：[agent/runner.py](agent/runner.py)、[agent/graph.py](agent/graph.py)、[agent/nodes.py](agent/nodes.py)。

`run_turn` 接受明确请求和运行资源，返回 `AgentResult`。图控制步骤，模型选择具体工具；支持原生及嵌入 JSON 工具调用、最多 8 轮决策和空承诺护栏。异常携带已完成产物，取消仍保持取消语义，不自动重跑写入。

### 2. 意图理解与工具路由

代码：[agent/intent.py](agent/intent.py)、[agent/prompts.py](agent/prompts.py)、[agent/router.py](agent/router.py)。

当前计划由调用方明确提供，不从系统提示反向猜测；规则提取目标、领域和槽位，模糊指代交给拥有完整历史的模型。意图不生成第二套执行流程。HTTP 和 CLI 使用相同路由，专业 Agent 可通过 `allowed_tools` 限制能力。

### 3. 旅行计划与需求管理

代码：[tools/plan/](tools/plan/)、[service/mcp/server.py](service/mcp/server.py)。

- **做什么**：创建、修改、查询、汇总、列出和删除旅行计划，维护目的地、日期、人数、预算、兴趣、约束、节奏等信息。
- **亮点**：将高层需求与具体日程分开保存；一个计划可关联多段网页对话，方便持续调整。
- **怎么用**：先调用 `create_travel_plan`，后续调用传回的 `plan_id`。自然语言示例：“创建杭州三日游，2026-11-06 到 2026-11-08，两个人，总预算 3000 元，喜欢美食和博物馆，节奏轻松。”

### 4. 日程安排、路线协调与确定性审计

代码：[tools/itinerary/](tools/itinerary/)、[service/validation.py](service/validation.py)、[service/travel/itinerary_audit.py](service/travel/itinerary_audit.py)。

- **做什么**：增删改查日程、跨天移动活动、保存路线，并检查时间、强度、餐饮、预算和预订风险。
- **亮点**：开始时间要求为 24 小时制 `HH:MM`，日程按时间排序；基于活动时长检查重叠。删除或移动日程会清理关联的旧路线。`reconcile_travel_plan` 可按受影响日期或活动重新计算相邻活动的交通，结合审计提示交通缓冲不足。
- **怎么用**：用 `add_itinerary_item` 添加活动，`day` 从 1 开始；变更后调用 `reconcile_travel_plan`，也可单独调用 `audit_travel_plan`。审计返回 `issues`、严重程度和 `suggestions`，路线失败记录在 `route_errors` 中。

审计提供建议，不会自动修复所有问题。预算检查使用已记录的估算费用，不代表实际报价或支付金额。

### 5. 高德地图：地点搜索、地理编码、静态地图与路线

代码：[tools/maps/](tools/maps/)、[service/integrations/travel_amap.py](service/integrations/travel_amap.py)。

- **做什么**：搜索景点、餐厅、酒店，计算地点间的距离和交通时长。
- **亮点**：酒店、景点、餐饮使用 POI 关键词搜索；传入中心坐标时使用周边搜索。路径规划自动解析起终点地址和公交城市编码，并转换成现有行程审核所需的米数与秒数。
- **怎么用**：配置地图密钥后，调用 `amap_search_places`、`amap_search_hotels`、`amap_compute_route`，或输入“找西湖附近的酒店，计算到浙江省博物馆的公共交通时间”。可通过 `AMAP_TIMEOUT_SECONDS` 调整超时，默认 10 秒。

另外提供 `amap_geocode(address, city)` 和 `amap_static_map(center, zoom, width, height, markers)`。静态地图返回 `image_data_url`，由后端请求图片，结果不包含密钥；支持最多 10 个地点标记，图片最大 1024×1024，缩放级别 1–17。地图工具名称已由 `google_maps_*` 改为 `amap_*`，外部调用方需同步更新。

地点选择中，每个地点都可通过“搜索 POI，确定具体位置”查询候选并确认。确认后保存高德 POI 信息、地址解析结果、GCJ-02 坐标和地图图片引用。地图是地点位置示意，不是景点实景照片。

生成路线后进入独立页面 `/plans/:planId/review`：顶部显示当天路线地图，下方以 7:3 展示当天行程和“未确定的行程”。点击日期导航切换行程；景点卡片可拖动排序、暂存到右侧，再切换日期拖入当天，或使用卡片按钮完成相同操作。家、酒店起终点和已选车次固定。每段市内路线可选择步行、公交、驾车，并按返回的交通耗时重排游玩开始时间；也可在卡片内修改开始时间、停留时长和门票预算。无法计算的路段会标注临时估算，不绘制虚假的直线导航；未安排完的地点会出现在审核提示中。

高德响应缓存在 MySQL `map_api_cache` 表，首次访问时自动建表：POI、地址解析和静态图缓存 30 天，路径规划缓存 24 小时；相同接口与参数优先读库，失败和空搜索结果不缓存。图片通过后端 `/api/maps/images/:key` 读取，行程 JSON 只保存引用，历史图片引用过期后仍可读取，重新请求地图时更新缓存。路线缓存可能滞后于实时路况，出行前请核对。静态图最多标记 10 个地点和绘制 4 段不连续路线，完整地点和路段保留在行程列表中。路线选择只更新交通时间，费用仍按现有每日交通预算核算。

交通卡片内可填写实际出发站与到达站（或机场），车站接驳同样支持步行、公交、驾车并计入时间审核。尚未确定具体站点时会标注按城市位置估算，不把城市中心的计算结果当成准确接驳路线；当天游玩地图展示目的地内的接驳和景点路线，不把跨城车次画成市内导航直线。

接口清单与官方文档：[POI 搜索](https://lbs.amap.com/api/webservice/guide/api/search/)（`/v3/place/text`、`/v3/place/around`）、[地理编码](https://lbs.amap.com/api/webservice/guide/api/georegeo)（`/v3/geocode/geo`，坐标输入的公交路线通过 `/v3/geocode/regeo` 获取城市编码）、[静态地图](https://lbs.amap.com/api/webservice/guide/api/staticmaps)（`/v3/staticmap`）、[路径规划 2.0](https://lbs.amap.com/api/webservice/guide/api/newroute)（`/v5/direction/driving`、`walking`、`bicycling`、`electrobike`、`transit/integrated`）。

地点和坐标以中国境内、高德 GCJ-02 坐标为准，经度在前、纬度在后；旧 Google 地点 ID 和坐标不能直接当成高德数据复用。`region_code` 现在接受中文城市名、citycode 或 adcode；`language_code` 与 `units` 为调用兼容参数，距离始终返回米，`TWO_WHEELER` 对应电动自行车。驾车避让选项使用高德“少收费/不走高速”策略，“少收费”不保证零收费；`avoid_ferries=True` 会明确报不支持。酒店报价仍为模拟价格。

### 6. 候选推荐、可解释评分与预算明细

代码：[tools/recommendation/](tools/recommendation/)、[service/travel/recommendation_ranker.py](service/travel/recommendation_ranker.py)。

- **做什么**：保存尚未排入日程的候选酒店、餐厅、景点等，更新推荐，记录交通、住宿、餐饮等预算项。
- **亮点**：`rank_recommendations` 使用规则综合评分、评价数量、价格档位、类别、营业状态及偏好关键词；返回分数、推荐理由 `reasons` 和取舍 `tradeoffs`，方便解释选择。
- **怎么用**：搜索候选后调用 `rank_recommendations` 排序，再用 `add_recommendation` 保存；确定后再安排日程。用 `add_budget_item` 记录费用，用 `summarize_travel_plan` 或审计查看费用与覆盖情况。

### 7. 长期偏好与中期记忆

代码：[agent/memory.py](agent/memory.py)、[service/storage/memory_store.py](service/storage/memory_store.py)、[tools/memory/](tools/memory/)。

- **做什么**：在当前对话之外，保留稳定的旅行偏好和旅行中的明确要求，供后续理解需求时参考。
- **亮点**：长期偏好使用可阅读的 Markdown，按兴趣、酒店、餐厅、节奏等类别去重追加；模型提取记忆时区分稳定偏好与单次旅行要求，并提示当前明确要求优先。
- **怎么用**：输入“我平时喜欢安静的酒店，旅行节奏要慢一些”，或用 `update_travel_preference_profile` 显式写入偏好；`read_travel_preference_profile` 查看画像。输入“这次不要参考历史”会跳过本次长期和中期记忆的使用及后续记忆提取。

文件位于 `data/memory/travel_preference_profile.md` 和 `data/memory/mid_term_memory.jsonl`。中期读取默认取最近 30 条，当前没有按 `plan_id` 过滤；“不要参考历史”不会删除已有文件，也不会清空当前对话。

### 8. 工具契约、领域目录与执行策略

代码：[tooling/](tooling/)、[tools/catalog.py](tools/catalog.py)、[agent/executor.py](agent/executor.py)。

只有一个 `ToolRegistry`。MCP 定义适配成 `ToolSpec`，所有调用经过 `ToolExecutor` 的工具范围、权限和 JSON Schema 校验。MCP `isError` 与业务失败都进入结构化 `ToolResult`，不能被外层传输成功覆盖。

注册异步 handler 即可添加本地能力；未来专业 Agent 也可用 handler 调用同一个 `run_turn`。风险元数据用于记录和后续审批设计，当前没有确认界面或可由客户端设置的批准标志。

### 9. MySQL 持久化与基础设施适配

代码：[service/storage/travel_db.py](service/storage/travel_db.py)、[infrastructure/](infrastructure/)、[service/config.py](service/config.py)。

- **做什么**：保存旅行计划、关联对话、消息顺序及工具信息，提供数据库与外部服务适配入口。
- **亮点**：启动时初始化表结构；网页消息按会话和序号读取；计划与对话持久化后可在服务重启后继续使用。
- **怎么用**：配置 `TRAVEL_DB_*` 并启动服务即可。开发代码可通过 `infrastructure.repositories` 访问仓储函数，通过 `infrastructure.external_clients` 获取地图和模型客户端。

### 10. HTTP API 与 React 网页

代码：[service/api/server.py](service/api/server.py)、[frontend/src/App.jsx](frontend/src/App.jsx)、[frontend/package.json](frontend/package.json)。

- **做什么**：用 Python 标准库 HTTP 服务连接浏览器与 Agent；React 展示行程列表、日程时间线、路线、推荐、约束和持久化对话。
- **亮点**：聊天与旅行计划关联，创建计划后可直接跳转详情；前端无需直接连接 MCP 或 MySQL。
- **怎么用**：按快速开始启动前后端；通过 `/api/health` 检查 HTTP 服务响应。网页聊天当前等待完整 JSON 响应，未使用流式展示。

| HTTP 接口 | 用途 |
| --- | --- |
| `GET /api/health` | 查看服务状态与模型配置 |
| `GET /api/plans`、`GET /api/plans/{plan_id}` | 查看计划列表、详情 |
| `GET /api/conversations`、`GET /api/plans/{plan_id}/conversations` | 查看全部或某个计划的对话 |
| `GET /api/conversations/{conversation_id}` | 查看对话信息 |
| `GET /api/conversations/{conversation_id}/messages` | 查看对话消息 |
| `POST /api/conversations` | 创建对话，可传入 `plan_id` |
| `POST /api/conversations/{conversation_id}/messages` | 发送消息并运行 Agent |
| `POST /api/chat` | 通用聊天入口 |

例如在 PowerShell 中发送首次请求，后续传入返回的 `conversation_id` 继续对话：

```powershell
$body = @{ message = "创建杭州三日游，2026-11-06 到 2026-11-08，两人预算3000元" } | ConvertTo-Json
Invoke-RestMethod -Uri "http://127.0.0.1:8787/api/chat" -Method Post -ContentType "application/json; charset=utf-8" -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
```

### 11. 请求重试、上下文压缩与结果验证

代码：[infrastructure/llm.py](infrastructure/llm.py)、[agent/compress.py](agent/compress.py)、[tooling/results.py](tooling/results.py)、[service/travel/tool_verification.py](service/travel/tool_verification.py)。

模型适配处理退避、备用模型、认证恢复、流式降级和遥测；Agent 提供上下文压缩策略。旅行规则负责审计数据完整性、空地图结果和路线刷新问题。

每次工具操作有 `operation_id`。修正参数时模型可用内部 `_retry_of` 关联失败操作；该字段在执行前被移除，不改变 MCP 参数。一个活动的成功不会清除另一个活动的失败。

上下文压缩有损，但仅修改模型工作消息；持久化始终使用独立的 `new_messages`。

### 12. 对话日志、API 遥测与用量统计

前端侧栏的“运行日志”（`/logs`）可按会话 ID、运行状态和 UTC 日期筛选。对话页可查看当前会话的日志，新消息下的“查看本次运行日志”可直接定位某次回答。选择运行后，时间线展示实际模型请求上下文与工具定义、模型响应、重试错误、工具参数与结果、意图分析和最终状态；展开事件可查看完整 JSON，顶部汇总耗时和模型 Token。点击“刷新”读取最新进度；无结束事件的运行会显示“运行中 / 未收尾”。

每次 HTTP 或 CLI Agent 任务生成独立 `run_id`，HTTP 响应和持久化消息的 metadata 同时保存该 ID。连接失败、执行异常和取消会记录结束状态及异常堆栈。日志继续保存为现有项目目录下的 JSONL，API 遥测也会同步到运行时间线；未含运行 ID 的旧日志按会话合并展示，不能补回旧请求的上下文。

只读接口：`GET /api/logs/runs?conversation_id=...&status=...&since=YYYY-MM-DD&until=YYYY-MM-DD`、`GET /api/logs/runs/{run_id}?conversation_id=...`。接口仅允许本机客户端和本机页面访问，只读取当前工作目录对应项目的日志；查询非法 ID 返回 400，缺失运行返回 404。诊断读取限最近 100 个会话、每会话末尾 5000 行，列表最多返回 200 次运行，截断时页面会提示；按会话查询可读取该会话。未额外安装日志服务或数据库。

日志会保存模型可见的对话、记忆和工具结果。写入及读取时会脱敏常见凭据字段、Bearer/Basic 认证值和文本中的密钥赋值，但不替代个人信息清理；请保护本地日志文件。完整请求上下文会增加磁盘占用，目前不自动轮转或删除。

代码：[service/utils/transcript_log.py](service/utils/transcript_log.py)、[service/utils/api_telemetry.py](service/utils/api_telemetry.py)、[service/utils/usage_stats.py](service/utils/usage_stats.py)、[service/utils/log_paths.py](service/utils/log_paths.py)。

- **做什么**：记录对话、意图、工具调用与结果，另行记录模型请求耗时、重试、错误及可用的 token 用量。
- **亮点**：对话过程和 API 诊断分开记录；`/usage` 扫描会话日志并使用缓存生成统计，便于排查调用问题。
- **怎么用**：运行命令行时查看打印的 `Transcript` 路径；输入 `/usage` 查看统计。日志默认根目录为用户目录的 `.claude`，设置 `TASK_PROGRESS_LOG_HOME` 可调整位置。统计反映本地记录，不等同于云端账单。

## MCP 工具一览

工具统一由 [tools/catalog.py](tools/catalog.py) 注册，目前共 24 个。

| 业务领域 | 工具 |
| --- | --- |
| 旅行计划 | `create_travel_plan`、`update_travel_profile`、`get_travel_plan`、`summarize_travel_plan`、`list_travel_plans`、`delete_travel_plan` |
| 日程与审计 | `add_itinerary_item`、`update_itinerary_item`、`delete_itinerary_item`、`move_itinerary_item`、`list_itinerary`、`update_itinerary_routes`、`reconcile_travel_plan`、`audit_travel_plan` |
| 地图 | `amap_search_places`、`amap_search_hotels`、`amap_compute_route`、`amap_geocode`、`amap_static_map` |
| 推荐与预算 | `add_recommendation`、`update_recommendation`、`rank_recommendations`、`add_budget_item` |
| 偏好与验证 | `read_travel_preference_profile`、`update_travel_preference_profile`、`verify_last_tool_result` |

一个典型的工具使用顺序是：创建计划 → 搜索候选 → 评分和保存推荐 → 添加日程与预算 → 协调路线 → 审计 → 返回摘要。修改已有行程时，使用已有 `plan_id` 和 `item_id`，检查变更结果及审计意见。

## 项目结构与阅读顺序

```text
agent/                         # 唯一执行核心
  runner.py                    # 公开请求入口与结果
  state.py / context.py        # 数据契约 / 本轮资源和工具范围
  graph.py / nodes.py          # 流程连接 / 节点实际逻辑
  intent.py / prompts.py       # 意图 / 提示
  router.py / executor.py      # 工具选择 / 唯一执行入口
  memory.py / compress.py      # 记忆策略 / 压缩规则
  tool_calls.py                # 模型工具调用和重试关联解析
service/
  chat.py / cli.py             # HTTP 聊天用例 / 命令行交互
  api/                         # HTTP 路由、JSON、状态码
  travel/                      # 行程审计、推荐排序、领域校验
  storage/                     # MySQL、Markdown、JSONL 存储
  mcp/                         # 现有旅行工具业务实现
infrastructure/                # 模型和 MCP 连接等外部适配
tooling/                     # 工具契约、唯一注册表、结果规范
tools/                       # 对外 MCP 工具目录
tests/                       # 行为回归、协议快照和依赖边界
```

建议从 `agent/state.py` 和 `agent/runner.py` 开始，再看 `graph.py` 与 `nodes.py`，最后阅读工具适配和应用服务。旅行算法和数据存储不反向依赖 Agent。

历史迁移计划保留在 `docs/superpowers/plans/`。旧源码在忽略的 `data/` 中留有本地备份，运行时不会导入。

## 检查与测试

现有测试使用 Python 标准库 `unittest`：

```powershell
uv run python -m unittest discover -s tests -v
```

测试覆盖时间排序与冲突、删除/移动后的路线处理、协调与审计、空洞执行承诺护栏、工具注册及策略检查。它们不能代替 MySQL、Ollama 和 高德地图 的真实服务联调。
# 攻略直接生成每日行程

选择往返交通后，在规划页上传攻略图片或粘贴文字，点击“生成每日行程”。系统通过模型的真实地图工具调用查找地点；多候选由模型结合地图类别和攻略语境选择，ID 和坐标由服务端绑定。生成完成后直接打开每日行程草稿，未定位地点可在结果页修正或移除，再重新生成。

生成接口为 `POST /api/plans/{plan_id}/generation`，状态查询为同路径 GET。任务保存在 MySQL `planning_jobs` 表，重复提交进行中的相同输入不会启动第二个任务；提交结果时校验原计划，编辑过的计划不会被旧生成结果覆盖。

使用现有 API 启动入口运行单进程。启动时遗留的生成任务会标记失败，原计划保留，可手动重试；本版本不支持跨多个 API worker 恢复任务。OpenAI 兼容模型需支持严格 JSON Schema 和 function calling；Ollama 使用原生工具调用并由服务端校验，未返回合法工具调用时不会执行地图请求。
