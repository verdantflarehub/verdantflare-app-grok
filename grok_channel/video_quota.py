"""Explicit, expiring video limits and account-local observations (never provider balance)."""
from datetime import datetime, time, timedelta, UTC
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .store import now

REASONS = {
    'video_daily_limit_reached': 'The registered daily video limit has been reached.',
    'video_monthly_limit_reached': 'The registered monthly video limit has been reached.',
    'video_concurrency_limit_reached': 'The registered concurrent video limit has been reached.',
    'video_limits_expired': 'Video limit information has expired and must be checked again.',
    'video_limits_not_yet_valid': 'Video limit information is not yet effective.',
    'membership_expired': 'The registered membership has expired.',
    'video_limits_invalid': 'Registered video limits cannot be verified.',
    'account_required': 'A default or explicitly selected account is required.',
    'account_disabled': 'The selected account is disabled.',
    'authorization_not_verified': 'The selected account authorization is not verified.',
}


def instant(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError('invalid_video_limits')
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError('invalid_video_limits') from None
    if result.tzinfo is None:
        raise ValueError('invalid_video_limits')
    return result.astimezone(UTC)


def validate(value):
    if value is None:
        return None
    required = {'source', 'checked_at', 'valid_until', 'timezone', 'period', 'count_policy'}
    counts = {'daily_limit', 'monthly_limit', 'max_concurrent'}
    if (not isinstance(value, dict) or not required <= set(value) or set(value) - required - counts
            or value['source'] not in {'membership_registration', 'local_protection'}
            or value['period'] != 'calendar' or value['count_policy'] != 'accepted_and_unknown'):
        raise ValueError('invalid_video_limits')
    if not isinstance(value['timezone'], str) or len(value['timezone']) > 100:
        raise ValueError('invalid_video_limits')
    try:
        ZoneInfo(value['timezone'])
    except (ValueError, ZoneInfoNotFoundError):
        raise ValueError('invalid_video_limits') from None
    if instant(value['checked_at']) >= instant(value['valid_until']):
        raise ValueError('invalid_video_limits')
    if not any(value.get(key) is not None for key in counts):
        raise ValueError('invalid_video_limits')
    for key in counts:
        if value.get(key) is not None and (type(value[key]) is not int or not 0 <= value[key] <= 1_000_000_000):
            raise ValueError('invalid_video_limits')
    return dict(value)


def _window(rows, start, end, limit):
    rows = [row for row in rows if start <= instant(row['created_at']) < end and row['accepted'] != 0]
    used = sum(row['accepted'] == 1 for row in rows)
    reserved = sum(row['accepted'] is None for row in rows)
    return {'limit': limit, 'used': used, 'reserved': reserved,
            'remaining': None if limit is None else max(0, limit - used - reserved),
            'starts_at': start.isoformat(), 'resets_at': end.isoformat()}


def project(db, account, registration):
    observed = instant(now())
    result = {'status': 'unknown', 'scope': 'channel_account', 'usage_source': 'local_calls',
              'observed_at': observed.isoformat(), 'source': None, 'timezone': None,
              'checked_at': None, 'valid_until': None, 'daily': None, 'monthly': None,
              'concurrent': None, 'reason_code': None, 'reset_at': None,
              'provider_remaining': None}
    raw = registration.get('video_limits')
    if raw is None:
        result['observed_at'] = None
        return result
    try:
        limits = validate(raw)
        tz = ZoneInfo(limits['timezone'])
        local = observed.astimezone(tz)
        daily_start = datetime.combine(local.date(), time.min, tzinfo=tz)
        daily_end = datetime.combine(local.date() + timedelta(days=1), time.min, tzinfo=tz)
        monthly_start = daily_start.replace(day=1)
        monthly_end = monthly_start.replace(year=local.year + 1, month=1) if local.month == 12 else monthly_start.replace(month=local.month + 1)
        rows = db.execute('SELECT accepted,state,created_at FROM channel_calls WHERE organization_id=? '
                          'AND account_id=? AND media=?',
                          (account['organization_id'], account['id'], 'video')).fetchall()
        active = sum(row['accepted'] != 0 and row['state'] not in {'completed', 'failed', 'rejected'} for row in rows)
        concurrent = limits.get('max_concurrent')
        result.update(status='known', source=limits['source'], timezone=limits['timezone'],
                      checked_at=limits['checked_at'], valid_until=limits['valid_until'],
                      count_policy=limits['count_policy'], period=limits['period'],
                      daily=_window(rows, daily_start, daily_end, limits.get('daily_limit')),
                      monthly=_window(rows, monthly_start, monthly_end, limits.get('monthly_limit')),
                      concurrent={'limit': concurrent, 'reserved': active,
                                  'remaining': None if concurrent is None else max(0, concurrent - active)})
        if observed < instant(limits['checked_at']):
            result.update(status='invalid', reason_code='video_limits_not_yet_valid')
        elif observed >= instant(limits['valid_until']):
            result.update(status='expired', reason_code='video_limits_expired')
        expiry = registration.get('membership_expiry')
        if expiry:
            expires = (datetime.combine(datetime.fromisoformat(expiry).date() + timedelta(days=1),
                                        time.min, tzinfo=tz) if len(expiry) == 10 else instant(expiry))
            if observed >= expires:
                result.update(status='expired', reason_code='membership_expired')
        if result['reason_code'] is None:
            for key in ('daily', 'monthly', 'concurrent'):
                if result[key]['limit'] is not None and result[key]['remaining'] == 0:
                    name = 'concurrency' if key == 'concurrent' else key
                    result.update(status='exhausted', reason_code=f'video_{name}_limit_reached',
                                  reset_at=result[key].get('resets_at'))
                    break
    except (ValueError, TypeError, KeyError, ZoneInfoNotFoundError):
        result.update(status='invalid', reason_code='video_limits_invalid')
    return result
