# 审核 Agent 结构化输出与委派恢复实施方案

> 执行时使用 Superpowers 的 executing-plans，按任务先复现再修改。

**目标** 让审核任务按类别并发搜索，从可修复 JSON 和总结失败中恢复，阻止失败或失效成果被引用，并减少父 Agent 输入和委派返回的重复内容。

**架构** 在模型网关归一化结构化正文，在审核子角色内根据当前工具证据恢复搜索结果，由审核父角色管理可引用成果。父 Agent 的输入和工具回复分别在进入模型消息前精简。

**技术栈** Python、httpx、jsonschema、json-repair、unittest、uv。

**需求来源** 本次请求中的六项日志问题及后续提出的多类别搜索拆分。以当前工作区的审核实现为基线，配合既有 [审核可靠性方案](2026-11-18-review-agent-reliability.md)。

## 现状与约束

- `OpenAICompatibleClient.chat` 已显式发送严格 `json_schema`，字典 Schema 使用 `jsonschema` 校验。当前审核链路未传输出 token 上限。字符列号无法判定是否触发截断，需要读取 `finish_reason` 和用量。
- `ReviewDelegations` 用运行内字典管理任务及成果。当前代码已经在修订后直接执行处理器，但失败结果也进入引用查找和反馈。先锁定一次调用完成修订的行为，再按回归结果收敛修改范围。
- 子 Agent 已在 `_remember_result` 收集成功搜索工具的证据，`_with_evidence` 已把它们保留到失败结果。补上有限的成功恢复和结果校验即可。
- 当前 `PARENT_PROMPT` 已要求按类别同批委派，`nodes.py` 也已通过 `asyncio.gather` 和信号量执行同批调用。`delegate_task` 仍接受自由文本任务，现有混合候选 HTTP 测试还用一个任务搜索三类地点。
- 保留角色白名单、同任务角色约束、工具和模型预算、调用 ID、计划版本检查、编辑试算及写入授权。取消、工具链中断和仍缺步骤的任务保留现有失败或部分状态。

## 1. 模型网关恢复

文件：`service/utils/model_client.py` 管理请求体和响应完成原因，`infrastructure/llm.py` 管理模型请求参数与重试边界，`agent/chat/context.py`、`agent/chat/nodes.py` 传递审核子角色的输出预算和结构化失败类别。依赖写入 `pyproject.toml` 与 `uv.lock`。

