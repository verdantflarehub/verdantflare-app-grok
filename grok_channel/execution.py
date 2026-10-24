"""Grok image and video generation through the pinned gateway, with durable intent."""
import base64
import io
import json
import os
from contextlib import nullcontext
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
from .proxy import ProxyUnavailable

import httpx
from PIL import Image, ImageOps

from .profile import origin
from .store import Conflict, now

MODELS = {'grok-imagine-image', 'grok-imagine-image-2.0', 'grok-imagine-image-quality'}
VIDEO_MODELS = {'grok-imagine-video', 'grok-imagine-video-1.5', 'grok-imagine-video-1.5-lite', 'grok_video_v15_lite'}
ASPECTS = {'16:9', '9:16', '1:1', '4:3', '3:4'}
VIDEO_ASPECTS = {'16:9', '9:16', '1:1', '4:3', '3:4', '3:2', '2:3'}
MAX_BYTES = 32 * 1024 * 1024
MAX_VIDEO_BYTES = 256 * 1024 * 1024


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


def video_payload(value):
    if not isinstance(value, dict) or set(value) - {
        'prompt', 'duration_seconds', 'duration', 'aspect_ratio', 'resolution', 'mode', 'image', 'references'
    }:
        raise ValueError('invalid_payload')
    if not isinstance(value.get('prompt'), str) or not value['prompt'].strip() or len(value['prompt']) > 16000:
        raise ValueError('invalid_prompt')
    duration = value.get('duration_seconds', value.get('duration', 5))
    if type(duration) is not int or not 1 <= duration <= 15:
        raise ValueError('unsupported_parameter')
    aspect_ratio = value.get('aspect_ratio', '16:9')
    resolution = value.get('resolution', '480p')
    mode = value.get('mode', 'text_to_video')
    if aspect_ratio not in VIDEO_ASPECTS or resolution not in {'480p', '720p'} or mode not in {
        'text_to_video', 'image_to_video', 'reference_to_video'
    }:
        raise ValueError('unsupported_parameter')
    result = {
        'prompt': value['prompt'].strip(),
        'duration_seconds': duration,
        'aspect_ratio': aspect_ratio,
        'resolution': resolution,
        'mode': mode,
    }
    if value.get('image') is not None:
        checked = image(value['image'], source=True)
        result['image'] = {key: checked[key] for key in ('mime_type', 'data')}
    if value.get('references') is not None:
        refs = value['references']
        if not isinstance(refs, list) or not 1 <= len(refs) <= 7:
            raise ValueError('unsupported_parameter')
        result['references'] = [
            {key: checked[key] for key in ('mime_type', 'data')}
            for checked in (image(item, source=True) for item in refs)
        ]
    return result


