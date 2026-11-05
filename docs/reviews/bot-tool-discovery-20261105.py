"""Offline counterfactual request accounting; never call an LLM or business tool."""
from __future__ import annotations

import argparse
from copy import deepcopy
from itertools import combinations_with_replacement
import json
from pathlib import Path
import sys

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--root', required=True, type=Path, help='Checkout fixed to the measured base revision')
parser.add_argument('--tokenizer-dir', required=True, type=Path, help='Isolated tiktoken 0.12.0 installation')
args = parser.parse_args()
ROOT = args.root.resolve()
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(args.tokenizer_dir.resolve()))
import tiktoken

from src.application.agent_tool_registry import AGENT_TOOL_REGISTRY, RECOMMENDED_FLOW, get_tool_definition
from src.application.bot.contracts import ExecutionContract
from src.application.bot.eval_fixtures import fixture_observations
from src.application.bot.memory import memory_tool_description
from src.application.bot.scene import build_scene_manifest, load_general_scene
from src.application.bot.tools import tool_descriptions


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


META = [
    {'name': 'find_tools', 'description': 'Find allowed read-only tools by keywords or exact names; returns purpose and parameter summaries.',
     'input_schema': {'type': 'object', 'properties': {'query': {'type': 'string'}}, 'required': ['query'], 'additionalProperties': False}},
    {'name': 'get_tool_schema', 'description': 'Get the complete allowed Bot tool definition by exact name before calling it.',
     'input_schema': {'type': 'object', 'properties': {'name': {'type': 'string'}}, 'required': ['name'], 'additionalProperties': False}},
]
contract = ExecutionContract('measure', 'measure', 'om_chat', 'eval', {'user_message': '解释当前运行记录'}, {}, {})
manifest = build_scene_manifest(contract, 'measure')
TOOLS = [{k: t[k] for k in ('name', 'description', 'input_schema')} for t in manifest.tool_descriptions]
BY_NAME = {t['name']: t for t in TOOLS}
INDEX = [{'name': t['name'], 'description': get_tool_definition(t['name']).catalog_summary or t['description'].split(' Returns ')[0]} for t in TOOLS]
BASE_MESSAGES = [dict(m) for m in manifest.messages if m['role'] == 'system']
BASE_MESSAGES.append({'role': 'system', 'content': load_general_scene()['option_candidates_guidance']})
# Same context reference for A/B; values are synthetic and never authorize a business read.
BASE_MESSAGES.append({'role': 'system', 'content': 'Context: {}'})
BASE_MESSAGES.append({'role': 'user', 'content': '解释当前运行记录'})
DISCOVERY_MESSAGE = {'role': 'system', 'content': 'Available read-only tools (name and purpose): ' + dumps(INDEX)
    + '\nUse find_tools to search, then get_tool_schema for the exact definition before calling a tool. Two misses: use an exact name from this index. Unloaded tools return a recoverable error. Schema retrieval grants no new permission.'}


def function_call(name, arguments, identifier):
    return {'id': identifier, 'type': 'function', 'function': {'name': name, 'arguments': dumps(arguments)}}


def append_calls(messages, calls, results):
    messages.append({'role': 'assistant', 'content': '', 'tool_calls': calls})
    for call, result in zip(calls, results, strict=True):
        messages.append({'role': 'tool', 'tool_call_id': call['id'], 'content': dumps(result)})


def wire(messages, tools):
    # Same three fields transformed by runtime.request_model -> Chat Completions.
    return {'messages': messages, 'tools': [
        {'type': 'function', 'function': {'name': t['name'], 'description': t['description'], 'parameters': t['input_schema']}}
        for t in tools]}


