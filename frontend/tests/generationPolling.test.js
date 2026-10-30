import test from 'node:test';
import assert from 'node:assert/strict';
import { watchGeneration } from '../src/generationPolling.js';

test('a refreshed page resumes an existing task and stops polling on completion', async () => {
  const seen = []; let count = 0;
  await new Promise((resolve, reject) => {
    watchGeneration(async path => {
      assert.equal(path, '/api/plans/p/generation');
      return { job: { status: ++count === 1 ? 'running' : 'completed' } };
    }, 'p', job => { seen.push(job.status); if (job.status === 'completed') resolve(); }, reject, 1);
  });
  assert.deepEqual(seen, ['running', 'completed']);
  assert.equal(count, 2);
});

test('unmounted page ignores an in flight response', async () => {
  let finish, updates = 0;
  const stop = watchGeneration(() => new Promise(resolve => { finish = resolve; }), 'p', () => updates++, () => updates++);
  stop(); finish({ job: { status: 'completed' } });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(updates, 0);
});

test('failed jobs and network errors remain readable without resubmitting generation', async () => {
  let calls = 0;
  await new Promise(resolve => watchGeneration(async () => { calls++; return { job: { status: 'failed', error: '工具未调用' } }; }, 'p', job => { assert.equal(job.error, '工具未调用'); resolve(); }, () => assert.fail()));
  await new Promise(resolve => watchGeneration(async () => { calls++; throw new Error('offline'); }, 'p', () => assert.fail(), error => { assert.equal(error.message, 'offline'); resolve(); }));
  assert.equal(calls, 2);
});
