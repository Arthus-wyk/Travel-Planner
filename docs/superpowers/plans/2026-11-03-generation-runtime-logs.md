# Generation Runtime Logs Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** 在现有 `/logs` 页面查看历史和新生成任务的完整诊断链路。

**Architecture:** 在读取层将现有生成文件转成运行事件，复用对话日志 API、筛选和时间线。任务编号为运行编号，按时间排序，不改业务生成行为。

**Tech Stack:** Python 标准库、unittest、FastAPI、现有 React/Vite。

**Spec:** `docs/superpowers/specs/2026-11-03-generation-runtime-logs-design.md`

## Global Constraints

- 兼容历史文件，保留对话日志、本机访问限制及脱敏。
- 固定文件名及 5 个轮转备份，每组最新 8 MiB；不新增依赖。
- 当前本地页面及日志就在此工作目录，在当前目录实现，保留可审查 diff。

## Review Focus

- 并发任务不得串日志：通过不同任务编号的夹具验证。
- 正在写入、损坏记录及轮转：验证仍能读取完整事件并报告截断。
- 密钥及项目边界：验证脱敏、符号链接拒绝、会话过滤。
- 任务终态与模型格式差异：验证失败/完成/待输入及 Ollama/OpenAI Token。
- 原有对话日志不能回归：执行现有日志和 API 测试。

### Task 1: 读取与 API

**Files:** 新建 `service/generation_logs.py`，修改 `service/runtime_logs.py`、`service/api/server.py`、`service/api/request_logging.py`；测试 `tests/test_generation_runtime_logs.py`、`tests/test_runtime_logs.py`、`tests/test_request_logging.py`。

**Interfaces:** `generation_events(project_path, job_id="") -> (events, truncated)`；`list_runs(..., job_id="")`；原有 `read_run` 返回结构不变。

- [x] 写失败测试：历史任务与地图错误按任务隔离、时间排序、脱敏；轮转与未完成块；API 任务筛选与本机限制。
- [x] 运行 `python -m unittest discover -s tests -p 'test*runtime_logs.py' -v`，确认新测试因未接入日志而失败。
- [x] 实现有界读取、事件转换、任务汇总与筛选。
- [x] 重跑上述测试，期望全部通过。

### Task 2: 页面与整体验证

**Files:** 修改 `frontend/src/RuntimeLogs.jsx`、`frontend/src/GuideGeneration.jsx`、`README.md`。

**Interfaces:** 使用 Task 1 的 `jobId`、`planId`、`source`、阶段与事件类型；URL 支持 `job_id` 筛选及已有 `run_id` 直达。

- [x] 增加任务筛选、阶段和 API/错误展示；生成界面链接至对应任务。
- [x] 执行 Python 相关测试、前端 Node 测试和生产构建。
- [x] 对真实失败任务调用读取接口验证阶段、30 个地图错误和 3 次排序请求可见；验证本地运行页面。
- [x] 审查 diff，报告验证结果和日志保留范围限制。

## Verification

- Python 全量：260 项，259 通过，1 项因 Windows 符号链接权限跳过。
- 前端 Node：18 项通过；Vite 生产构建通过。
- 新浏览器回归覆盖任务 URL/筛选、阶段、API/异常筛选及堆栈展开。
- 真实任务 adea374135994857a7937c6813cd69c6：204 条事件、34 次模型请求、96 条异常筛选事件、5 条地点位置相关错误；390px 无横向溢出，无浏览器运行错误。
- 独立审查发现异常模型字段会丢失原始响应，已补失败测试并修正；全量测试再次通过。
- 实现决定：日志查询不回写响应，防止重复复制日志挤掉历史；在当前本地目录交付可审查 diff，未提交或推送。
