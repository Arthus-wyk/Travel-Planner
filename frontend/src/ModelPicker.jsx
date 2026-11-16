import React, { useEffect, useId, useState } from "react";

export default function ModelPicker({ api, model, onChange, disabled }) {
  const id = useId();
  const [models, setModels] = useState([]);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    api("/api/models", { signal: controller.signal }).then(result => {
      if (controller.signal.aborted) return;
      setModels(result.models);
      onChange(result.default);
    }).catch(cause => {
      if (!controller.signal.aborted) setError(cause.message);
    });
    return () => controller.abort();
  }, [api, onChange]);

  return <div className="model-picker">
    <label htmlFor={id}>模型</label>
    <select id={id} value={model} disabled={disabled || !models.length} onChange={event => onChange(event.target.value)}>
      {!models.length && <option value="">{error ? "模型列表加载失败" : "正在加载模型"}</option>}
      {models.map(id => <option key={id} value={id}>{id}</option>)}
    </select>
    {error && <p role="alert">{error}</p>}
  </div>;
}
