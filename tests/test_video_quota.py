import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import httpx
from cryptography.fernet import Fernet
from starlette.testclient import TestClient

from grok_channel import video_quota
from grok_channel.profile import registration
from grok_channel.server import Grok, create_execution
from grok_channel.store import Store, Conflict


class VideoQuotaTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'state.db'
        self.key = Fernet.generate_key().decode()
        env = patch.dict('os.environ', {'GROK_ALLOWED_API_ORIGINS': 'https://gateway.example.invalid'})
        env.start()
        self.addCleanup(env.stop)
        self.current = '2026-10-27T03:00:00+00:00'
        clock = patch('grok_channel.video_quota.now', side_effect=lambda: self.current)
        clock.start()
        self.addCleanup(clock.stop)
        calls_clock = patch('grok_channel.calls.now', side_effect=lambda: self.current)
        calls_clock.start()
        self.addCleanup(calls_clock.stop)
        self.http = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
        self.addCleanup(self.http.close)
        self.service = Grok(Store(self.path, self.key), self.http)
        self.limits = {'source': 'membership_registration', 'checked_at': '2026-10-01T00:00:00+08:00',
                       'valid_until': '2026-12-01T00:00:00+08:00', 'timezone': 'Asia/Shanghai',
                       'period': 'calendar', 'count_policy': 'accepted_and_unknown',
                       'daily_limit': 3, 'monthly_limit': 10, 'max_concurrent': 2}
        created = self.service.accounts.create('org', 'admin', 'create', {
            'api_url': 'https://gateway.example.invalid/v1', 'api_key': 'synthetic-test-key',
            'proxy': {'mode': 'direct'}, 'default': True, 'registration': {'video_limits': self.limits}})
        self.account = self.service.accounts.checked('org', 'admin', created['account_id'], 1, 'active')

    def reserve(self, operation, media='video'):
        return self.service.calls.reserve('org', 'video-mcp' if media == 'video' else 'image-mcp',
            operation, operation, 'grok-imagine-video-1.5', {'prompt': 'test'}, None, '2', media=media)

    def quota(self):
        return self.service.calls.video_preflight('org')['video_quota']

    def update(self, limits=None, **registration_fields):
        current = self.service.accounts.get('org', self.account['account_id'])
        self.service.accounts.patch('org', 'admin', current['account_id'], current['revision'],
            {'registration': {'video_limits': self.limits if limits is None else limits, **registration_fields}})

    def test_registration_requires_explicit_counting_and_timed_source(self):
        self.assertEqual(registration({'video_limits': self.limits})['video_limits'], self.limits)
        for changed in ({'daily_limit': True}, {'monthly_limit': -1}, {'max_concurrent': 1.5},
                        {'timezone': 'not/a-timezone'}, {'period': 'rolling'},
                        {'count_policy': 'success_only'}, {'checked_at': '2026-10-01'},
                        {'valid_until': '2026-09-01T00:00:00Z'}, {'source': 'unknown'}):
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, 'invalid_video_limits'):
                registration({'video_limits': self.limits | changed})
        with self.assertRaises(ValueError):
            registration({'video_limits': self.limits | dict.fromkeys(('daily_limit', 'monthly_limit', 'max_concurrent'))})

    def test_only_video_accepted_or_unknown_counts_and_rejected_releases(self):
        image, _ = self.reserve('image', media='image')
        self.service.calls.accepted(image['id'], 'image-remote')
        unknown, _ = self.reserve('unknown')
        accepted, _ = self.reserve('accepted')
        self.service.calls.accepted(accepted['id'], 'video-remote')
        self.service.calls.observed(accepted['id'], 'failed')
        q = self.quota()
        self.assertEqual((q['daily']['used'], q['daily']['reserved'], q['daily']['remaining']), (1, 1, 1))
        self.assertEqual(q['concurrent']['reserved'], 1)
        self.service.calls.rejected(unknown['id'], 'provider_rejected')
        q = self.quota()
        self.assertEqual((q['daily']['used'], q['daily']['reserved'], q['daily']['remaining']), (1, 0, 2))
        self.assertEqual(q['scope'], 'channel_account')
        self.assertIsNone(q['provider_remaining'])

    def test_last_slot_is_atomic_idempotent_and_durable(self):
        self.update(self.limits | {'daily_limit': 1})
        def create(operation):
            try:
                return self.reserve(operation)
            except Conflict as error:
                return str(error)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(create, ['one', 'two']))
        successes = [result for result in results if isinstance(result, tuple)]
        self.assertEqual(len(successes), 1)
        self.assertIn('video_daily_limit_reached', results)
        row = successes[0][0]
        replay, created = self.reserve(row['operation_id'])
        self.assertFalse(created)
        self.assertEqual(replay['id'], row['id'])
        reopened = Grok(Store(self.path, self.key), self.http)
        self.assertEqual(reopened.calls.video_preflight('org')['reason_code'], 'video_daily_limit_reached')

    def test_timezone_daily_and_month_reset_keep_old_inflight_concurrency(self):
        self.current = '2026-10-31T15:59:59+00:00'
        self.reserve('old-month')
        q = self.quota()
        self.assertEqual(q['daily']['resets_at'], '2026-11-01T00:00:00+08:00')
        self.current = '2026-10-31T16:00:00+00:00'
        q = self.quota()
        self.assertEqual(q['daily']['reserved'], 0)
        self.assertEqual(q['monthly']['reserved'], 0)
        self.assertEqual(q['concurrent']['reserved'], 1)
        self.reserve('new-month')
        with self.assertRaisesRegex(Conflict, 'video_concurrency_limit_reached'):
            self.reserve('too-many')

    def test_monthly_zero_expired_and_membership_are_distinct(self):
        for limits, expected in ((self.limits | {'monthly_limit': 0}, 'video_monthly_limit_reached'),
                                 (self.limits | {'valid_until': '2026-10-27T03:00:00Z'}, 'video_limits_expired'),
                                 (self.limits | {'checked_at': '2026-11-01T00:00:00Z'}, 'video_limits_not_yet_valid')):
            self.update(limits)
            with self.assertRaisesRegex(Conflict, expected):
                self.reserve(expected)
        self.update(membership_expiry='2026-10-26')
        with self.assertRaisesRegex(Conflict, 'membership_expired'):
            self.reserve('expired-membership')

    def test_missing_limits_are_unknown_and_never_unlimited(self):
        current = self.service.accounts.get('org', self.account['account_id'])
        self.service.accounts.patch('org', 'admin', current['account_id'], current['revision'], {'registration': {}})
        q = self.quota()
        self.assertEqual(q['status'], 'unknown')
        self.assertIsNone(q['daily'])
        self.assertIsNone(q['provider_remaining'])

    def test_preflight_is_private_readonly_and_exact_model(self):
        api = TestClient(create_execution(self.service, 'e' * 40))
        self.addCleanup(api.close)
        headers = {'Authorization': 'Bearer ' + 'e' * 40, 'X-Organization-Id': 'org', 'X-Caller-Id': 'video-mcp'}
        body = {'model_id': 'grok-imagine-video-1.5'}
        for _ in range(2):
            result = api.post('/v1/video-preflight', headers=headers, json=body)
            self.assertEqual(result.status_code, 200)
            self.assertTrue(result.json()['can_submit'])
            self.assertNotIn('account_id', result.json())
        self.assertEqual(self.service.calls.list('org')['total'], 0)
        self.assertEqual(api.post('/v1/video-preflight', headers=headers | {'X-Caller-Id': 'image-mcp'}, json=body).status_code, 403)
        self.assertEqual(api.post('/v1/video-preflight', headers=headers, json={'model_id': 'fake'}).status_code, 400)
        other = api.post('/v1/video-preflight', headers=headers | {'X-Organization-Id': 'other'}, json=body)
        self.assertEqual(other.status_code, 409)
        self.assertEqual(other.json()['error'], 'account_required')
        self.service.accepting = False
        self.assertEqual(api.post('/v1/video-preflight', headers=headers, json=body).json()['reason_code'], 'channel_unavailable')

    def test_account_projection_surfaces_limits_and_no_shared_dollar_units(self):
        row, _ = self.reserve('one')
        result = self.service.accounts.get('org', self.account['account_id'])
        self.assertEqual(result['video_quota']['daily']['reserved'], 1)
        self.assertEqual(result['video_quota']['usage_source'], 'local_calls')
        self.assertNotIn('USD', str(result['video_quota']))


if __name__ == '__main__':
    unittest.main()
