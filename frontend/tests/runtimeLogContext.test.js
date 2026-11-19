import test from 'node:test';
import assert from 'node:assert/strict';
import { formatLogContext } from '../src/runtimeLogContext.js';

test('copies all turns, model context, tools and truncation notice once per event', () => {
  const text = formatLogContext({ run: { sessionId: 'chat', status: 'completed' }, truncated: true, events: [
    { type: 'model_request', body: { model: 'test', messages: [{ role: 'system', content: 'system-context' }, { role: 'user', content: 'first-question' }] } },
    { type: 'model_response', content: 'followup-question', requestId: 'call-123', runId: 'turn-1', attempt: 2, finishReason: 'length', usage: { inputTokens: 25 }, durationMs: 500 },
    { type: 'tool_result', result: { name: 'place-result' } },
    { type: 'model_request', messages: [{ role: 'user', content: 'second-answer' }] },
  ] });
  for (const value of ['system-context', 'first-question', 'followup-question', 'place-result', 'second-answer', '截断', 'call-123', 'turn-1', 'length', 'inputTokens', 'durationMs', 'attempt']) assert.ok(text.includes(value));
  assert.equal(text.split('system-context').length - 1, 1);
});