- [ ] 在 `tests/test_model_client.py` 用 `httpx.MockTransport` 复现缺逗号、未转义引号、Markdown 代码围栏、空白和 `finish_reason=length`。断言修复后的内容能被下游再次解析，合法 JSON 路径保持原文；语法合法但缺必填项、类型错误、顶层非对象仍失败。为截断的工具调用和拒答保留现有断言。
- [ ] OpenAI 兼容客户端的字典 Schema 正文采用 `json.loads` → 仅在语法失败时尝试 `json_repair.loads` → 原 Schema 校验。语法修复后的对象通过校验才序列化回 `message.content`。空白单独归类，拒答、内容过滤、明确的截断及工具参数继续走现有拒收分支。修复与失败诊断记录模型、阶段、结束原因、用量和错误类别，诊断字段避免引入原始正文。
- [ ] 保留已发出的严格 `json_schema` 请求并验证带工具和纯输出两种请求。先用实际配置的网关测出模型支持的请求格式，若原生 DeepSeek 或实际部署明确只能使用 JSON 模式，为该提供方或已确认的组合显式使用 `json_object`，仍用原 Schema 本地校验；保留通用客户端遇到 HTTP 400 的行为。JSON 模式下明确提示输出 JSON。参考 [DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)、[硅基流动 Chat Completions](https://docs.siliconflow.cn/docs/api/chat-completions-post) 和 [json-repair](https://github.com/mangiucugna/json_repair)。
- [ ] 增加可选的审核子角色输出上限，沿现有调用参数传入 HTTP `max_tokens`，Ollama 若启用该设置则映射到 `options.num_predict`。先记录未设置时的基线和最长编辑提案用量，再定显式值，确认它能容纳最长验收样例且不会误降部署已有上限。测试用固定值 8192 验证请求传递和未设置时的兼容行为，部署值以实测为准。
- [ ] 让 ReAct 调用端获得机器可读的最终正文失败类别（空白、解析失败、Schema 不符、正文截断），用它驱动搜索结果恢复。有效的工具调用先走原生分支。覆盖网关返回、ReAct 终止状态到子角色的整条路径，避免依赖错误字符串。此阶段的恢复在本地完成，模型重试继续走已有基础设施。

## 2. 成果引用与修订

文件：`agent/chat/review_parent.py` 管理引用解析、发布和依赖失效，`agent/chat/review_children.py` 管理当前任务版本的证据和消息。

- [ ] 在 `tests/test_review_native_delegation.py` 先补齐断言。模拟任务失败、阻塞及要求重排，检查返回的 `artifact_ref` 是 null，下一任务提交对应的 `task_id:vN` 得到 `stale_artifact`。初始化版本能引用，伪造版本、另一运行的引用、自引用和旧任务版本仍按现有契约拒绝。
- [ ] 在 `ReviewDelegations` 由已核验的 `TaskContext.snapshot.plan_version` 初始化本轮快照引用。快照引用走独立解析分支，子角色继续从 `TaskContext.snapshot` 读取快照。执行前后统一比较任务依赖的有效版本，快照引用不进入按任务 ID 失效的依赖集合。父提示允许原样引用当前快照版本或已完成任务返回的引用。
- [ ] 发布条件统一为角色正常返回已通过结果校验的 `completed`，并且依赖在执行结束时仍有效。仅这些结果进入引用解析和成功缓存。其余结果留作本轮进度和诊断，任何失败回复的 `artifact_ref` 均为 null。进度仍可呈现已验证证据，编辑交付仍需唯一有效提案及匹配试算。
- [ ] 同一任务更新条件时，沿用父层立即执行路径，先失效旧结果及传递依赖，再执行新一版。检查子角色历史消息、POI、试算、待纠错操作和证据的复用边界，确保旧版工具结果不能成为新版输入证据。保留累计额度、步骤和会话标识，现有额度允许时直接处理新版本；旧版纠错操作不得为新版增加免费调用。
- [ ] 扩展现有并发回归，断言修改目标的第一次调用就执行新搜索且成功发布 v2，任务额度无额外占用，旧版提案失效，使用旧依赖且晚完成的任务无法重新发布。覆盖预算耗尽和失败修订，确认不存在旧成功结果回流。

## 3. 按搜索目标拆分并发委派

文件：`tools/delegation.py` 声明角色专用入参，`agent/chat/review_parent.py` 定义拆分规则并传递搜索范围，`agent/chat/review_children.py` 用该范围执行搜索。

- [ ] 将独立任务定义为「搜索目标和约束已知，执行时无需读取另一个委派任务的结果」。同一天、同一城市可以共享范围。每个需要单独选数的类别或明确子类别用一个 `task_id`。第一天找 1 个公园、2 个景点、4 家餐厅时，在同一条 assistant 消息发出 `search-park`、`search-spots`、`search-food` 三个调用，`selection_count` 分别为 1、2、4，各调用的 `artifact_refs` 为空。若餐厅必须靠近用户选中的公园，等公园确定后再发餐饮任务。编辑安排等候选确定后再委派。
- [ ] 为 `role=search_places` 添加必填的结构化 `search_scope`，含单值 `category`、`day`（可空的正整数）和 `selection_count`（可空的正整数）。首批类别覆盖 `park`、`attraction`、`food`、`hotel` 及单目标扩展类别。类别用于搜索分组，公园仍可映射到当前行程的景点类型。日期未指定或数量未指定时传 null。现有 `task` 和 `facts` 用于补充同一范围的条件。子角色以结构化范围为准，候选不足时只汇报该组缺口。Schema 拒绝数组、空类别、缺字段和非法数量，其他角色沿用原有入参。
- [ ] 复用现有并发、超时与预算。三项独立搜索消耗三次委派额度，各用自己的子任务预算。父提示加入上述同批示例，要求结果按来源任务维护分组和选择数量。高德类别映射可用时，复用已有类型映射约束候选；字段不足时避免仅凭 POI 名称做硬分类。分组时保持已有的 POI 来源和跨组去重检查。
- [ ] 在 `tests/test_review_native_delegation.py` 验证三次调用同批进入处理器且类别、数量与 ID 独立；用同步事件证明处理器在任一任务结束前均已进入，避免用睡眠时间推断并发。覆盖多类别参数被工具校验拒绝、一类失败时保留其余两类、依赖选点的搜索延后执行。将 `tests/test_review_assistant_http.py` 的混合单任务夹具改成三个分类调用，校验候选分组数量以及最终一次试算和保存。
- [ ] 对真实父模型做重复烟测，检查首个可委派轮次的同批调用数量、每个子任务的搜索范围和最终分组。单值 Schema 可以约束每次调用的形状，用户自由文本里的类别覆盖率仍需通过真实模型日志检验。

## 4. 搜索总结失败时交付已验证候选

文件：`agent/chat/review_children.py` 的角色执行器与 `_parse_result`，测试放入 `tests/test_review_child_adapter.py` 与 `tests/test_review_reliability.py`。

- [ ] 模拟当前版本成功搜索到 POI，随后最终正文空白、损坏或截断。断言处理器从已收集证据按首次出现顺序去重，返回 `completed` 及匹配的 `place_ids`，父委派产生可引用成果。本地不再调用一次高德。
- [ ] 仅当 `search_places` 的最后结构化正文失败且当前版本有可信搜索结果时，从 `state.evidence["places"]` 组装固定短摘要和候选。生成的数据先通过 `child_schema`，再走 `_parse_result` 的 POI 来源校验及现有证据合并。成功表示已取得候选，若工具已证实某项约束仍未满足，通过 `remaining` 交代；父 Agent 继续负责按用户需求分组。
- [ ] 覆盖无 POI、搜索工具失败、仅有旧版证据、模型明确返回 `needs_replan`、解析成功但业务校验拒绝虚构 POI、权限错误、取消以及编辑试算失败。上述场景均不能借搜索兜底发布成果。用端到端模拟确认父 Agent 可引用恢复生成的候选，但仍需正常试算才能交付编辑。

## 5. 精简父 Agent 首条消息

文件：`agent/chat/review_parent.py` 构造父角色需要的字段，`agent/chat/runner.py` 仅在父角色构造精简模型消息。现有 `review_workflow.py` 的恢复数据和版本流程继续提供输入。

- [ ] 父消息保留一份 `original_text`、快照、页面选择、必要会话历史及 `clarification_answers`。改写成功时保留改写文本和解析引用；改写失败时以原文作为有效 `rewritten_text`，省略等值字段，父提示以原文解释缺省值。异常类别继续记录在改写诊断事件。不要把父模型消息保存成恢复数据。
- [ ] 在 `tests/test_react_loop.py` 或审核工作流测试中捕获实际首条用户消息，断言改写失败及历史恢复后没有重复的原文副本和内部 `error`，成功改写时引用仍然可见，多轮澄清答案和已选 POI 均传入模型。同步修正父提示中的字段名。

## 6. 精简委派工具消息并联合验收

文件：`agent/chat/review_parent.py` 生成委派业务数据，`agent/chat/nodes.py` 生成模型回复消息，`docs/tool-output-contracts.md` 记录委派的模型可见契约。

- [ ] 仅精简模型可见的 `delegate_task` 信封。成功回复保留 `success`、`operation_id`、`data` 和预算反馈。业务数据包含任务标识、版本、状态、摘要、成果引用及角色相关的小字段。失败回复保留 `success`、`operation_id`、必要任务元数据和 `error_type`、`message`、`retryable`，成果引用为 null，错误描述出现一次。根据实际纠错资格与剩余额度给出 `retryable`，保留 `_retry_of` 需要的 ID。类型化的内部结果和详细执行事件继续走现有记录逻辑。
- [ ] 在 `nodes.py` 以内部校验后的成功状态驱动预算、失败链和后续调用门控，再序列化模型消息，避免门控从精简后的 JSON 读取 `verification.status`。用 `tests/test_react_loop.py` 和委派回归捕获成功与失败模型消息，检查无重复错误、并发调用 ID 一一对应、纠错次数及其他工具行为正常。
- [ ] 按涉及文件运行测试，回归前后各执行一次，区分基线问题与新增失败。

  ```powershell
  uv run python -m unittest discover -s tests -p 'test_model_client.py'
  uv run python -m unittest discover -s tests -p 'test_react_loop.py'
  uv run python -m unittest discover -s tests -p 'test_request_rewrite.py'
  uv run python -m unittest discover -s tests -p 'test_review*.py'
  git diff --check
  ```

- [ ] 使用脱敏日志夹具重放缺逗号、空白、旧引用、任务修订及已验证 POI 的例子。比对首条模型消息和委派回复的长度，确认修订无需额外委派，高德查询在总结失败后无需重跑。有相应凭证时，再对部署中的模型网关做一次带原生工具的审核烟测，记录请求格式、结束原因、输出用量和最终状态。记录任何实际未运行的项目。

## 实施记录

- 输出预算通过 `REVIEW_CHILD_MAX_TOKENS` 按需配置。默认 0 继续使用提供方上限，避免把未知的部署上限压低。硅基流动保持严格 `json_schema`，显式配置原生 DeepSeek 的客户端使用 `json_object` 和本地 Schema 校验。
- 父运行的成果表只存当前版本的成功结果。非成功结果仍可出现在进度和诊断中。任务修订清除旧版消息、失败链和证据，保留已消耗的工具额度、模型步骤和会话 ID。
- 依赖用户选点的搜索通过已有澄清交互确认锚点，随后只查询依赖类别。普通候选卡片上的地点选择继续进入原有编辑试算流程。
- 硅基流动 DeepSeek-V4.1-Flash 两次合成父任务均以 `tool_calls` 结束，返回公园、景点、餐饮各一个委派，选择数量为 1、2、4，分别使用 651 和 690 个输出 token。传入 `max_tokens=8192` 的 30 项模拟编辑以 `stop` 结束，通过严格校验，输出约 1,144 token。
- 运行审核、模型客户端、ReAct、改写、聊天 API、上下文压缩和工具架构的 143 项测试，142 项通过。Python 编译检查和 `git diff --check` 通过。
- 基线已复现 `test_cli_model_selection_ignores_environment` 因仓库没有 `service.cli` 而报错。真实高德及最长生产编辑样例未运行。
