export function watchGeneration(api, planId, onJob, onError, interval = 1500) {
  let active = true, timer;
  async function poll() {
    try {
      const { job } = await api(`/api/plans/${encodeURIComponent(planId)}/generation`);
      if (!active) return;
      onJob(job);
      if (["queued", "running"].includes(job?.status)) timer = setTimeout(poll, interval);
    } catch (cause) { if (active) onError(cause); }
  }
  poll();
  return () => { active = false; clearTimeout(timer); };
}
