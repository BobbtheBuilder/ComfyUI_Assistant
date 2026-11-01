import importlib.util
import json
from pathlib import Path
import sys
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


class WorkflowBuilderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.inventory = ({'EmptyImage': object(), 'SaveImage': object()}, {})
        self.graph = {'1': {'class_type': 'EmptyImage', 'inputs': {'width': 64}},
                      '2': {'class_type': 'SaveImage', 'inputs': {'images': ['1', 0]}}}
        self.schemas = {
            'EmptyImage': {'input': {'required': {'width': ['INT']}}, 'output': ['IMAGE']},
            'SaveImage': {'input': {'required': {'images': ['IMAGE']}}, 'output': [], 'output_node': True}}
        self.enterContext(patch.object(builder.node_catalog, 'snapshot', return_value=self.inventory))
        self.enterContext(patch.object(builder.node_catalog, 'record', side_effect=lambda n, _i: dict(self.schemas.get(n, {}))))
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
        self.assertEqual(requests, [('image example', 4)])
        self.assertEqual(result[-1]['type'], 'result')

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
