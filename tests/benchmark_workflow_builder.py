"""Offline, repeatable CPU/bridge benchmark; never calls a model or touches a canvas.

Run: python -m tests.benchmark_workflow_builder --output <report.json>
The bridge's simulated I/O measures scheduling, not real provider latency.
"""
import argparse
import asyncio
import importlib
import json
from pathlib import Path
import statistics
import sys
import time
import types
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = 'assistant_performance_benchmark'
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = package
builder = importlib.import_module(PACKAGE + '.workflow_builder')
catalog = builder.node_catalog


class Image:
    """Image fixture with dynamic schema and source evidence like a custom node."""
    RETURN_TYPES = ('IMAGE',)
    FUNCTION = 'execute'

    @classmethod
    def INPUT_TYPES(cls):
        return {'required': {'width': ('INT', {'min': 1})},
                'optional': {'image': ('IMAGE',)}}

    def execute(self, width, image=None):
        raise AssertionError('Benchmarks must not generate images')


class Save(Image):
    OUTPUT_NODE = True


def graph(size):
    return {str(i): {'class_type': 'Save' if i == size else 'Image',
                    'inputs': {'width': 64, **({'image': [str(i - 1), 0]} if i > 1 else {})}}
            for i in range(1, size + 1)}


def sample(operation, repeats):
    started = time.perf_counter()
    operation()
    cold = (time.perf_counter() - started) * 1000
    values = []
    for _ in range(repeats):
        started = time.perf_counter()
        operation()
        values.append((time.perf_counter() - started) * 1000)
    return {'first_ms': cold, 'median_ms': statistics.median(values),
            'min_ms': min(values), 'max_ms': max(values), 'samples_ms': values}


async def bridge(inventory, batched=False):
    observed = {}
    docs = [{'text': 'Unicode image docs: \u753b\u50cf ' * 2000, 'source': 'fixture.md'}]

    async def events(config, messages, tools):
        observed['rounds'] = observed.get('rounds', 0) + 1
        if len(messages) == 2:
            if batched:
                yield {'type': 'tool_call', 'id': 'schemas', 'name': 'get_node_schemas',
                       'arguments': json.dumps({'node_types': ['Image', 'Save'] * 4})}
            else:
                for i, name in enumerate(['Image', 'Save'] * 4):
                    yield {'type': 'tool_call', 'id': str(i), 'name': 'get_node_schema',
                           'arguments': json.dumps({'node_type': name})}
            for i in range(3):
                yield {'type': 'tool_call', 'id': 'docs' + str(i), 'name': 'search_docs',
                       'arguments': '{"query":"image", "limit":0}'}
        else:
            observed['tool_bytes'] = sum(len(m['content'].encode()) for m in messages if m['role'] == 'tool')
            yield {'type': 'tool_call', 'id': 'done', 'name': 'submit_workflow',
                   'arguments': json.dumps({'workflow': graph(4)})}

    def search(*_args):
        time.sleep(0.015)  # Deliberate independent I/O, not a model deadline.
        return docs

    with patch.object(builder.CONFIG_STORE, 'resolved', return_value={}), \
            patch.object(catalog, 'snapshot', return_value=inventory), \
            patch.object(builder.providers, 'chat_events', events), \
            patch.object(builder.kb, 'search', search):
        results = [e async for e in builder.build_workflow('build fixture', 'benchmark', '')]
        assert results[-1]['workflow'] == graph(4)
    return observed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output')
    parser.add_argument('--repeats', type=int, default=7)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error('--repeats must be positive')
    inventory = ({'Image': Image, 'Save': Save}, {})
    lookup = getattr(catalog, 'schema_record', catalog.record)
    report = {'kind': 'offline synthetic benchmark; no real LLM timing', 'repeats': args.repeats}
    report['schema_16'] = sample(lambda: [lookup('Image', inventory) for _ in range(16)], args.repeats)
    for size in (4, 64, 256):
        workflow = graph(size)
        report['validate_' + str(size)] = sample(lambda: builder.validate_workflow(workflow, inventory), args.repeats)
    report['bridge_reads'] = sample(lambda: asyncio.run(bridge(inventory)), args.repeats)
    report['bridge_observed'] = asyncio.run(bridge(inventory))
    if any(t['function']['name'] == 'get_node_schemas' for t in builder.TOOLS):
        report['bridge_batched'] = sample(lambda: asyncio.run(bridge(inventory, True)), args.repeats)
    large = ({**{f'Installed{i}': Image for i in range(50000)}, **inventory[0]}, {})
    report['inventory_50k_bridge'] = sample(lambda: asyncio.run(bridge(large)), args.repeats)
    output = json.dumps(report, indent=2)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(output + '\n', encoding='utf-8')
    print(output)


if __name__ == '__main__':
    main()
