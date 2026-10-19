import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
import httpx
from PIL import Image
from starlette.testclient import TestClient

from grok_channel.server import Grok, create_execution
from grok_channel.store import Store

buffer = io.BytesIO()
Image.new('RGB', (2, 3), (20, 40, 60)).save(buffer, format='PNG')
IMAGE = base64.b64encode(buffer.getvalue()).decode()


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'state.db'
        self.key = Fernet.generate_key().decode()
        env = patch.dict('os.environ', {'GROK_ALLOWED_API_ORIGINS':'https://gateway.example.invalid'})
        env.start(); self.addCleanup(env.stop)
        self.mode = 'success'; self.requests = []
        def upstream(request):
            self.requests.append(request)
            if self.mode == 'timeout': raise httpx.ReadTimeout('synthetic interruption', request=request)
            if self.mode == 'reject': return httpx.Response(400)
            if self.mode == 'limited': return httpx.Response(429)
            if self.mode == 'bad': return httpx.Response(200, json={'data':[{'b64_json':base64.b64encode(b'not an image').decode()}]})
            return httpx.Response(200, json={'data':[{'b64_json':IMAGE}]})
        self.http = httpx.Client(transport=httpx.MockTransport(upstream))
        self.addCleanup(self.http.close)
        self.service = Grok(Store(str(self.path),self.key),self.http)
        account = self.service.accounts.create('org','admin','create',{'api_url':'https://gateway.example.invalid/v1',
            'api_key':'synthetic-test-key','proxy':{'mode':'direct'},'default':True,'registration':{'budget_limit':'20'}})
        self.account = self.service.accounts.checked('org','admin',account['account_id'],1,'active')
        self.api = TestClient(create_execution(self.service,'e'*40)); self.addCleanup(self.api.close)
        self.headers = {'Authorization':'Bearer '+'e'*40,'X-Organization-Id':'org','X-Caller-Id':'image-mcp'}

    def submit(self, operation='task', **changes):
        return self.api.post('/v1/calls',headers=self.headers,json={'idempotency_key':operation,'task_id':operation,
            'model_id':'grok-imagine-image-2.0','capability':'image','payload':{'prompt':'synthetic test'},'budget_limit':'2',**changes})

    def test_generate_once_and_read_encrypted_result_after_archival(self):
        response = self.submit(); self.assertEqual(response.status_code,200,response.text)
        result = response.json(); self.assertEqual(result['state'],'completed'); self.assertIsNone(result['budget']['amount'])
        self.assertIsNone(result['settlement']['amount']); self.assertEqual(self.submit().json(),result)
        self.assertEqual(len(self.requests),1)
        body = json.loads(self.requests[0].content)
        self.assertEqual(body['response_format'],'b64_json'); self.assertEqual(body['resolution'],'2k')
        self.service.accounts.patch('org','admin',self.account['account_id'],self.account['revision'],{},archive=True)
        data = self.api.get('/v1/calls/'+result['call_id']+'/result',headers=self.headers).json()
        self.assertEqual(data['image']['data'],IMAGE); self.assertEqual(data['image']['height'],3)
        self.assertNotIn(IMAGE.encode(),self.path.read_bytes())
        self.assertEqual(self.submit().json(),result)
        self.assertEqual(self.submit('new').status_code,409); self.assertEqual(len(self.requests),1)

    def test_edit_preserves_single_reference_and_log_operation(self):
        result = self.submit(payload={'action':'edit','prompt':'edit test','resolution':'1k',
            'image':{'mime_type':'image/png','data':IMAGE}}).json()
        self.assertEqual(result['state'],'completed'); self.assertEqual(result['operation'],'edit')
        self.assertEqual(self.requests[0].url.path,'/v1/images/edits')
        body = json.loads(self.requests[0].content)
        self.assertEqual(body['image']['url'],'data:image/png;base64,'+IMAGE); self.assertNotIn('aspect_ratio',body)
        self.assertEqual(self.service.calls.list('org')['items'][0]['operation'],'edit')

    def test_uncertain_calls_never_resubmit_including_after_restart(self):
        for mode in ('timeout','bad','limited'):
            self.mode=mode
            result=self.submit(mode).json(); self.assertEqual(result['state'],'acceptance_unknown')
            self.assertIsNone(result['accepted']); self.assertEqual(self.submit(mode).json(),result)
            reopened=Grok(Store(str(self.path),self.key),self.http)
            self.assertEqual(reopened.execution.submit('org','image-mcp',mode,mode,'grok-imagine-image-2.0',
                {'prompt':'synthetic test'},None,'2'),result)
        self.assertEqual(len(self.requests),3)

    def test_rejection_does_not_resubmit_or_charge_reservation(self):
        self.mode='reject'; result=self.submit().json()
        self.assertEqual(result['state'],'rejected'); self.assertFalse(result['accepted'])
        self.assertEqual(self.submit().json(),result); self.assertEqual(len(self.requests),1)
        row=self.service.calls.get('org',result['call_id'])
        self.assertEqual(row['reservation_amount'],'0')

    def test_invalid_parameters_never_reach_upstream(self):
        for change in ({'model_id':'unapproved'}, {'budget_limit':None}, {'capability':'video'},
            {'payload':{'prompt':'test','image':{'url':'https://unapproved.invalid/a.png'}}},
            {'payload':{'prompt':'test','action':'edit','image':{'mime_type':'image/jpeg','data':IMAGE}}},
            {'payload':{'prompt':'test','aspect_ratio':'3:2'}}):
            self.assertIn(self.submit(**change).status_code,{400,409})
        self.assertEqual(self.requests,[])

    def test_execution_identity_and_cross_org_recovery_are_enforced(self):
        result=self.submit().json()
        path='/v1/calls/'+result['call_id']+'/result'
        for headers, status in ((self.headers|{'Authorization':'Bearer '+'m'*40},401),
            (self.headers|{'X-Caller-Id':'video-mcp'},403),(self.headers|{'X-Organization-Id':'other'},404)):
            self.assertEqual(self.api.get(path,headers=headers).status_code,status)
        self.assertEqual(self.api.get(path+'?url=bad',headers=self.headers).status_code,403)
        self.assertEqual(len(self.requests),1)

    def test_video_submission_polling_and_encrypted_result(self):
        video_headers = {**self.headers, 'X-Caller-Id': 'video-mcp'}
        def video_upstream(request):
            self.requests.append(request)
            if request.method == 'POST' and request.url.path == '/v1/videos/generations':
                return httpx.Response(200, json={'request_id': 'vid-req-1'})
            if request.method == 'GET' and request.url.path == '/v1/videos/vid-req-1':
                return httpx.Response(200, json={'status': 'done', 'progress': 100,
                    'usage': {'cost_in_usd_ticks': 2500000000},
                    'video': {'url': '/v1/videos/vid-req-1/content', 'respect_moderation': True}})
            if request.method == 'GET' and request.url.path == '/v1/videos/vid-req-1/content':
                return httpx.Response(200, content=b'\x00\x00\x00\x20ftypisom-video-data',
                    headers={'content-type': 'video/mp4'})
            return httpx.Response(404)
        self.service.client = self.service.execution.client = httpx.Client(transport=httpx.MockTransport(video_upstream))
        self.addCleanup(self.service.client.close)
        submitted = self.api.post('/v1/calls', headers=video_headers, json={
            'idempotency_key': 'vid-task', 'task_id': 'vid-task', 'model_id': 'grok-imagine-video',
            'capability': 'video', 'payload': {'prompt': 'video test', 'duration': 5, 'resolution': '720p', 'aspect_ratio': '16:9'},
            'budget_limit': '2'})
        self.assertEqual(submitted.status_code, 200, submitted.text)
        call = submitted.json()
        self.assertEqual(call['state'], 'in_progress')
        polled = self.api.get('/v1/calls/' + call['call_id'], headers=video_headers).json()
        self.assertEqual(polled['state'], 'completed')
        self.assertEqual(polled['settlement']['amount'], '0.25')
        content = self.api.get('/v1/calls/' + call['call_id'] + '/result', headers=video_headers)
        self.assertEqual(content.status_code, 200)
        self.assertEqual(content.content, b'\x00\x00\x00\x20ftypisom-video-data')
        self.assertEqual(self.api.get('/v1/calls/' + call['call_id'] + '/result', headers=self.headers).status_code, 403)

if __name__ == '__main__': unittest.main()
