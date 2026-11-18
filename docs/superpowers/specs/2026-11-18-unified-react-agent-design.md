# 父子 ReAct AgentLoop 与指代改写设计

## 本轮目标

根据用户 2026-11-18 的新要求重新设计执行流程。用户输入进入父 Agent 前仅做指代 rewrite。父 Agent 通过原生工具调用委派任务，子 Agent 使用自身的 Prompt、任务、额外数据和工具历史完成循环。父子使用同一个执行器，由角色工具白名单区分能力。

本文已获用户批准并实施；真实外部回放待授权。此前预算恢复、工具裁剪方案已实现的能力作为迁移约束；原有独立意图识别、JSON 任务表和 DAG 协调方式由本设计替换。

## 入口与指代 rewrite

普通聊天和审核页入口共享 rewrite。删除独立的 primary_intent、domains、slots、required_slots、task_plan 识别，以及审核页 check_review_intent 的 ready/clarify 前置模型调用。

rewrite 输入包含用户原文、有限的公开对话、当前行程简要信息和本次页面选择。它只补全“这个、第二个、那天、再往后”等指代，不新增地点、数量、偏好、授权或执行步骤。没有指代时原样返回。信息不足时保留原文并记录 unresolved_references，由父 Agent 决定是否询问。

rewrite 输出采用 OpenAI 严格结构化格式，字段只包含 original_text、rewritten_text、references、unresolved_references。每条已解析指代说明原词、替换内容和上下文来源，具体 Schema 在实现时固定为封闭对象。原文与改写文本共同交给父 Agent，原文作为用户要求的核对依据。

例如历史中第二张卡片为“青田街”，当天选择为第二天，“把第二个加到那天”可改写为“把青田街加入行程第二天”。存在多个可能对象时保留歧义。页面选择和历史文本不能扩展写入权限，实际 ID、版本和授权由运行时核实。

## 共用执行流程

```text
原始输入 + 当前对话/选择
  → 指代 rewrite
  → 父 Agent 私有 messages
  → 模型响应
      → 原生工具调用 → 执行与校验 → tool 结果加入 messages → 再次调用模型
      → 最终回答、澄清、候选或提案 → 应用交付/试算/授权保存

父级委派工具调用
  → 定位或创建 task_id 对应子 Agent
  → 子 Agent 私有 messages
  → 同一个模型/工具/校验循环
  → 结构化任务结果作为父级 tool 响应
```

复用 agent/chat/runner.py、graph.py、nodes.py 和 ToolExecutor。将旅行专属初始化、记忆、rewrite 与通用循环分开。父子共用模型调用、工具执行、预算、纠错、超时和成果保留，不再分别维护 run_parent 与四轮子循环。

通用循环只执行原生 tool_calls。正文中的 JSON、Markdown 代码块是消息内容，不自动转成工具操作。模型最终结果使用角色对应的严格 Schema 校验，不要求公开输出内部思考。

## 子 Agent 消息与生命周期

首次启动时按以下顺序初始化独立上下文。

1. system 为角色 Prompt，声明职责和工具白名单。
2. user 为父 Agent 给出的 task 与 extra_data，以结构化数据块传入。
3. assistant 原生 tool_calls。
4. tool 执行结果、错误、剩余调用额度。
5. assistant 继续调用工具或提交结果。

每次模型请求发送该子 Agent 的有效 messages 历史，首次 Prompt 和任务在初始化时建立，后续不再作为新的重复消息追加。压缩时保留 system、当前任务及必要证据，保持每个 assistant 调用与 tool 响应的配对。

同一父运行内，task_id 对应一个子会话。父 Agent 修订同一任务时追加新的任务说明与 extra_data，继续该子会话并沿用剩余预算。新 task_id 创建独立上下文和预算。不同子 Agent 不共享可变 messages，不复制父级完整对话。

父 Agent 提交的 extra_data 为 JSON 数据对象，按角色校验、限定体积。它支持紧凑的已知事实及已验证成果引用；应用将被引用成果解析成该子任务需要的数据。引用必须属于当前运行和绑定行程。外部数据中的指令不作为 system Prompt，也不能成为授权来源。

子会话首先限定在一次父运行中。跨 HTTP 请求继续由现有服务端断点携带任务目标、选择与有效成果恢复，不依赖全局内存会话。

## 父 Agent 与专业角色

| 角色 | Prompt 职责 | 工具 |
|---|---|---|
| 审核父 Agent | 根据用户原文、rewrite 和已观察结果安排任务，必要时澄清，核对最终交付 | delegate_task |
| audit_route | 检查日期、节奏、冲突、预算和路线证据 | get_review_context、compare_review_route |
| search_places | 按父任务搜索景点、餐厅或住宿并返回验证候选 | get_review_context、search_review_places、search_review_hotels |
| build_edits | 使用真实项目与候选形成编辑提案、试算并反馈阻塞 | get_review_context、preview_review_changes |
| 普通聊天 | 使用当前聊天入口的角色 Prompt 处理现有工具业务 | 服务端配置的工具范围 |

