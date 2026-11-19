import asyncio
import json
import httpx
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from service.utils import log_paths
from service.utils.transcript_log import TranscriptLogger


class RuntimeLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / 'project'
        self.patch = patch.object(log_paths, 'PROJECTS_DIR', Path(self.temp.name) / 'logs')
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_model_attempts_and_run_share_id_and_secrets_are_redacted(self):
        from service.utils.runtime_log import trace_run
        from service.runtime_logs import list_runs, read_run
        from infrastructure.llm import request_model_with_retry, RetryConfig
        transcript = TranscriptLogger(self.project, session_id='chat-1')
        client = SimpleNamespace(chat=AsyncMock(side_effect=[
            httpx.ConnectError('Authorization: Bearer secret-token'),
            SimpleNamespace(message=SimpleNamespace(role='assistant', content='answer', tool_calls=[])),
        ]))
        async def execute():
            with trace_run(transcript, model='test', text='hello') as run:
                response, _, _ = await request_model_with_retry(
                    ollama=client, model='test', messages=[{'role': 'user', 'content': 'api_key=secret-value'}],
                    tools=[], config=RetryConfig(max_attempts=2, base_delay_seconds=0, jitter_seconds=0),
                )
                run.finish('completed', response.message.content)
        asyncio.run(execute())
        runs = list_runs(self.project)['runs']
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]['status'], 'completed')
        detail = read_run(self.project, runs[0]['runId'])
        requests = [e for e in detail['events'] if e['type'] == 'model_request']
        self.assertEqual([e['attempt'] for e in requests], [1, 2])
        self.assertEqual(len({e['requestId'] for e in requests}), 2)
        self.assertTrue(all(e['runId'] == runs[0]['runId'] for e in detail['events']))
        raw = transcript.path.read_text(encoding='utf-8')
        self.assertNotIn('secret-token', raw)
        self.assertNotIn('secret-value', raw)
        self.assertIn('answer', raw)

    def test_failed_turn_is_retained_in_merged_conversation(self):
        from service.utils.runtime_log import trace_run
        from service.runtime_logs import list_runs, read_run
        transcript = TranscriptLogger(self.project, session_id='chat-1')
        with self.assertRaises(RuntimeError):
            with trace_run(transcript, model='test', text='first'):
                raise RuntimeError('broken')
        with trace_run(transcript, model='test', text='second') as run:
            run.finish('completed', 'ok')
        failed = list_runs(self.project)['runs']
        self.assertEqual(len(failed), 1)
        events = read_run(self.project, failed[0]['runId'])['events']
        self.assertIn('RuntimeError: broken', next(e for e in events if e.get('traceback'))['traceback'])
        self.assertEqual(failed[0]['status'], 'completed')
        self.assertEqual(failed[0]['turnCount'], 2)
        self.assertEqual(len(list_runs(self.project)['runs']), 1)
        with self.assertRaises(ValueError):
            list_runs(self.project, conversation_id='../elsewhere')
        with self.assertRaises(ValueError):
            read_run(self.project, '../../secret')

    def test_reader_handles_legacy_partial_lines_and_project_boundary(self):
        from service.runtime_logs import list_runs, read_run
        transcript = TranscriptLogger(self.project, session_id='old-chat')
        transcript.record_message('user', 'old question')
        with transcript.path.open('a', encoding='utf-8') as stream:
            stream.write('{partial\n')
        TranscriptLogger(Path(self.temp.name) / 'other', session_id='other-chat').record_message('user', 'private')
        runs = list_runs(self.project)['runs']
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]['status'], 'legacy')
        self.assertEqual(len(read_run(self.project, runs[0]['runId'])['events']), 1)
        self.assertEqual(list_runs(self.project, conversation_id='missing')['runs'], [])

class ConversationGroupingTests(unittest.TestCase):
    def test_recent_activity_sort_filter_and_old_links_include_all_turns(self):
        from service.runtime_logs import list_runs, read_run
        with tempfile.TemporaryDirectory() as folder, patch.object(log_paths, 'PROJECTS_DIR', Path(folder)):
            project = Path(folder) / 'project'
            transcript = TranscriptLogger(project, session_id='active-old')
            for run_id, day, kind, fields in [
                ('a' * 32, '01', 'run_start', {'input': 'first'}),
                ('a' * 32, '01', 'run_end', {'status': 'needs_input', 'durationMs': 10}),
                ('b' * 32, '19', 'run_start', {'input': 'second'}),
                ('b' * 32, '19', 'run_end', {'status': 'completed', 'durationMs': 20}),
            ]:
                transcript.record(kind, {'timestamp': f'2026-11-{day}T12:00:00+00:00', 'runId': run_id, **fields})
            other = TranscriptLogger(project, session_id='other')
            other.record('run_start', {'timestamp': '2026-11-18T12:00:00+00:00', 'runId': 'c' * 32})
            runs = list_runs(project)['runs']
            self.assertEqual([item['sessionId'] for item in runs], ['active-old', 'other'])
            recent = list_runs(project, since='2026-11-19', until='2026-11-19')['runs']
            self.assertEqual(len(recent), 1)
            self.assertEqual(recent[0]['durationMs'], 30)
            self.assertEqual(recent[0]['turnCount'], 2)
            self.assertEqual(recent[0]['input'], 'first')
            for run_id in ('a' * 32, 'b' * 32):
                self.assertEqual(len(read_run(project, run_id)['events']), 4)
            self.assertEqual(list_runs(project, conversation_id='other')['runs'][0]['sessionId'], 'other')


