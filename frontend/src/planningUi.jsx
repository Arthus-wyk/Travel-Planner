import React, { useEffect, useRef, useState } from 'react';
import { useBlocker } from 'react-router-dom';
import { focusIssue } from './planningValidation';

export function useUnsavedChanges(dirty) {
  const shouldWarn = useRef(dirty);
  shouldWarn.current = dirty;
  const blocker = useBlocker(() => shouldWarn.current);
  useEffect(() => {
    if (blocker.state === 'blocked') {
      if (window.confirm('有未保存的修改。离开将放弃这些修改，是否继续？')) blocker.proceed();
      else blocker.reset();
    }
  }, [blocker]);
  useEffect(() => {
    const warn = event => { if (shouldWarn.current) { event.preventDefault(); event.returnValue = ''; } };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [dirty]);
  return () => { shouldWarn.current = false; };
}
export function BusyStatus({ task }) {
  const [seconds, setSeconds] = useState(0);
  useEffect(() => { setSeconds(0); if (!task) return; const timer = setInterval(() => setSeconds(value => value + 1), 1000); return () => clearInterval(timer); }, [task]);
  return task ? <p role="status" className="guided-note">{task}… 已等待 {seconds} 秒{seconds >= 30 && '，当前任务仍在处理中，请勿重复提交。'}</p> : null;
}
export function IssueList({ issues, title = '继续前需要处理' }) {
  const [expanded, setExpanded] = useState(false);
  if (!issues.length) return null;
  const blockers = issues.filter(x => x.blocking !== false);
  return <div className="guided-issues" aria-live="polite"><div className="guided-issues-heading"><strong>{blockers.length ? `${title} ${blockers.length} 项` : '规划提示'}</strong>{issues.length > 3 && <button type="button" aria-expanded={expanded} onClick={() => setExpanded(value => !value)}>{expanded ? '收起' : `查看全部 ${issues.length} 项`}</button>}</div><ul>{(expanded ? issues : issues.slice(0, 3)).map((item, index) => <li key={`${item.code}-${item.activityId}-${index}`}>{item.message} <button type="button" onClick={() => focusIssue(item.targetId, item.field)}>去处理</button></li>)}</ul></div>;
}
export function LoadError({ error, onRetry }) {
  return <div className="guided-error" role="alert">{error}<button type="button" onClick={onRetry}>重新读取</button><a href="/">返回首页</a></div>;
}