def request_bodies(names, *, mode, observations=None, parallel=False, memory=False):
    """Two hypothetical business calls; no selection or execution is simulated.

    Direct batch-fetches exact-name schemas in one round (two meta calls for two
    distinct tools); search adds a keyword-search round. search_activate loads
    search matches without get_tool_schema. direct_activate is an optimistic
    lower bound: exact names, native schema activation, short confirmation only.
    """
    selected = [deepcopy(BY_NAME[n]) for n in dict.fromkeys(names)]
    messages = deepcopy(BASE_MESSAGES)
    resident = [memory_tool_description()] if memory else []
    output_messages = []
    bodies = []
    if mode == 'full':
        active = deepcopy(TOOLS) + resident
    else:
        discovery_message = deepcopy(DISCOVERY_MESSAGE)
        if mode == 'search_activate':
            instruction = 'Use find_tools to search; matching definitions become callable on the next request. get_tool_schema returns a complete definition if needed.'
        elif mode == 'direct_activate':
            instruction = 'For exact names call get_tool_schema; the definition becomes callable on the next request and the result confirms activation. Otherwise use find_tools first.'
        else:
            instruction = 'Use find_tools to search, then get_tool_schema for the exact definition before calling a tool.'
        discovery_message['content'] = discovery_message['content'].replace(
            'Use find_tools to search, then get_tool_schema for the exact definition before calling a tool.', instruction)
        messages.insert(-1, discovery_message)
        active = deepcopy(META) + resident
        if mode in ('search', 'search_activate'):
            bodies.append(wire(deepcopy(messages), deepcopy(active)))
            results = [{'name': t['name'], 'description': next(i['description'] for i in INDEX if i['name'] == t['name']),
                        'parameters': {k: {'type': v.get('type'), 'required': k in t['input_schema'].get('required', [])}
                                       for k, v in t['input_schema'].get('properties', {}).items()}}
                       for t in selected]
            call = function_call('find_tools', {'query': ' '.join(names)}, 'search1')
            result = {'ok': True, 'data': {'matches': results}}
            append_calls(messages, [call], [result])
            output_messages.append({'role': 'assistant', 'content': '', 'tool_calls': [call]})
        if mode != 'search_activate':
            bodies.append(wire(deepcopy(messages), deepcopy(active)))
            calls = [function_call('get_tool_schema', {'name': t['name']}, f'schema{i}') for i, t in enumerate(selected)]
            results = ([{'ok': True, 'data': {'loaded': t['name']}} for t in selected] if mode == 'direct_activate'
                       else [{'ok': True, 'data': {'tool': t}} for t in selected])
            append_calls(messages, calls, results)
            output_messages.append({'role': 'assistant', 'content': '', 'tool_calls': calls})
        active += selected
    if observations is None:
        observations = [{'ok': True, 'data': {'status': 'available'}} for _ in names]
    actual_calls = [function_call(name, {}, f'call{i}') for i, name in enumerate(names)]
    if parallel:
        bodies.append(wire(deepcopy(messages), deepcopy(active)))
        append_calls(messages, actual_calls, observations)
        output_messages.append({'role': 'assistant', 'content': '', 'tool_calls': actual_calls})
    else:
        for call, observation in zip(actual_calls, observations, strict=True):
            bodies.append(wire(deepcopy(messages), deepcopy(active)))
            append_calls(messages, [call], [observation])
            output_messages.append({'role': 'assistant', 'content': '', 'tool_calls': [call]})
    bodies.append(wire(deepcopy(messages), deepcopy(active)))
    # Final answer is shared and therefore omitted from the differential output count.
    return bodies, output_messages


def measure(enc, names, **kwargs):
    count = lambda value: len(enc.encode(dumps(value)))
    result = {}
    for mode in ('full', 'direct', 'search', 'search_activate', 'direct_activate'):
        bodies, outputs = request_bodies(names, mode=mode, **kwargs)
        result[mode] = {'model_requests': len(bodies), 'input_tokens_per_request': [count(b) for b in bodies],
                        'input_tokens': sum(count(b) for b in bodies), 'output_call_tokens': sum(count(o) for o in outputs)}
    for mode in ('direct', 'search', 'search_activate', 'direct_activate'):
        result[mode]['input_saved_percent'] = round(100 * (1 - result[mode]['input_tokens'] / result['full']['input_tokens']), 2)
    return result


