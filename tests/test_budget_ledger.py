import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from grok_channel.accounts import Accounts
from grok_channel.calls import Calls
from grok_channel.store import Store


class BudgetLedgerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        environment = patch.dict('os.environ', {'GROK_ALLOWED_API_ORIGINS': 'https://gateway.example.invalid'})
        environment.start()
        self.addCleanup(environment.stop)
        self.store = Store(str(Path(temporary.name) / 'calls.sqlite3'), Fernet.generate_key().decode())
        accounts = Accounts(self.store)
        account = accounts.create('org', 'admin', 'create', {
            'api_url': 'https://gateway.example.invalid/v1', 'api_key': 'synthetic-test-key',
            'proxy': {'mode': 'direct'}, 'default': True})
        accounts.checked('org', 'admin', account['account_id'], 1, 'active')
        self.calls = Calls(self.store)

    def reserve(self, operation, ceiling='2'):
        return self.calls.reserve('org', 'video-mcp', operation, operation,
                                  'grok-imagine-video', {'prompt': 'test'}, None, ceiling, media='video')

    def test_original_ceiling_survives_rejection_restart_and_idempotent_recovery(self):
        row, created = self.reserve('rejected')
        self.assertTrue(created)
        self.calls.rejected(row['id'], 'provider_rejected')
        self.calls = Calls(self.store)
        replay, created = self.reserve('rejected')
        self.assertFalse(created)
        view = self.calls.project(replay)
        self.assertEqual(view['budget_limit'], {'amount': '2', 'unit': 'USD', 'source': 'caller_limit'})
        self.assertEqual(view['reservation']['amount'], '0')
        self.assertIsNone(view['budget']['amount'])
        self.assertIsNone(view['settlement']['amount'])
        self.assertEqual(self.calls.list('org')['total'], 1)

    def test_limits_reservations_estimates_and_real_settlement_have_independent_totals(self):
        unknown, _ = self.reserve('unknown', '2')
        complete, _ = self.reserve('complete', '3')
        self.calls.accepted(complete['id'], 'provider-video')
        self.calls.complete_video(complete['id'], '0.25')
        view = self.calls.project(self.calls.get('org', complete['id']))
        self.assertEqual(view['budget_limit']['amount'], '3')
        self.assertEqual(view['settlement'], {'amount': '0.25', 'unit': 'USD', 'source': 'provider_reported'})
        summary = self.calls.list('org')['summary']
        self.assertEqual(summary['budget_limit'], {'amount': '5', 'unit': 'USD', 'unknown_count': 0})
        self.assertEqual(summary['reservation']['amount'], '2')
        self.assertEqual(summary['budget'], {'amount': None, 'unit': 'USD', 'unknown_count': 2})
        self.assertEqual(summary['settlement'], {'amount': '0.25', 'unit': 'USD', 'unknown_count': 1})
        self.assertIsNone(self.calls.get('org', unknown['id'])['accepted'])

    def test_legacy_migration_does_not_infer_original_ceiling_from_reservation(self):
        row, _ = self.reserve('legacy')
        self.calls.rejected(row['id'], 'provider_rejected')
        with self.store.connect() as database:
            database.execute('ALTER TABLE channel_calls DROP COLUMN budget_limit_amount')
        self.calls = Calls(self.store)
        replay, created = self.reserve('legacy')
        self.assertFalse(created)
        view = self.calls.project(replay)
        self.assertEqual(view['budget_limit'], {'amount': None, 'unit': 'USD', 'source': 'unknown'})
        self.assertEqual(view['reservation']['amount'], '0')
        self.assertEqual(self.calls.list('org')['summary']['budget_limit']['unknown_count'], 1)


if __name__ == '__main__':
    unittest.main()
