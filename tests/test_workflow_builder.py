import importlib.util
import asyncio
import json
from pathlib import Path
import sys
import threading
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = 'comfyart_builder_test'
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = package
spec = importlib.util.spec_from_file_location(PACKAGE + '.workflow_builder', ROOT / 'workflow_builder.py')
builder = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = builder
spec.loader.exec_module(builder)
REAL_SCHEMA_RECORD = builder.node_catalog.schema_record


class WorkflowBuilderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.inventory = ({'EmptyImage': object(), 'SaveImage': object()}, {})
        self.graph = {'1': {'class_type': 'EmptyImage', 'inputs': {'width': 64}},
                      '2': {'class_type': 'SaveImage', 'inputs': {'images': ['1', 0]}}}
        self.schemas = {
            'EmptyImage': {'input': {'required': {'width': ['INT']}}, 'output': ['IMAGE']},
            'SaveImage': {'input': {'required': {'images': ['IMAGE']}}, 'output': [], 'output_node': True}}
        self.enterContext(patch.object(builder.node_catalog, 'snapshot', return_value=self.inventory))
        self.enterContext(patch.object(builder.node_catalog, 'schema_record', side_effect=lambda n, _i: dict(self.schemas.get(n, {}))))
        self.enterContext(patch.object(builder.CONFIG_STORE, 'resolved', return_value={'provider': 'strata', 'model': 'test'}))

    async def test_uses_existing_provider_and_returns_validated_graph(self):
        configs = []
        async def events(config, messages, tools):
            configs.append(config)
            yield {'type': 'tool_call', 'id': 'done', 'name': 'submit_workflow', 'arguments': json.dumps({'workflow': self.graph})}
        with patch.object(builder.providers, 'chat_events', events):
            result = [event async for event in builder.build_workflow('make an image', 'generate', '@size')]
        self.assertEqual(result[-1]['workflow'], self.graph)
        self.assertEqual(configs[0]['provider'], 'strata')

    async def test_bad_connection_is_repaired_before_return(self):
        invalid = {'1': {'class_type': 'SaveImage', 'inputs': {'images': ['missing', 0]}}}
        seen_errors = []
        async def events(config, messages, tools):
            if len(messages) == 2:
                graph = invalid
            else:
                seen_errors.append(messages[-1]['content'])
                graph = self.graph
            yield {'type': 'tool_call', 'id': 'submit', 'name': 'submit_workflow', 'arguments': json.dumps({'workflow': graph})}
        with patch.object(builder.providers, 'chat_events', events):
            result = [e async for e in builder.build_workflow('build', 'generate', '')]
        self.assertEqual(result[-1]['workflow'], self.graph)
        self.assertIn('missing source', seen_errors[0])

    def test_uninstalled_model_option_is_rejected(self):
        self.schemas['EmptyImage']['input']['required']['width'] = [[64, 128]]
        self.graph['1']['inputs']['width'] = 999
        with self.assertRaisesRegex(ValueError, 'installed option'):
            builder.validate_workflow(self.graph, self.inventory)

    async def test_doc_search_uses_existing_knowledge_base(self):
        requests = []
        async def events(config, messages, tools):
            if len(messages) == 2:
                yield {'type': 'tool_call', 'id': 'kb', 'name': 'search_docs', 'arguments': '{"query":"image example"}'}
            else:
                yield {'type': 'tool_call', 'id': 'submit', 'name': 'submit_workflow', 'arguments': json.dumps({'workflow': self.graph})}
        def search(query, limit):
            requests.append((query, limit))
            return [{'text': 'example'}]
        with patch.object(builder.providers, 'chat_events', events), patch.object(builder.kb, 'search', search):
            result = [e async for e in builder.build_workflow('build', 'generate', '')]
        self.assertEqual(requests, [('image example', 0)])
        self.assertEqual(result[-1]['type'], 'result')

    async def test_docs_reach_model_as_complete_structured_results(self):
        docs = [{'text': '\u753b\u50cf "quoted" \\ ' * 5000, 'source': 'fixture.md', 'index': i} for i in range(9)]
        received = []
        async def events(config, messages, tools):
            if len(messages) == 2:
                yield {'type': 'tool_call', 'id': 'kb', 'name': 'search_docs',
                       'arguments': '{"query":"image", "limit":9}'}
            else:
                received.append(json.loads(messages[-1]['content']))
                yield {'type': 'tool_call', 'id': 'submit', 'name': 'submit_workflow',
                       'arguments': json.dumps({'workflow': self.graph})}
        with patch.object(builder.providers, 'chat_events', events), \
                patch.object(builder.kb, 'search', return_value=docs) as search:
            result = [e async for e in builder.build_workflow('build', 'generate', '')]
        search.assert_called_once_with('image', 9)
        self.assertEqual(received, [docs])
        self.assertEqual(result[-1]['workflow'], self.graph)

    async def test_build_metrics_exclude_prompts_and_report_phase_totals(self):
        async def events(config, messages, tools):
            yield {'type': 'tool_call', 'id': 'done', 'name': 'submit_workflow',
                   'arguments': json.dumps({'workflow': self.graph})}
        with patch.object(builder.providers, 'chat_events', events), patch.object(builder, 'debug_log') as log:
            _ = [e async for e in builder.build_workflow('private prompt', 'generate', 'private rules')]
        event = log.call_args.kwargs
        self.assertEqual(event['outcome'], 'success')
        self.assertEqual(event['entry_point'], 'bridge')
        self.assertEqual(event['counts']['model_rounds'], 1)
        self.assertGreaterEqual(event['phases_ms']['validation'], event['phases_ms']['schema'])
        self.assertGreaterEqual(event['elapsed_ms'], 0)
        self.assertNotIn('private', json.dumps(event))

    async def test_batched_schemas_preserve_order_and_individual_errors(self):
        received = []
        def schema(name, inventory):
            if name == 'Broken':
                raise RuntimeError('extension failed')
            return {'name': name, 'available': name != 'Missing', **self.schemas.get(name, {})}
        async def events(config, messages, tools):
            if len(messages) == 2:
                self.assertTrue(any(t['function']['name'] == 'get_node_schemas' for t in tools))
                yield {'type': 'tool_call', 'id': 'batch', 'name': 'get_node_schemas',
                       'arguments': '{"node_types":["EmptyImage","Broken","Missing","EmptyImage"]}'}
            else:
                received.extend(json.loads(messages[-1]['content']))
                yield {'type': 'text', 'text': json.dumps(self.graph)}
        with patch.object(builder.providers, 'chat_events', events), \
                patch.object(builder.node_catalog, 'schema_record', side_effect=schema):
            _ = [event async for event in builder.build_workflow('build', 'generate', '')]
        self.assertEqual([r['node_type'] for r in received], ['EmptyImage', 'Broken', 'Missing', 'EmptyImage'])
        self.assertEqual(received[0]['schema'], received[3]['schema'])
        self.assertEqual(received[1]['error'], 'extension failed')
        self.assertFalse(received[2]['schema']['available'])

    async def test_independent_reads_overlap_and_keep_result_order(self):
        loop = asyncio.get_running_loop()
        both_started = asyncio.Event()
        release = threading.Event()
        started = set()
        lock = threading.Lock()
        received = []
        def schema(name, inventory):
            with lock:
                started.add(name)
                if len(started) == 2:
                    loop.call_soon_threadsafe(both_started.set)
            release.wait()
            return dict(self.schemas[name])
        async def events(config, messages, tools):
            if len(messages) == 2:
                for ident, name in [('first', 'EmptyImage'), ('second', 'SaveImage')]:
                    yield {'type': 'tool_call', 'id': ident, 'name': 'get_node_schema',
                           'arguments': json.dumps({'node_type': name})}
                yield {'type': 'tool_call', 'id': 'failed', 'name': 'search_docs', 'arguments': '{}'}
            else:
                received.extend(m for m in messages if m['role'] == 'tool')
                yield {'type': 'text', 'text': json.dumps(self.graph)}
        with patch.object(builder.providers, 'chat_events', events), \
                patch.object(builder.node_catalog, 'schema_record', side_effect=schema), \
                patch.object(builder.kb, 'search', side_effect=RuntimeError('KB unavailable')):
            async def consume():
                return [e async for e in builder.build_workflow('build', 'generate', '')]
            task = asyncio.create_task(consume())
            try:
                await asyncio.wait_for(both_started.wait(), 2)
            finally:
                release.set()
                result = await task
        self.assertEqual([r['tool_call_id'] for r in received], ['first', 'second', 'failed'])
        self.assertEqual(json.loads(received[2]['content']), {'error': 'KB unavailable'})
        self.assertEqual(result[-1]['workflow'], self.graph)

    async def test_submission_is_a_barrier_and_later_reads_do_not_start(self):
        async def events(config, messages, tools):
            yield {'type': 'tool_call', 'id': 'done', 'name': 'submit_workflow',
                   'arguments': json.dumps({'workflow': self.graph})}
            yield {'type': 'tool_call', 'id': 'later', 'name': 'search_docs', 'arguments': '{}'}
        with patch.object(builder.providers, 'chat_events', events), patch.object(builder.kb, 'search') as search:
            result = [e async for e in builder.build_workflow('build', 'generate', '')]
        search.assert_not_called()
        self.assertEqual(result[-1]['workflow'], self.graph)

    async def test_cancellation_closes_provider_and_reports_cancelled(self):
        started, closed = asyncio.Event(), asyncio.Event()
        async def events(config, messages, tools):
            try:
                started.set()
                await asyncio.Event().wait()
                yield {'type': 'text', 'text': 'never reached'}
            finally:
                closed.set()
        async def consume():
            return [e async for e in builder.build_workflow('private', 'generate', '')]
        with patch.object(builder.providers, 'chat_events', events), patch.object(builder, 'debug_log') as log:
            task = asyncio.create_task(consume())
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(closed.is_set())
        self.assertEqual(log.call_args.kwargs['outcome'], 'cancelled')

    async def test_cancellation_during_reads_never_reaches_submission(self):
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release, finished = threading.Event(), threading.Event()
        def search(*args):
            loop.call_soon_threadsafe(started.set)
            try:
                release.wait()
                return []
            finally:
                finished.set()
        async def events(config, messages, tools):
            yield {'type': 'tool_call', 'id': 'read', 'name': 'search_docs', 'arguments': '{}'}
            yield {'type': 'tool_call', 'id': 'done', 'name': 'submit_workflow',
                   'arguments': json.dumps({'workflow': self.graph})}
        async def consume():
            return [e async for e in builder.build_workflow('build', 'generate', '')]
        with patch.object(builder.providers, 'chat_events', events), \
                patch.object(builder.kb, 'search', side_effect=search), \
                patch.object(builder, 'validate_workflow') as validate:
            task = asyncio.create_task(consume())
            await started.wait()
            try:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                validate.assert_not_called()
            finally:
                release.set()
                await asyncio.to_thread(finished.wait)

    async def test_retrieval_counts_come_from_caller_and_errors_keep_their_ids(self):
        received = []
        inventory = ({f'Node{i}': object() for i in range(120)}, {})
        async def events(config, messages, tools):
            if len(messages) == 2:
                for ident, args in [('all', '{}'), ('some', '{"limit":75}'), ('bad', '{"limit":-1}')]:
                    yield {'type': 'tool_call', 'id': ident, 'name': 'search_nodes', 'arguments': args}
                yield {'type': 'tool_call', 'id': 'invalid', 'name': 'get_node_schemas', 'arguments': '[]'}
            else:
                received.extend(m for m in messages if m['role'] == 'tool')
                yield {'type': 'text', 'text': json.dumps(self.graph)}
        with patch.object(builder.providers, 'chat_events', events), \
                patch.object(builder.node_catalog, 'snapshot', return_value=inventory), \
                patch.object(builder, 'validate_workflow', return_value=self.graph):
            _ = [e async for e in builder.build_workflow('build', 'generate', '')]
        self.assertEqual(len(json.loads(received[0]['content'])), 120)
        self.assertEqual(len(json.loads(received[1]['content'])), 75)
        self.assertEqual(received[2]['tool_call_id'], 'bad')
        self.assertIn('nonnegative', json.loads(received[2]['content'])['error'])
        self.assertEqual(received[3]['tool_call_id'], 'invalid')
        self.assertIn('object', json.loads(received[3]['content'])['error'])

    def test_validation_observes_changed_model_choices_between_submissions(self):
        choices = ['installed.safetensors']
        class Model:
            RETURN_TYPES = ()
            OUTPUT_NODE = True
            @classmethod
            def INPUT_TYPES(cls):
                return {'required': {'model': (choices,)}}
        inventory = ({'Model': Model}, {})
        graph = {'1': {'class_type': 'Model', 'inputs': {'model': 'installed.safetensors'}}}
        with patch.object(builder.node_catalog, 'schema_record', REAL_SCHEMA_RECORD):
            self.assertEqual(builder.validate_workflow(graph, inventory), graph)
            choices[:] = ['replacement.safetensors']
            with self.assertRaisesRegex(ValueError, 'installed option'):
                builder.validate_workflow(graph, inventory)
            graph['1']['inputs']['model'] = 'replacement.safetensors'
            self.assertEqual(builder.validate_workflow(graph, inventory), graph)

    async def test_real_debug_output_is_correlated_and_contains_no_request_content(self):
        debug = sys.modules[PACKAGE + '.debug']
        async def events(config, messages, tools):
            yield {'type': 'text', 'text': json.dumps(self.graph)}
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(debug, 'LOG_PATH', str(Path(folder) / 'debug.log')), \
                patch.object(debug, '_events', None), patch.object(debug, '_enabled', return_value=True), \
                patch.object(builder.providers, 'chat_events', events):
            _ = [e async for e in builder.build_workflow('private request', 'generate', 'private rule')]
            record = json.loads(Path(debug.LOG_PATH).read_text(encoding='utf-8').strip())
        self.assertEqual(record['event'], 'build.finished')
        self.assertEqual(record['data']['entry_point'], 'bridge')
        self.assertTrue(record['data']['build_id'])
        self.assertEqual(record['data']['outcome'], 'success')
        self.assertNotIn('private', json.dumps(record))

    def test_invalid_widget_types_and_bounds_are_rejected(self):
        self.schemas['EmptyImage']['input']['required']['width'] = ['INT', {'min': 16, 'max': 128}]
        for value in ('abc', True, 999):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.graph['1']['inputs']['width'] = value
                builder.validate_workflow(self.graph, self.inventory)

    def test_cycles_are_rejected(self):
        self.schemas['EmptyImage']['input']['optional'] = {'image': ['IMAGE']}
        self.graph['1']['inputs']['image'] = ['1', 0]
        with self.assertRaisesRegex(ValueError, 'cycle'):
            builder.validate_workflow(self.graph, self.inventory)
