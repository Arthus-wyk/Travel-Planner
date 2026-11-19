/** Export retained events once, with model inputs and outputs as separate sections. */
export function formatLogContext({ run, events, truncated }) {
  const json = value => typeof value === "string" ? value : JSON.stringify(value, null, 2);
  const sections = events.map(event => {
    const header = `[${event.timestamp}] ${event.type}${event.stage ? ` (${event.stage})` : ""}`;
    if (event.type === "model_request") {
      const { body, messages, content, ...metadata } = event;
      const { messages: bodyMessages, ...parameters } = body || {};
      return `${header}\n模型输入\n${json(messages ?? bodyMessages ?? content)}\n请求参数与记录信息\n${json({ ...metadata, ...(body ? { parameters } : {}) })}`;
    }
    if (event.type === "model_response") {
      const { body, content, tool_calls, ...metadata } = event;
      return `${header}\n模型输出\n${json(body ?? { content, tool_calls })}\n响应记录信息\n${json(metadata)}`;
    }
    return `${header}\n${json(event)}`;
  });
  return `会话：${run.sessionId}\n状态：${run.status}\n${truncated ? "注意：仅包含保留范围内的日志，较早记录可能已截断。\n" : ""}\n${sections.join("\n\n")}`;
}