class RuntimeLogApiTests(unittest.TestCase):
    def test_local_api_lists_details_and_rejects_external_origin_and_bad_ids(self):
        from fastapi.testclient import TestClient
        from service.api.server import create_app
        from service.utils.runtime_log import trace_run
        with tempfile.TemporaryDirectory() as folder, patch.object(log_paths, 'PROJECTS_DIR', Path(folder)):
            transcript = TranscriptLogger(Path.cwd(), session_id='test-chat')
            with trace_run(transcript, model='test', text='find this') as run:
                run.finish('completed', 'answer')
            with TestClient(create_app(Path('mcp_server.py'), 'test'), base_url='http://127.0.0.1', client=('127.0.0.1', 50000)) as client:
                    response = client.get('/api/logs/runs?conversation_id=test-chat')
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()['runs'][0]['input'], 'find this')
                    detail = client.get(f'/api/logs/runs/{run.run_id}')
                    self.assertEqual(detail.json()['events'][-1]['answer'], 'answer')
                    self.assertEqual(client.get('/api/logs/runs?conversation_id=..%2Fsecret').status_code, 400)
                    self.assertEqual(client.get('/api/logs/runs/' + 'f' * 32).status_code, 404)
                    self.assertEqual(client.get('/api/logs/runs', headers={'Origin': 'https://example.com'}).status_code, 403)
                    self.assertEqual(client.get('/api/logs/runs', headers={'Host': 'example.com'}).status_code, 403)

class RuntimeLogSafetyTests(unittest.TestCase):
    def test_redacts_secrets_inside_tool_json_and_headers(self):
        from service.utils.runtime_log import redact
        data = {'result': '{"api_key": "hidden-key", "client_secret": "hidden-secret", "refresh_token": "hidden-refresh", "password": "alpha beta gamma", "name": "safe"}',
                'headers': {'Authorization': 'Basic hidden-basic', 'X-Goog-Api-Key': 'hidden-google'},
                'url': 'https://service/path?access_token=hidden-access&name=safe',
                'error': 'credentials: password="words with spaces"'}
        encoded = json.dumps(redact(data))
        for secret in ('hidden-key', 'hidden-secret', 'hidden-refresh', 'beta gamma', 'words with spaces', 'hidden-basic', 'hidden-google', 'hidden-access'):
            self.assertNotIn(secret, encoded)
        self.assertIn('safe', encoded)

    def test_concurrent_turn_ids_remain_separate_in_merged_session(self):
        from service.utils.runtime_log import trace_run, record_event, current_run
        from service.runtime_logs import list_runs, read_run
        with tempfile.TemporaryDirectory() as folder, patch.object(log_paths, 'PROJECTS_DIR', Path(folder)):
            transcript = TranscriptLogger(Path.cwd(), session_id='concurrent')
            async def task(text):
                with trace_run(transcript, model='test', text=text) as run:
                    await asyncio.sleep(0)
                    record_event('marker', {'text': text})
                    if text == 'cancel':
                        raise asyncio.CancelledError()
                    run.finish('completed', text)
            async def execute():
                await asyncio.gather(task('one'), task('two'), task('cancel'), return_exceptions=True)
                self.assertIsNone(current_run.get())
            asyncio.run(execute())
            runs = list_runs(Path.cwd(), conversation_id='concurrent')['runs']
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]['turnCount'], 3)
            events = read_run(Path.cwd(), runs[0]['runId'])['events']
            self.assertEqual({e['text'] for e in events if e['type'] == 'marker'}, {'one', 'two', 'cancel'})
            self.assertEqual(len({e['runId'] for e in events}), 3)
            self.assertEqual(len([e for e in events if e.get('status') == 'cancelled']), 1)


class ChatRuntimeLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_connection_failure_is_logged_before_agent_starts(self):
        from contextlib import asynccontextmanager
        from service import chat
        from service.runtime_logs import list_runs, read_run
        @asynccontextmanager
        async def broken_connection(_):
            raise OSError('MCP unavailable')
            yield
        with tempfile.TemporaryDirectory() as folder, patch.object(log_paths, 'PROJECTS_DIR', Path(folder)), \
             patch.object(chat, 'get_conversation', return_value=({'conversation_id': 'broken-chat'}, None)), \
             patch.object(chat, 'list_messages', return_value=([], None)), \
             patch.object(chat, 'save_turn_message'), patch.object(chat, 'connect_tools', broken_connection), \
             patch.object(chat, 'rewrite_request', return_value={'original_text': 'hello', 'rewritten_text': 'hello', 'references': [], 'unresolved_references': []}):
            with self.assertRaisesRegex(OSError, 'MCP unavailable'):
                await chat.handle_chat(server_file=Path('mcp_server.py'), model='test', conversation_id='broken-chat', user_message='hello')
            runs = list_runs(Path.cwd(), conversation_id='broken-chat')['runs']
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]['status'], 'failed')
            events = read_run(Path.cwd(), runs[0]['runId'])['events']
            self.assertEqual(events[0]['type'], 'run_start')
            self.assertIn('MCP unavailable', events[-1]['traceback'])

class MergedTimelineTests(unittest.TestCase):
    def test_model_pair_is_one_entry_and_search_checks_both_halves(self):
        from service.runtime_logs import event_entries
        events = [
            {'type': 'model_request', 'timestamp': '1', 'requestId': 'a', 'messages': [{'content': 'input-only'}]},
            {'type': 'model_response', 'timestamp': '2', 'requestId': 'a', 'content': 'output-only'},
            {'type': 'model_request', 'timestamp': '3', 'requestId': 'b', 'messages': []},
        ]
        entries = list(event_entries(events))
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0][1]['type'], 'model_response')
        self.assertEqual(entries[0][2], events[0])
        found = list(event_entries(events, 'input-only'))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][0]['eventId'], entries[0][0]['eventId'])