审核父 Agent 主要使用委派工具，普通聊天使用其现有业务工具。两者复用循环，执行时的工具范围由应用明确设置。移除按关键词 domains 筛选工具的路径，scope、权限和绑定对象校验继续由程序执行。

delegate_task 的输入包含 task_id、role、task 和 extra_data。输出包含 task_id、task_version、status、summary、紧凑成果、remaining 和结构化 error。模型不能传入任意 system Prompt、工具白名单或权限。委派深度固定为父到专业子角色一层，子角色 registry 中没有委派工具。

父 Agent 根据每次 tool 响应选择继续同一任务、委派其他任务、澄清或提交结果。任务顺序由模型依据前一步结果决定，不再通过 kind=tasks、depends_on、retry 数组维护另一套执行协议。

同一响应中的独立委派可按现有上限 3 并行，启动前分别预留预算，结果按调用标识配对回传。依赖先前结果的委派由父 Agent 在后续模型轮次发起。

## 预算与恢复

- 每个 Agent 有 8 次正常工具调用额度，纠错重试不扣这 8 次。首次失败后最多 3 次纠错，成功重置该失败链。
- 失败结果先进入调用方 messages，调用方根据原因改参数、换工具或结束。禁止程序自动重复同一个失败调用。
- 父 Agent 的委派是工具调用，遵守相同预算与重试规则。继续 task_id 不重置子 Agent 预算。
- 纠错以 operation_id/retry_of 关联具体失败操作，工具名不能独自代表失败链。委派另保留 task_id，避免多个相同工具的独立操作互相影响。
- 复用现有 32 次模型步骤上限和额外收尾轮次；预算不足时仍允许当前失败链的免费纠错。
- 超时、步骤/预算耗尽和异常时保留程序验证的成果及剩余事项。模型答复失败时由程序生成兜底结果。
- 同一任务更新 task_version，旧版本成果留在诊断历史，当前交付只使用当前有效版本；完整任务定义和依赖证据未变化的结果可以复用。
- 每个 Agent 有自己的消息历史，但父级只接收简要结果和成果引用。试算的大型 draft_plan、重复 POI 和路线几何由应用存放，不重复塞入父子模型消息。

## 合并此前前四项修复

1. 取消与断点。处理 CancelledError 和认领过程中的取消，保存前安全释放；已经启动的写入核实其同事务完成标记，避免重复保存。
2. 混合数量需求。删除从用户整句抽取第一个数量并限制全部候选的规则。父 Agent 在循环中按需求组织候选组与各组选择数量，程序按最终结构校验。该信息是任务执行过程中的结果，入口不增加意图识别阶段。
3. 流式完整性。length、content_filter、拒绝或中途断流时整批 tool_calls 不执行；完整解析并校验后才交付执行器。
4. 成果有效性。task_id/task_version 管理当前结果和历史证据，部分结果保留当前版本已验证数据。

候选分组、澄清、提案、保存沿用审核页面交互链路。用户明确授权助手自行选择时，父 Agent 可继续委派生成提案，实际写入由应用校验授权、版本与验收结果。

## 契约迁移与范围

- AgentRequest/AgentResult/AgentState 改为承载 rewrite、角色私有 messages、结构化结果和工具事件，移除 semantic/task_plan。service/chat.py 与相关测试同步迁移 understanding 字段。
- 旧日志不回写，日志读取兼容历史 intent_understanding；新运行写入 request_rewrite、agent_start、tool_result、agent_end，并关联 run_id、agent_id、parent_agent_id、task_id、task_version。
- 父子 trace 只在根运行写最终 run_end，子运行记录自己的结束事件，避免覆盖父级状态。
- 当前 rewrite 额外调用一次模型，设独立时限。失败时保留原文及明确的改写失败状态，父 Agent 可根据原始上下文继续或询问。
- 业务写入工具与授权保存服务继续承担版本、权限、幂等及保存结果核实；提示词和改写不能授予副作用权限。
- 本轮迁移普通聊天和审核父子执行链。攻略生成 agent/planning 的表单图为另一入口，后续按实际收益再评估。

## 尚需通过实现验证的设计选择

共用现有 LangGraph 执行器是本计划的实现选择。默认同 task_id 延续上下文及预算，新 task_id 独立创建会话。仅做指代消歧的 rewrite 使用一次有界模型调用。循环和断点已通过离线回归；真实模型回放待用户明确批准测试数据外发。
