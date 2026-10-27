import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient

from grok_channel.capabilities import load_metadata, project
from grok_channel.server import create_execution


class CapabilitiesTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'channel.yaml'
        self.cap = {'capability_id': 'video.test', 'type': 'video', 'model_id': 'grok-imagine-video',
                    'display_name': 'Grok Imagine Video', 'operation': 'video.generate',
                    'parameter_schema': {}, 'execution_state': 'enabled'}
        self.execution = SimpleNamespace(video_models={'grok-imagine-video'}, models=set())

    def write(self, capabilities):
        self.path.write_text(json.dumps({'schema_version': 1, 'channel_id': 'grok',
                                        'capabilities': capabilities}), encoding='utf-8')

    def test_real_declaration_names_and_unverified_adapter_intersection(self):
        self.write([self.cap, {**self.cap, 'capability_id': 'video.future', 'model_id': 'future'}])
        loaded = load_metadata(self.path)
        self.assertEqual(loaded['capabilities'][1]['execution_state'], 'catalog_only')
        value = project(loaded, 'video-mcp', self.execution)
        self.assertEqual(value['capabilities'][0]['display_name'], 'Grok Imagine Video')
        self.assertEqual(value['capabilities'][0]['execution_state'], 'adapter_connected_unverified')
        self.assertEqual(value['capabilities'][1]['execution_state'], 'disabled')
        self.assertEqual(project(loaded, 'image-mcp', self.execution)['capabilities'], [])
        self.assertEqual(loaded['capabilities'][0]['execution_state'], 'enabled')

    def test_missing_file_has_no_fabricated_models_and_invalid_names_fail(self):
        self.assertEqual(load_metadata('')['capabilities'], [])
        for name in ['grok-imagine-video', '\ufffdBroken']:
            self.write([{**self.cap, 'display_name': name}])
            with self.assertRaises(ValueError):
                load_metadata(self.path)
        self.write([self.cap, self.cap])
        with self.assertRaises(ValueError):
            load_metadata(self.path)

    def test_internal_directory_requires_token_and_caller_but_no_account_identity(self):
        self.write([self.cap])
        with patch.dict('os.environ', {'CHANNEL_METADATA_FILE': str(self.path)}):
            app = create_execution(SimpleNamespace(execution=self.execution), 't' * 32)
        client = TestClient(app)
        self.addCleanup(client.close)
        self.assertEqual(client.get('/v1/capabilities').status_code, 401)
        headers = {'Authorization': 'Bearer ' + 't' * 32, 'X-Caller-Id': 'video-mcp'}
        value = client.get('/v1/capabilities', headers=headers)
        self.assertEqual(value.status_code, 200)
        self.assertEqual(value.json()['capabilities'][0]['display_name'], 'Grok Imagine Video')
        headers['X-Caller-Id'] = 'dashboard'
        self.assertEqual(client.get('/v1/capabilities', headers=headers).status_code, 403)
