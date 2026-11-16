import React, { useState } from "react";
import { apiUrl } from "./apiUrl";

export default function XiaohongshuTest() {
  const [mode, setMode] = useState("text");
  const [value, setValue] = useState("杭州 西湖");
  const [output, setOutput] = useState("");
  const [pending, setPending] = useState(false);

  async function submit(event) {
    event.preventDefault();
    setPending(true);
    setOutput("");
    try {
      const params = new URLSearchParams({ keyword: value.trim() });
      const response = await fetch(apiUrl(`/api/xiaohongshu/search/${mode}?${params}`));
      const body = await response.text();
      let formatted = body;
      try { formatted = JSON.stringify(JSON.parse(body), null, 2); } catch { /* Show plain text responses too. */ }
      setOutput(`本地 API HTTP ${response.status}\n${formatted}`);
    } catch (error) {
      setOutput(`无法请求本地 API：${error.message}`);
    } finally {
      setPending(false);
    }
  }

  return <section style={{ padding: "1.5rem", minWidth: 0 }}>
    <h1>小红书接口测试</h1>
    <form onSubmit={submit}>
      <label>测试类型 <select value={mode} onChange={(event) => setMode(event.target.value)}>
        <option value="text">查询文本</option>
        <option value="images">查询图片</option>
      </select></label>{" "}
      <label>搜索关键词{" "}
        <input value={value} onChange={(event) => setValue(event.target.value)} required maxLength={200} />
      </label>{" "}
      <button type="submit" disabled={pending || !value.trim()}>运行测试</button>
    </form>
    <pre aria-live="polite" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
      {pending ? "正在请求本地 API…" : output}
    </pre>
  </section>;
}