def main():
    output = {'base': '92ddd9e16', 'method': 'offline JSON token proxies, not provider billing/semantic accuracy',
              'registry_entries': len(AGENT_TOOL_REGISTRY), 'bot_read_definitions': len(tool_descriptions(list(AGENT_TOOL_REGISTRY))),
              'catalog_bytes': len(json.dumps(tool_descriptions(list(AGENT_TOOL_REGISTRY)), ensure_ascii=False).encode()),
              'actual_scene_tool_count': len(TOOLS), 'scene_wire_bytes': len(json.dumps(TOOLS, ensure_ascii=False).encode()),
              'recommended_flow': {n: {'in_scene': n in BY_NAME, 'read_only': get_tool_definition(n).is_pure_read()} for n in RECOMMENDED_FLOW},
              'encodings': {}}
    fixture_ids = ('opening_candidate_snapshot_diagnostics_model_ready', 'close_advice_notification_diagnostics_model_ready',
                   'current_option_exposure_model_ready')
    for encoding in ('cl100k_base', 'o200k_base'):
        enc = tiktoken.get_encoding(encoding)
        count = lambda value: len(enc.encode(dumps(value)))
        data = {'tool_tokens': {t['name']: count(t) for t in TOOLS}, 'full_tools_tokens': count(TOOLS),
                'shared_messages_tokens': count(BASE_MESSAGES), 'index_tokens': count(INDEX),
                'meta_tokens': count(META), 'discovery_prompt_tokens': count(DISCOVERY_MESSAGE),
                'pairs': {}, 'fixtures': {}}
        for label, names in {
            'cheap_tool_twice': ('project_context', 'project_context'),
            'filter_tool_twice': ('candidate_filter_explain', 'candidate_filter_explain'),
            'candidate_tool_twice': ('option_candidates', 'option_candidates'),
            'account_then_filter': ('project_context', 'candidate_filter_explain'),
            'runtime_then_filter': ('runtime_status', 'candidate_filter_explain'),
            'runs_then_logs': ('runtime_runs', 'runtime_logs'),
            'positions_then_receipt': ('option_positions_read', 'receipt_read'),
        }.items():
            data['pairs'][label] = measure(enc, names)
        data['pairs']['account_then_filter_with_memory'] = measure(enc, ('project_context', 'candidate_filter_explain'), memory=True)
        data['pairs']['two_parallel_calls'] = measure(enc, ('runtime_status', 'candidate_filter_explain'), parallel=True)
        for fixture_id in fixture_ids:
            observations = fixture_observations(fixture_id)
            supported = [o for o in observations if o['tool_name'] in BY_NAME]
            unsupported = [o['tool_name'] for o in observations if o['tool_name'] not in BY_NAME]
            data['fixtures'][fixture_id] = {'observed_tools': [o['tool_name'] for o in observations],
                'scene_tools': [o['tool_name'] for o in supported], 'outside_scene': unsupported,
                'note': 'observations are pre-injected; scripted fixture does not select any tools'}
            if len(supported) >= 2:
                data['fixtures'][fixture_id]['two_call_cost'] = measure(enc, tuple(o['tool_name'] for o in supported[:2]), observations=supported[:2])
        # Unweighted sensitivity analysis over all 55 unordered tool pairs,
        # repetitions included. This is not a measured session distribution.
        pair_results = [measure(enc, names) for names in combinations_with_replacement(BY_NAME, 2)]
        data['all_55_pairs_unweighted'] = {
            mode: {'min_saved_percent': min(r[mode]['input_saved_percent'] for r in pair_results),
                   'max_saved_percent': max(r[mode]['input_saved_percent'] for r in pair_results),
                   'mean_input_tokens': round(sum(r[mode]['input_tokens'] for r in pair_results) / len(pair_results), 2),
                   'pairs_saving_input': sum(r[mode]['input_tokens'] < r['full']['input_tokens'] for r in pair_results)}
            for mode in ('direct', 'search', 'search_activate', 'direct_activate')
        }
        output['encodings'][encoding] = data
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
