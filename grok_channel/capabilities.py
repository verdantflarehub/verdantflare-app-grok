"""Read the installed declaration; adapter allowlists never supply a directory."""
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import re

import yaml

from .execution import MODELS, VIDEO_MODELS


class DeclarationLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise ValueError('invalid_channel_metadata')
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise ValueError('invalid_channel_metadata')
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def load_metadata(path=None):
    path = path if path is not None else os.getenv('CHANNEL_METADATA_FILE', '')
    if not path:
        return {'schema_version': 1, 'channel_id': 'grok', 'version': 'unavailable',
                'state': 'unavailable', 'capabilities': []}
    raw = Path(path).read_bytes()
    if len(raw) > 256 * 1024:
        raise ValueError('invalid_channel_metadata')
    data = yaml.load(raw.decode('utf-8'), Loader=DeclarationLoader)
    if (not isinstance(data, dict) or type(data.get('schema_version')) is not int or data.get('schema_version') != 1
            or data.get('channel_id') != 'grok' or not isinstance(data.get('capabilities'), list)
            or set(data) - {'schema_version', 'channel_id', 'channel_name', 'channel_logo', 'capabilities'}
            or len(data['capabilities']) > 64):
        raise ValueError('invalid_channel_metadata')
    seen = set()
    for item in data['capabilities']:
        if (not isinstance(item, dict) or set(item) - {'capability_id', 'type', 'model_id', 'display_name',
                'operation', 'parameter_schema', 'execution_state', 'card_title', 'card_group', 'source_url',
                'input_summary', 'output_summary', 'endpoint_id', 'model_logo', 'model_logo_mime'}
                or any(not isinstance(item.get(k), str) or not item[k].strip()
                for k in ('capability_id', 'type', 'model_id', 'display_name', 'operation', 'execution_state'))
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', item['capability_id'])
                or item['capability_id'] in seen or '\ufffd' in item['display_name']
                or any(ord(c) < 32 for c in item['display_name'])
                or item['display_name'] == item['model_id']
                or not isinstance(item.get('parameter_schema'), dict)
                or item['execution_state'] not in {'enabled', 'disabled', 'catalog_only', 'adapter_connected_unverified'}):
            raise ValueError('invalid_channel_metadata')
        seen.add(item['capability_id'])
        supported = ((item['type'] == 'video' and item['model_id'] in VIDEO_MODELS and item['operation'] == 'video.generate')
                     or (item['type'] == 'image' and item['model_id'] in MODELS
                         and item['operation'] in {'image.generate', 'image.edit'}))
        if not supported:
            item['execution_state'] = 'catalog_only'
    return {**data, 'version': hashlib.sha256(raw).hexdigest(), 'state': 'available'}


def project(metadata, caller, execution):
    media = 'video' if caller == 'video-mcp' else 'image'
    allowed = execution.video_models if media == 'video' else execution.models
    data = deepcopy(metadata)
    data['capabilities'] = [item for item in data['capabilities'] if item['type'] == media]
    for item in data['capabilities']:
        if item['model_id'] not in allowed:
            item['execution_state'] = 'disabled'
        # Installation/configuration alone does not prove the upstream works.
        elif media == 'video' and item['execution_state'] == 'enabled':
            item['execution_state'] = 'adapter_connected_unverified'
    return data
