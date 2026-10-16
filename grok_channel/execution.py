"""Grok image generation/editing through the pinned gateway, with durable intent."""
import base64
import io
import json
import os

import httpx
from PIL import Image, ImageOps

from .profile import origin
from .store import Conflict, now

MODELS = {'grok-imagine-image', 'grok-imagine-image-2.0', 'grok-imagine-image-quality'}
ASPECTS = {'16:9', '9:16', '1:1', '4:3', '3:4'}
MAX_BYTES = 32 * 1024 * 1024


def image(value, *, source=False):
    if not isinstance(value, dict) or set(value) != {'mime_type', 'data'}:
        raise ValueError('invalid_image')
    if value['mime_type'] not in {'image/png', 'image/jpeg', 'image/webp'}:
        raise ValueError('invalid_image')
    if not isinstance(value['data'], str) or len(value['data']) > MAX_BYTES * 4 // 3 + 4:
        raise ValueError('invalid_image')
    data = base64.b64decode(value['data'], validate=True)
    if not data or len(data) > (10 * 1024 * 1024 if source else MAX_BYTES):
        raise ValueError('image_size_exceeded')
    try:
        with Image.open(io.BytesIO(data)) as im:
            if (im.format not in {'PNG', 'JPEG', 'WEBP'} or getattr(im, 'n_frames', 1) != 1
                    or im.width * im.height > 16 * 1024 * 1024 or Image.MIME[im.format] != value['mime_type']):
                raise ValueError('invalid_image')
            im.verify()
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            width, height = ImageOps.exif_transpose(im).size
    except (OSError, Image.DecompressionBombError) as error:
        raise ValueError('image_integrity_failed') from error
    return {**value, 'width': width, 'height': height}


def payload(value):
    if not isinstance(value, dict) or set(value) - {'prompt', 'action', 'resolution', 'aspect_ratio', 'image'}:
        raise ValueError('invalid_payload')
    if not isinstance(value.get('prompt'), str) or not value['prompt'].strip() or len(value['prompt']) > 32000:
        raise ValueError('invalid_prompt')
    action, resolution = value.get('action', 'generate'), value.get('resolution', '2k')
    if action not in {'generate', 'edit'} or resolution not in {'1k', '2k'}:
        raise ValueError('unsupported_parameter')
    result = {'prompt': value['prompt'], 'action': action, 'resolution': resolution}
    if action == 'edit':
        if 'aspect_ratio' in value:
            raise ValueError('unsupported_parameter')
        checked = image(value.get('image'), source=True)
        result['image'] = {key: checked[key] for key in ('mime_type', 'data')}
    else:
        if 'image' in value or value.get('aspect_ratio', '16:9') not in ASPECTS:
            raise ValueError('unsupported_parameter')
        result['aspect_ratio'] = value.get('aspect_ratio', '16:9')
    return result


class Execution:
    def __init__(self, calls, client):
        self.calls, self.client = calls, client
        self.models = set(os.environ.get('GROK_IMAGE_MODELS', 'grok-imagine-image-2.0').split(','))
        if not self.models or not self.models <= MODELS:
            raise ValueError('invalid_image_models')
        with calls.store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS channel_results ('
                       'call_id TEXT PRIMARY KEY, ciphertext BLOB NOT NULL, created_at TEXT NOT NULL)')

    def submit(self, organization, caller, operation, task, model, value, account, ceiling):
        if model not in self.models:
            raise ValueError('capability_not_supported')
        value = payload(value)
        row, fresh = self.calls.reserve(organization, caller, operation, task, model, value, account, ceiling,
                                        action=value['action'])
        if not fresh:
            return self.calls.project(row)
        try:
            base = origin(row['api_url'])
            if json.loads(row['proxy_json']) != {'mode': 'direct'}:
                raise ValueError('proxy_unavailable')
            key = self.calls.credential(row)
        except (ValueError, Conflict):
            self.calls.rejected(row['id'])
            raise
        body = {key: value[key] for key in ('prompt', 'resolution')}
        body.update(model=model, n=1, response_format='b64_json')
        if value['action'] == 'edit':
            item = value['image']
            body['image'] = {'url': 'data:' + item['mime_type'] + ';base64,' + item['data']}
            endpoint = '/images/edits'
        else:
            body['aspect_ratio'] = value['aspect_ratio']
            endpoint = '/images/generations'
        try:
            with self.client.stream('POST', base + endpoint, headers={'Authorization': 'Bearer ' + key},
                                    json=body, follow_redirects=False, timeout=300) as result:
                if result.status_code in {400, 401, 403, 422}:
                    self.calls.rejected(row['id'])
                elif result.is_success:
                    raw = bytearray()
                    for part in result.iter_bytes():
                        raw.extend(part)
                        if len(raw) > MAX_BYTES * 4 // 3 + 1024 * 1024:
                            raise ValueError('response_too_large')
                    response = json.loads(raw)
                    if not isinstance(response.get('data'), list) or len(response['data']) != 1:
                        raise ValueError('invalid_response')
                    encoded = response['data'][0]['b64_json']
                    if not isinstance(encoded, str):
                        raise ValueError('invalid_response')
                    decoded = base64.b64decode(encoded, validate=True)
                    with Image.open(io.BytesIO(decoded)) as im:
                        mime = Image.MIME.get(im.format)
                    output = image({'mime_type': mime, 'data': encoded})
                    self.finish(row['id'], output)
        except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError, OSError, Image.DecompressionBombError):
            # 429, 5xx, malformed/truncated output and lost responses are not evidence of non-acceptance.
            # In particular, no automatic re-POST is permitted after process restart.
            pass
        return self.calls.project(self.calls.get(organization, row['id'], caller))

    def finish(self, call_id, output):
        timestamp = now()
        with self.calls.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT state FROM channel_calls WHERE id=?', (call_id,)).fetchone()
            if row is None or row['state'] != 'acceptance_unknown':
                return
            ciphertext = self.calls.store.cipher.encrypt(json.dumps(output).encode())
            db.execute('INSERT INTO channel_results VALUES (?,?,?)', (call_id, ciphertext, timestamp))
            db.execute("UPDATE channel_calls SET state='completed',accepted=1,updated_at=?,finished_at=?,"
                       "revision=revision+1 WHERE id=?", (timestamp, timestamp, call_id))

    def result(self, organization, caller, call_id):
        row = self.calls.get(organization, call_id, caller)
        with self.calls.store.connect() as db:
            result = db.execute('SELECT ciphertext FROM channel_results WHERE call_id=?', (call_id,)).fetchone()
        if result is None:
            raise Conflict('result_not_available')
        return {**self.calls.project(row), 'image': json.loads(self.calls.store.cipher.decrypt(result['ciphertext']))}