class Execution:
    def __init__(self, calls, client, *, transport=None):
        self.calls, self.client = calls, client
        self.transport = transport
        self.models = set(os.environ.get('GROK_IMAGE_MODELS', 'grok-imagine-image-2.0').split(','))
        if not self.models or not self.models <= MODELS:
            raise ValueError('invalid_image_models')
        self.video_models = {
            m.strip() for m in os.environ.get(
                'GROK_VIDEO_MODELS',
                'grok-imagine-video,grok-imagine-video-1.5,grok-imagine-video-1.5-lite,grok_video_v15_lite'
            ).split(',') if m.strip()
        }
        if not self.video_models or not self.video_models <= VIDEO_MODELS:
            raise ValueError('invalid_video_models')
        self.video_dir = Path(calls.store.path).parent / 'video_results'
        self.video_dir.mkdir(parents=True, exist_ok=True)
        with calls.store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS channel_results ('
                       'call_id TEXT PRIMARY KEY, ciphertext BLOB NOT NULL, created_at TEXT NOT NULL)')

    def submit(self, organization, caller, operation, task, model, value, account, ceiling, *, capability='image'):
        if capability == 'video':
            return self.submit_video(organization, caller, operation, task, model, value, account, ceiling)
        if model not in self.models:
            raise ValueError('capability_not_supported')
        value = payload(value)
        row, fresh = self.calls.reserve(organization, caller, operation, task, model, value, account, ceiling,
                                        action=value['action'], media='image')
        if not fresh:
            return self.calls.project(row)
        try:
            base = origin(row['api_url'])
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
            if self.transport is None and json.loads(row['proxy_json']) != {'mode': 'direct'}:
                raise ProxyUnavailable()
            with self.transport(organization, json.loads(row['proxy_json'])) if self.transport else nullcontext(self.client) as client:
                with client.stream('POST', base + endpoint, headers={'Authorization': 'Bearer ' + key},
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
                        item_data = response['data'][0]
                        encoded = item_data.get('b64_json')
                        if not isinstance(encoded, str) and isinstance(item_data.get('url'), str):
                            downloaded = self._download_asset(client, base, key, item_data['url'], MAX_BYTES)
                            encoded = base64.b64encode(downloaded).decode()
                        if not isinstance(encoded, str):
                            raise ValueError('invalid_response')
                        decoded = base64.b64decode(encoded, validate=True)
                        with Image.open(io.BytesIO(decoded)) as im:
                            mime = Image.MIME.get(im.format)
                        output = image({'mime_type': mime, 'data': encoded})
                        self.finish(row['id'], output)
        except ProxyUnavailable:
            self.calls.rejected(row['id'])
            raise Conflict('proxy_unavailable') from None
        except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError, OSError, Image.DecompressionBombError):
            # 429, 5xx, malformed/truncated output and lost responses are not evidence of non-acceptance.
            # In particular, no automatic re-POST is permitted after process restart.
            pass
        return self.calls.project(self.calls.get(organization, row['id'], caller))

    def submit_video(self, organization, caller, operation, task, model, value, account, ceiling):
        if model not in self.video_models:
            raise ValueError('capability_not_supported')
        value = video_payload(value)
        row, fresh = self.calls.reserve(organization, caller, operation, task, model, value, account, ceiling,
                                        action='generate', media='video')
        if not fresh:
            return self.calls.project(row)
        try:
            base = origin(row['api_url'])
            key = self.calls.credential(row)
        except (ValueError, Conflict):
            self.calls.rejected(row['id'])
            raise
        upstream_model = 'grok-imagine-video' if model == 'grok_video_v15_lite' else model
        body = {
            'model': upstream_model,
            'prompt': value['prompt'],
            'duration': value['duration_seconds'],
            'aspect_ratio': value['aspect_ratio'],
            'resolution': value['resolution'],
        }
        if value.get('image'):
            item = value['image']
            body['image'] = {'url': f"data:{item['mime_type']};base64,{item['data']}"}
        elif value.get('references'):
            body['reference_images'] = [
                {'url': f"data:{item['mime_type']};base64,{item['data']}"}
                for item in value['references']
            ]
        try:
            if self.transport is None and json.loads(row['proxy_json']) != {'mode': 'direct'}:
                raise ProxyUnavailable()
            with self.transport(organization, json.loads(row['proxy_json'])) if self.transport else nullcontext(self.client) as client:
                with client.stream('POST', base + '/videos/generations', headers={'Authorization': 'Bearer ' + key},
                                   json=body, follow_redirects=False, timeout=120) as result:
                    if result.status_code in {400, 401, 403, 422}:
                        self.calls.rejected(row['id'])
                    elif result.is_success:
                        raw = bytearray()
                        for part in result.iter_bytes():
                            raw.extend(part)
                            if len(raw) > 1024 * 1024:
                                raise ValueError('response_too_large')
                        response = json.loads(raw)
                        request_id = response.get('request_id') or response.get('id')
                        if not isinstance(request_id, str) or not request_id.strip():
                            raise ValueError('invalid_response')
                        self.calls.accepted(row['id'], request_id.strip())
                        self.calls.observed(row['id'], 'in_progress')
        except ProxyUnavailable:
            self.calls.rejected(row['id'])
            raise Conflict('proxy_unavailable') from None
        except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError, OSError):
            pass
        return self.calls.project(self.calls.get(organization, row['id'], caller))

    @staticmethod
    def _download_asset(client, base, key, url, max_bytes):
        parsed_base = urlsplit(base)
        target = url
        if target.startswith('/'):
            target = f'{parsed_base.scheme}://{parsed_base.netloc}{target}'
        for _ in range(4):
            parsed = urlsplit(target)
            if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
                raise ValueError('invalid_asset_url')
            same_origin = (parsed.scheme == parsed_base.scheme and parsed.netloc == parsed_base.netloc)
            headers = {'Authorization': 'Bearer ' + key} if same_origin else {}
            with client.stream('GET', target, headers=headers, follow_redirects=False, timeout=180) as res:
                if res.status_code in {301, 302, 303, 307, 308}:
                    location = res.headers.get('location', '')
                    if not location:
                        raise ValueError('redirect_missing_location')
                    target = location if not location.startswith('/') else f'{parsed.scheme}://{parsed.netloc}{location}'
                    continue
                if res.status_code != 200:
                    raise ValueError(f'download_http_{res.status_code}')
                raw = bytearray()
                for chunk in res.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > max_bytes:
                        raise ValueError('asset_size_exceeded')
                if not raw:
                    raise ValueError('empty_asset')
                return bytes(raw)
        raise ValueError('too_many_redirects')

    def refresh(self, organization, caller, call_id):
        row = self.calls.get(organization, call_id, caller)
        if (row.get('media') != 'video' or row['state'] in {'completed', 'failed', 'rejected'}
                or not row.get('provider_request_id')):
            return self.calls.project(row)
        try:
            base = origin(row['api_url'])
            key = self.calls.credential(row)
            if self.transport is None and json.loads(row['proxy_json']) != {'mode': 'direct'}:
                raise ProxyUnavailable()
            with self.transport(organization, json.loads(row['proxy_json'])) if self.transport else nullcontext(self.client) as client:
                request_id = row['provider_request_id']
                with client.stream('GET', f'{base}/videos/{request_id}',
                                   headers={'Authorization': 'Bearer ' + key},
                                   follow_redirects=False, timeout=60) as result:
                    if result.status_code == 404:
                        self.calls.observed(call_id, 'failed')
                        return self.calls.project(self.calls.get(organization, call_id, caller))
                    if not result.is_success and result.status_code != 202:
                        return self.calls.project(row)
                    raw = bytearray()
                    for part in result.iter_bytes():
                        raw.extend(part)
                        if len(raw) > 1024 * 1024:
                            raise ValueError('response_too_large')
                data = json.loads(raw)
                status = str(data.get('status') or '').lower()
                if status in {'failed', 'expired', 'cancelled', 'error'} or data.get('error'):
                    self.calls.observed(call_id, 'failed')
                elif status in {'queued', 'in_queue'}:
                    self.calls.observed(call_id, 'in_queue')
                elif status in {'pending', 'in_progress', 'running', 'processing'}:
                    self.calls.observed(call_id, 'in_progress')
                elif status in {'done', 'completed', 'succeeded'} or isinstance(data.get('video'), dict):
                    video_obj = data.get('video') if isinstance(data.get('video'), dict) else {}
                    video_url = video_obj.get('url') or data.get('url') or f'{base}/videos/{request_id}/content'
                    try:
                        video_bytes = self._download_asset(client, base, key, video_url, MAX_VIDEO_BYTES)
                    except Exception:
                        video_bytes = self._download_asset(
                            client, base, key, f'{base}/videos/{request_id}/content', MAX_VIDEO_BYTES
                        )
                    if b'ftyp' not in video_bytes[:32]:
                        raise ValueError('invalid_video_payload')
                    target_path = self.video_dir / f'{call_id}.bin'
                    temp_path = self.video_dir / f'{call_id}.bin.tmp'
                    temp_path.write_bytes(self.calls.store.cipher.encrypt(video_bytes))
                    temp_path.replace(target_path)
                    settlement = None
                    ticks = (data.get('usage') or {}).get('cost_in_usd_ticks') if isinstance(data.get('usage'), dict) else None
                    if isinstance(ticks, (int, float)) and ticks >= 0:
                        settlement = format(Decimal(str(ticks)) / Decimal('10000000000'), 'f')
                    self.calls.complete_video(call_id, settlement)
        except ProxyUnavailable:
            raise Conflict('proxy_unavailable') from None
        except (httpx.HTTPError, ValueError, TypeError, KeyError, OSError):
            pass
        return self.calls.project(self.calls.get(organization, call_id, caller))

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

    def video_result(self, organization, caller, call_id) -> bytes:
        row = self.calls.get(organization, call_id, caller)
        if row.get('state') != 'completed':
            self.refresh(organization, caller, call_id)
            row = self.calls.get(organization, call_id, caller)
        path = self.video_dir / f'{call_id}.bin'
        if row.get('state') != 'completed' or not path.is_file():
            raise Conflict('result_not_available')
        return self.calls.store.cipher.decrypt(path.read_bytes())
