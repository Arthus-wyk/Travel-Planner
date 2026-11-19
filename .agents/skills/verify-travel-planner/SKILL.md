---
name: verify-travel-planner
description: 验证 Travel Planner 的新建行程、攻略生成、审核保存和行程单，或为独立代码审核收集端到端证据。
---

# 验证 Travel Planner

从仓库根目录执行。先读 AGENTS.md 和 REVIEW.md，记录本次任务范围与 Diff 基线。

## 日常回归

```powershell
uv run python -m unittest discover -s tests -v
node --test frontend/tests/*.test.js frontend/src/reviewDraft.test.js
```

## 浏览器端到端

准备监听在 127.0.0.1 的独立 MySQL 测试实例。显式设置
`TRAVEL_DB_HOST`、`TRAVEL_DB_PORT`、`TRAVEL_DB_USER`、`TRAVEL_DB_PASSWORD`。
账号需要创建和清理测试 schema 的权限。运行器生成唯一的 `travel_e2e_*`
schema 并在退出时清理。常规业务库及账户不用于此流程。

按锁文件安装前端依赖（`npm ci --prefix frontend`）。沿用仓库浏览器脚本的 Playwright 加载方式。
预先安装 Playwright 和 Chromium 并将对应的 node_modules 目录设为 `NODE_PATH`。
使用已安装的 Edge 时设置 `TRAVEL_E2E_BROWSER=msedge`。

在专用于验证的 PowerShell 会话执行：

```powershell
$env:VITE_API_BASE = '/'
$env:VITE_AMAP_JS_KEY = ''
$env:VITE_AMAP_SECURITY_CODE = ''
$env:VITE_AMAP_SERVICE_HOST = ''
npm run build --prefix frontend -- --outDir ../.uv-cache/travel-e2e-dist --emptyOutDir
if ($LASTEXITCODE -ne 0) { throw 'E2E frontend build failed' }
uv run python -m tests.travel_e2e
```

运行器启动本地 FastAPI/Uvicorn，浏览器通过真实 HTTP 和 WebSocket
操作应用，使用实际 MySQL 存取。攻略提取、模型排序和地图数据使用固定替身。
用例覆盖上海到杭州三日行程、交通选择、攻略生成、地点顺序、预算、审核修改刷新后仍保留、
确认前的访问限制，以及最终打开行程单。以脚本退出码和 `PASS travel planning browser flow` 为准。
失败时保留终端输出，使用 `logs/generation.log`、`logs/api.responses.log` 和
`logs/errors.log` 中的 plan_id、job_id 或 request_id 定位相关记录。

## 真实服务 smoke

改动影响外部集成且测试凭证已配置时，使用隔离的测试数据库和测试账号。
使用两个终端分别运行：

```powershell
uv run python api_server.py --port 8788
```

```powershell
$env:VITE_API_BASE = 'http://127.0.0.1:8788'
npm run dev --prefix frontend
```

访问 `http://127.0.0.1:8788/api/health`，再打开 Vite 输出的地址。
使用未来日期创建上海到杭州三日行程，选择往返交通，粘贴包含西湖、灵隐寺
及住宿区域的攻略。等待生成终态。检查 POI 与预期城市一致、路段和预算可读，
保存一个合理的游玩时长并刷新核对，确认后打开行程单。记录命令、结果与定位 ID。
凭证缺失、配额或外部服务不可用时记录为外部阻塞。

## 独立审核与失败回传

1. 主 Agent 把轮次记为 0，执行所需验证并保存本轮证据。
2. 按 REVIEW.md 启动新上下文的只读 Reviewer。Codex 子 Agent 使用
   `fork_turns="none"`。每轮传当前 Diff 与验证结果。
3. 验证通过且 Reviewer 给出 `pass` 时结束。外部阻塞或 Reviewer 给出
   `blocked` 时交付剩余问题。
4. 可修复失败时，主 Agent 向 Coding Agent 回传本轮编号、失败命令与退出码、
   失败断言、脱敏的日志位置、审核发现和当前任务范围。仅当轮次小于 2 时将轮次加一，
   授权一次修复。Coding Agent 返回修改后，由主 Agent 重跑失败项和受影响主链路，
   并启动新的只读 Reviewer 复审最新 Diff。
5. 第 2 轮验证或审核仍失败时停止自动修改，交付当前 Diff 与未解决项。
   计数归主 Agent 所有，切换子 Agent 或重跑命令均不重置。测试重跑会保留首次失败记录。
