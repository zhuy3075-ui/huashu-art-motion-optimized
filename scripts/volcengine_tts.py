#!/usr/bin/env python3
"""Volcano/Doubao 2.0 async TTS with real timestamps and account-scoped voices.

Standard library only. ffprobe measures MP3 duration; ffmpeg checks full decoding.
API credentials are read from VOLC_TTS_API_KEY or Windows CurrentUser DPAPI.
"""
import argparse
import base64
import ctypes
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

BASE_URL = 'https://openspeech.bytedance.com/api/v3/tts'
RESOURCES = {'seed-tts-2.0', 'seed-icl-2.0'}
MAX_AUDIO_BYTES = 128 * 1024 * 1024

class TtsError(Exception):
    pass

def now():
    return datetime.now(timezone.utc).isoformat()

def load_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise TtsError(f"重复JSON字段: {key}")
            result[key] = value
        return result
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"), object_pairs_hook=unique,
                          parse_constant=lambda value: (_ for _ in ()).throw(TtsError(f"非法数值: {value}")))
    except (ValueError, OSError) as exc:
        raise TtsError(f"不能读取JSON文件: {path}") from exc

def write_json(path, value, exclusive=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if exclusive:
        try:
            with path.open("xb") as output:
                output.write(encoded)
        except FileExistsError as exc:
            raise TtsError(f"文件已存在，不能覆盖: {path}") from exc
        return
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".tts-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)

def identifier(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise TtsError(f"{label}必须为1–128位字母、数字、下划线或连字符")
    if value.upper().startswith("YOUR_"):
        raise TtsError(f"请填写真实{label}，不能使用模板占位符")
    return value

def number(value, label, low=None, high=None):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise TtsError(f"{label}必须为有限数值")
    if (low is not None and value < low) or (high is not None and value > high):
        raise TtsError(f"{label}超出允许范围")
    return value

def dpapi(data, decrypt=False):
    """Windows CurrentUser protection; no fallback plaintext credential store."""
    if os.name != "nt":
        raise TtsError("本地加密凭据仅支持Windows；其他系统用环境变量，不回退明文")
    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("bytes", ctypes.POINTER(ctypes.c_ubyte))]
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source, output = Blob(len(data), buffer), Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(Blob)]
    function.restype = ctypes.c_int
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise TtsError("Windows凭据加密/解密失败；须由保存时的本机用户读取")
    kernel.LocalFree.argtypes, kernel.LocalFree.restype = [ctypes.c_void_p], ctypes.c_void_p
    try:
        return ctypes.string_at(output.bytes, output.size)
    finally:
        kernel.LocalFree(output.bytes)

def public_https(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise TtsError("音频下载仅允许无用户凭据的HTTPS地址")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise TtsError("音频主机DNS解析失败") from exc
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise TtsError("拒绝内网/本机音频下载地址")

class AudioRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

def download_audio(url, output, file_format, timeout):
    output = Path(output)
    if output.exists():
        raise TtsError(f"音频文件已存在，不能覆盖: {output}")
    public_https(url)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=output.parent, suffix="." + file_format)
    try:
        # A separate unauthenticated request: NEVER forward X-Api-Key to audio_url.
        with urllib.request.build_opener(AudioRedirect()).open(url, timeout=timeout) as response, os.fdopen(fd, "wb") as file:
            fd = None
            size = 0
            while chunk := response.read(65536):
                size += len(chunk)
                if size > MAX_AUDIO_BYTES:
                    raise TtsError("音频超过128MiB限制；请按已确认段落拆分")
                file.write(chunk)
        duration = audio_duration(temp, file_format)
        digest = hashlib.sha256(Path(temp).read_bytes()).hexdigest()
        # Exclusive creation protects existing user audio, even under concurrent calls.
        os.link(temp, output)
        return {"path": str(output.resolve()), "sha256": digest, "duration_seconds": duration}
    except (urllib.error.URLError, OSError) as exc:
        raise TtsError("音频下载/保存失败；可恢复原任务查询，不要重复合成") from exc
    finally:
        if fd is not None:
            os.close(fd)
        if os.path.exists(temp):
            os.unlink(temp)


def audio_duration(path, file_format='mp3'):
    try:
        probe = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0',
            '-show_entries', 'stream=codec_name:format=duration', '-of', 'json', str(path)],
            capture_output=True, text=True, timeout=30, check=True)
        info = json.loads(probe.stdout)
        if not info.get('streams') or info['streams'][0].get('codec_name') != 'mp3':
            raise TtsError('下载文件不是有效 MP3')
        duration = number(float(info['format']['duration']), '音频时长', 0.000001)
        subprocess.run(['ffmpeg', '-v', 'error', '-xerror', '-i', str(path),
            '-map', '0:a:0', '-f', 'null', '-'], capture_output=True, timeout=60, check=True)
        return duration
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        raise TtsError('MP3完整解码校验失败；需要ffprobe与ffmpeg') from exc


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise TtsError('拒绝API重定向，保护X-Api-Key')


class Client:
    def __init__(self, key, timeout=30):
        if not isinstance(key, str) or not re.fullmatch(r'[!-~]+', key):
            raise TtsError('请在本机设置VOLC_TTS_API_KEY或configure加密保存')
        self.key, self.timeout = key, timeout
        self.opener = urllib.request.build_opener(NoRedirect())

    def post(self, action, payload, resource):
        if action not in {'submit', 'query'} or resource not in RESOURCES:
            raise TtsError('未知API操作或资源ID')
        request = urllib.request.Request(BASE_URL + '/' + action,
            data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8'),
            headers={'Content-Type': 'application/json', 'X-Api-Key': self.key,
                     'X-Api-Resource-Id': resource, 'X-Api-Request-Id': str(uuid.uuid4())}, method='POST')
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(16 * 1024 * 1024 + 1)
                if len(raw) > 16 * 1024 * 1024:
                    raise TtsError('响应过大；请按已确认镜头拆分')
                result = json.loads(raw)
        except urllib.error.HTTPError as exc:
            raise TtsError(f'火山HTTP错误 {exc.code}；不自动重试合成') from exc
        except (urllib.error.URLError, ValueError, TimeoutError) as exc:
            raise TtsError('火山请求未取得有效响应；通过原task_id恢复查询') from exc
        if not isinstance(result, dict) or result.get('code') != 20000000:
            code = result.get('code') if isinstance(result, dict) else None
            raise TtsError(f'火山业务错误 code={code}；请核对控制台权限和资源')
        if not isinstance(result.get('data'), dict):
            raise TtsError('火山响应缺少data对象')
        return result['data']


def fingerprint(key):
    return hashlib.sha256(key.encode()).hexdigest()


def account_dir(root, account, key=None):
    directory = Path(root) / 'volcengine' / identifier(account, '账号别名')
    if key is not None:
        binding = directory / 'account.json'
        if not binding.exists():
            write_json(binding, {'key_fingerprint': fingerprint(key)}, exclusive=True)
        if load_json(binding).get('key_fingerprint') != fingerprint(key):
            raise TtsError('账号别名绑定了另一Key；请使用新的别名隔离历史')
    return directory


def credentials(directory):
    key = os.environ.get('VOLC_TTS_API_KEY', '')
    if key:
        return key
    path = directory / 'credentials.dpapi.json'
    if not path.exists():
        return ''
    try:
        store = load_json(path)
        if store.get('protection') != 'windows-dpapi-current-user':
            raise TtsError('不支持的凭据格式')
        return dpapi(base64.b64decode(store['ciphertext'], validate=True), decrypt=True).decode('utf-8')
    except (ValueError, KeyError) as exc:
        raise TtsError('本地加密凭据无效') from exc


def validate_voice(voice):
    if not isinstance(voice, dict) or set(voice) - {'speaker', 'name', 'resource_id', 'source', 'preview_url'}:
        raise TtsError('音色需为speaker/name/resource_id/source/可选preview_url对象')
    identifier(voice.get('speaker'), 'speaker')
    if voice.get('resource_id') not in RESOURCES or voice.get('source') not in {'official', 'custom'}:
        raise TtsError('音色资源或来源无效')
    expected = 'seed-icl-2.0' if voice['source'] == 'custom' else 'seed-tts-2.0'
    if voice['resource_id'] != expected:
        raise TtsError('custom须使用seed-icl-2.0，official须使用seed-tts-2.0')
    if not isinstance(voice.get('name'), str) or not voice['name'].strip():
        raise TtsError('音色名称不能为空')
    if voice.get('preview_url'):
        url = urllib.parse.urlsplit(voice['preview_url'])
        if url.scheme != 'https' or not url.hostname or url.username or url.password:
            raise TtsError('试听地址须为无凭据HTTPS地址')
    return voice


def import_voices(directory, values):
    if not isinstance(values, list) or not values:
        raise TtsError('音色导入文件须为非空数组')
    voices = {}
    path = directory / 'voices.json'
    if path.exists():
        voices = {v['speaker']: v for v in load_json(path)['voices']}
    incoming = set()
    for voice in values:
        validate_voice(voice)
        if voice['speaker'] in incoming:
            raise TtsError('导入文件存在重复speaker')
        incoming.add(voice['speaker'])
        voices[voice['speaker']] = voice
    store = {'schema_version': 1, 'source': 'console_import_not_cloud_query',
             'imported_at': now(), 'voices': list(voices.values())}
    write_json(path, store)
    return store


def normalize(text):
    # Ignore only ordinary reading punctuation, not +, %, currency or emoji.
    ignored = '。！？,.!?；;、:："“”\'‘’「」『』《》（）()[]【】'
    return ''.join(c for c in unicodedata.normalize('NFKC', text).casefold()
                   if not c.isspace() and c not in ignored)


def validate_request(payload):
    if not isinstance(payload, dict) or set(payload) - {'speaker', 'resource_id', 'segments', 'audio_params'}:
        raise TtsError('请求须包含speaker/resource_id/segments/audio_params，不接受未实现字段')
    identifier(payload.get('speaker'), 'speaker')
    if payload.get('resource_id') not in RESOURCES:
        raise TtsError('resource_id必须为seed-tts-2.0或seed-icl-2.0')
    segments = payload.get('segments')
    if not isinstance(segments, list) or not segments:
        raise TtsError('segments须为已确认字幕/镜头片段数组')
    for segment in segments:
        if not isinstance(segment, dict) or set(segment) - {'text', 'spoken_text'}:
            raise TtsError('片段仅包含text及可选的已确认spoken_text')
        for text in segment.values():
            if not isinstance(text, str) or not normalize(text) or any(ord(c) < 32 and c not in '\n\t' for c in text):
                raise TtsError('片段须包含可朗读内容，不能含非法控制字符')
        if 'text' not in segment:
            raise TtsError('片段缺少原字幕text')
        if re.search(r'\n[ \t]*\n', segment['text']):
            raise TtsError('字幕片段不能含空白行；请拆成多个segments以保持SRT兼容')
    text = ''.join(s.get('spoken_text', s['text']) for s in segments)
    if len(text) > 100000:
        raise TtsError('超过100000字符；按已确认镜头拆分')
    audio = payload.get('audio_params')
    if not isinstance(audio, dict) or set(audio) - {'format', 'sample_rate', 'speech_rate', 'loudness_rate', 'enable_timestamp'}:
        raise TtsError('audio_params字段不支持')
    if audio.get('format') != 'mp3' or audio.get('enable_timestamp') is not True:
        raise TtsError('本适配器要求MP3与enable_timestamp=true')
    if type(audio.get('sample_rate')) is not int or audio['sample_rate'] not in {8000,16000,22050,24000,32000,44100,48000}:
        raise TtsError('sample_rate无效')
    for field in ('speech_rate', 'loudness_rate'):
        if field in audio:
            if type(audio[field]) is not int:
                raise TtsError(f'{field}须为整数')
            number(audio[field], field, -50, 100)
    return text


def submit(client, payload, job_path, directory, account, approval):
    text = validate_request(payload)
    if not isinstance(approval, str) or not approval.strip():
        raise TtsError('必须记录文案、音色和此次生成授权依据')
    if Path(job_path).exists():
        raise TtsError('任务文件已存在；请恢复查询，不能重复提交')
    voices = load_json(directory / 'voices.json')['voices'] if (directory / 'voices.json').exists() else []
    voice = next((v for v in voices if v['speaker'] == payload['speaker']), None)
    if voice is None or validate_voice(voice)['resource_id'] != payload['resource_id']:
        raise TtsError('音色未从控制台导入本账号，或资源ID不匹配；不得自动换音色')
    task_id = str(uuid.uuid4())
    job = {'schema_version': 1, 'provider': 'volcengine', 'task_id': task_id,
           'account': account, 'key_fingerprint': fingerprint(client.key), 'created_at': now(),
           'state': 'submitting', 'approval': approval, 'voice': voice, 'request': payload,
           'request_sha256': fingerprint(json.dumps(payload, ensure_ascii=False, sort_keys=True))}
    write_json(job_path, job, exclusive=True)
    params = {'text': text, 'speaker': payload['speaker'], 'audio_params': payload['audio_params']}
    if payload['resource_id'] == 'seed-icl-2.0':
        params['model'] = 'seed-tts-2.0-standard'
    try:
        data = client.post('submit', {'user': {'uid': account}, 'unique_id': task_id,
                           'req_params': params}, payload['resource_id'])
        if data.get('task_id') != task_id:
            raise TtsError('返回task_id与unique_id不一致；保留记录并核对控制台')
    except Exception:
        job['state'] = 'submission_unknown'
        write_json(job_path, job)
        raise
    job['state'] = 'submitted'
    write_json(job_path, job)
    return job


def build_timeline(payload, data, duration):
    """Match actual returned words strictly; never interpolate missing characters."""
    validate_request(payload)
    number(duration, '实测时长', 0.000001)
    sentences = data.get('sentences')
    if not isinstance(sentences, list) or not sentences:
        raise TtsError('结果没有句级/字级时间戳；不能用估算代替')
    words, previous = [], 0
    for sentence in sentences:
        if not isinstance(sentence, dict) or not isinstance(sentence.get('text'), str):
            raise TtsError('无效分句结构')
        start = number(sentence.get('startTime'), '句起点', 0, duration + 0.05)
        end = number(sentence.get('endTime'), '句终点', start, duration + 0.05)
        if start < previous - 0.001:
            raise TtsError('分句时间倒序或重叠')
        items = sentence.get('words')
        if not isinstance(items, list) or not items:
            raise TtsError('分句没有真实字级时间戳')
        joined = ''
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get('word'), str) or not item['word']:
                raise TtsError('无效字级结构')
            ws = number(item.get('startTime'), '字起点', start, end)
            we = number(item.get('endTime'), '字终点', ws, end)
            if normalize(item['word']) and we <= ws:
                raise TtsError('有朗读内容的字级时间区间必须为正')
            if ws < previous - 0.001:
                raise TtsError('字级时间倒序或重叠')
            confidence = item.get('confidence')
            if confidence is not None:
                number(confidence, 'confidence', 0, 1)
            words.append({'w': item['word'], 's': ws, 'e': we, 'confidence': confidence})
            previous = we
            joined += item['word']
        if normalize(joined) != normalize(sentence['text']):
            raise TtsError('分句文字与字级结果不一致')
        previous = end
    normalized = ''.join(normalize(w['w']) for w in words)
    if normalized != normalize(validate_request(payload)):
        raise TtsError('实际朗读文字与提交文字不一致；核对数字/缩写读法并保留原任务，不能自动重合成')
    captions, cursor = [], 0
    for segment in payload['segments']:
        target = len(normalize(segment.get('spoken_text', segment['text'])))
        selected, consumed = [], 0
        while cursor < len(words) and consumed < target:
            word = words[cursor]
            selected.append(word)
            consumed += len(normalize(word['w']))
            cursor += 1
        if consumed != target or not selected:
            raise TtsError('片段边界切开了平台词组；请用完整词组边界设计字幕')
        captions.append({'text': segment['text'], 'spoken_text': segment.get('spoken_text', segment['text']),
                         'start': selected[0]['s'], 'end': selected[-1]['e']})
        if captions[-1]['end'] <= captions[-1]['start']:
            raise TtsError('字幕时间区间必须为正')
    # Trailing punctuation-only items have no display characters; keep in raw wordList.
    if any(normalize(w['w']) for w in words[cursor:]):
        raise TtsError('存在未匹配的朗读词')
    return {'schema_version': 1, 'provider': 'volcengine', 'time_unit': 'seconds',
            'duration': duration, 'segments': [s['text'] for s in payload['segments']],
            'segmentStarts': [c['start'] for c in captions], 'wordList': words,
            'sentences': sentences, 'captions': captions, 'alignment': 'exact_normalized_no_interpolation'}


def srt(timeline):
    def stamp(value):
        ms = round(value * 1000)
        return f'{ms//3600000:02}:{ms//60000%60:02}:{ms//1000%60:02},{ms%1000:03}'
    return '\n'.join(f"{i}\n{stamp(c['start'])} --> {stamp(c['end'])}\n{c['text']}\n"
                     for i, c in enumerate(timeline['captions'], 1))


@contextmanager
def job_lock(path):
    lock = Path(str(path) + '.lock')
    try:
        with lock.open('x') as file:
            file.write(str(os.getpid()))
    except FileExistsError as exc:
        raise TtsError('任务正在处理；中断遗留锁须核对进程后人工移除') from exc
    try:
        yield
    finally:
        lock.unlink()


def verify_artifact(artifact):
    path = Path(artifact['path'])
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != artifact['sha256']:
        raise TtsError('本地已保存产物缺失或被修改')


def record_success(directory, job):
    event = {k: job[k] for k in ('task_id', 'voice', 'completed_at', 'audio', 'timeline', 'subtitle')}
    event['preference'] = 'generated_only_not_user_approved'
    event['request_sha256'] = job['request_sha256']
    path = directory / 'history' / (job['task_id'] + '.json')
    if path.exists():
        if load_json(path) != event:
            raise TtsError('成功历史证据冲突')
    else:
        write_json(path, event, exclusive=True)


def wait_job(client, job_path, directory, account, output, seconds=45, interval=3):
    number(seconds, '查询时间预算', 0, 60)
    number(interval, '查询间隔', 0.1, 60)
    with job_lock(job_path):
        job = load_json(job_path)
        if job.get('provider') != 'volcengine' or job.get('account') != account or job.get('key_fingerprint') != fingerprint(client.key):
            raise TtsError('任务属于另一平台/账号/API Key')
        if fingerprint(json.dumps(job['request'], ensure_ascii=False, sort_keys=True)) != job['request_sha256']:
            raise TtsError('任务请求被修改；请核对原已确认版本')
        if job['state'] == 'completed':
            for field in ('audio', 'timeline', 'subtitle'):
                verify_artifact(job[field])
            record_success(directory, job)
            return job
        if job['state'] == 'failed':
            raise TtsError('任务已失败；核对原因后另行获得生成授权')
        output = Path(output).resolve()
        if output.suffix.lower() != '.mp3':
            raise TtsError('输出音频须为.mp3')
        timeline_path, subtitle_path = output.with_suffix('.json'), output.with_suffix('.srt')
        if 'audio' not in job and (timeline_path.exists() or subtitle_path.exists()):
            raise TtsError('字幕/时间轴文件已存在；不得覆盖已有产物')
        if 'audio' in job:
            verify_artifact(job['audio'])
            if Path(job['audio']['path']) != output:
                raise TtsError('恢复任务必须沿用原音频输出路径')
        elif output.exists():
            raise TtsError('音频路径已存在且不属于此任务')
        deadline = time.monotonic() + seconds
        while True:
            data = client.post('query', {'task_id': identifier(job['task_id'], 'task_id')}, job['request']['resource_id'])
            if data.get('task_id') != job['task_id'] or type(data.get('task_status')) is not int or data['task_status'] not in {1,2,3}:
                raise TtsError('查询返回任务ID或状态异常')
            job['queried_at'], job['task_status'] = now(), data['task_status']
            if data['task_status'] == 3:
                job['state'] = 'failed'
                write_json(job_path, job)
                raise TtsError('火山任务合成失败；不自动重新提交')
            if data['task_status'] == 2:
                write_json(Path(str(job_path) + '.result.json'), data)
                if 'audio' not in job:
                    if not data.get('audio_url'):
                        raise TtsError('成功任务缺少音频地址；通过原任务再次查询')
                    job['audio'] = download_audio(data['audio_url'], output, 'mp3', client.timeout)
                    job['state'] = 'downloaded'
                    write_json(job_path, job)
                timeline = build_timeline(job['request'], data, job['audio']['duration_seconds'])
                if timeline_path.exists():
                    if load_json(timeline_path) != timeline:
                        raise TtsError('现有时间轴与原任务不符；不得覆盖')
                else:
                    write_json(timeline_path, timeline, exclusive=True)
                subtitles = srt(timeline)
                if subtitle_path.exists():
                    if subtitle_path.read_text(encoding='utf-8') != subtitles:
                        raise TtsError('现有字幕与原任务不符；不得覆盖')
                else:
                    with subtitle_path.open('x', encoding='utf-8') as file:
                        file.write(subtitles)
                for field, path in (('timeline', timeline_path), ('subtitle', subtitle_path)):
                    job[field] = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                job['state'], job['completed_at'] = 'completed', now()
                write_json(job_path, job)
                record_success(directory, job)
                return job
            job['state'] = 'running'
            write_json(job_path, job)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return job
            time.sleep(min(interval, remaining))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(os.getenv('CODEX_HOME', str(Path.home()/'.codex'))) / 'config' / 'huashu-art-motion')
    parser.add_argument('--account', default='default')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('configure', help='将本机环境变量中的API Key用Windows DPAPI保存；不发起合成')
    cmd = commands.add_parser('import-voices'); cmd.add_argument('--file', type=Path, required=True)
    commands.add_parser('voices'); commands.add_parser('history')
    cmd = commands.add_parser('submit')
    cmd.add_argument('--request', type=Path, required=True); cmd.add_argument('--job', type=Path, required=True)
    cmd.add_argument('--approval', required=True)
    cmd = commands.add_parser('wait')
    cmd.add_argument('--job', type=Path, required=True); cmd.add_argument('--out', type=Path, required=True)
    cmd.add_argument('--seconds', type=float, default=45); cmd.add_argument('--interval', type=float, default=3)
    cmd = commands.add_parser('timeline', help='离线验证原任务结果与本地MP3并导出时间轴/SRT')
    for flag in ('request', 'result', 'audio', 'out'):
        cmd.add_argument('--'+flag, type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        directory = account_dir(args.root, args.account)
        if args.command == 'timeline':
            if args.out.suffix.lower() != '.json':
                raise TtsError('离线时间轴输出须为.json，SRT由脚本生成同名文件')
            result = load_json(args.result)
            if 'data' in result:
                if result.get('code') != 20000000:
                    raise TtsError('离线结果业务码异常')
                result = result['data']
            if result.get('task_status') != 2:
                raise TtsError('离线结果须为已成功任务')
            timeline = build_timeline(load_json(args.request), result, audio_duration(args.audio))
            if args.out.with_suffix('.srt').exists():
                raise TtsError('SRT已存在')
            write_json(args.out, timeline, exclusive=True)
            with args.out.with_suffix('.srt').open('x', encoding='utf-8') as file:
                file.write(srt(timeline))
            value = {'timeline': str(args.out.resolve())}
        elif args.command in {'voices', 'history'}:
            env_key = os.environ.get('VOLC_TTS_API_KEY', '')
            if env_key:
                Client(env_key)  # Validate header-safe key without a network request.
                directory = account_dir(args.root, args.account, env_key)
            if args.command == 'voices':
                value = load_json(directory/'voices.json') if (directory/'voices.json').exists() else {'voices': []}
            else:
                value = [load_json(p) for p in sorted((directory/'history').glob('*.json'))]
        else:
            key = credentials(directory)
            client = Client(key)
            directory = account_dir(args.root, args.account, key)
            if args.command == 'configure':
                write_json(directory/'credentials.dpapi.json', {'protection': 'windows-dpapi-current-user',
                           'ciphertext': base64.b64encode(dpapi(key.encode())).decode('ascii')})
                value = {'account': args.account, 'configured': True, 'cloud_verified': False}
            elif args.command == 'import-voices':
                value = import_voices(directory, load_json(args.file))
            elif args.command == 'submit':
                value = submit(client, load_json(args.request), args.job, directory, args.account, args.approval)
            else:
                value = wait_job(client, args.job, directory, args.account, args.out, args.seconds, args.interval)
        # Never echo credentials, full script, signed download URLs or external error bodies.
        if args.command in {'submit', 'wait'}:
            value = {k: value[k] for k in ('task_id', 'state', 'audio', 'timeline', 'subtitle') if k in value}
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    except (TtsError, OSError, ValueError, KeyError, TypeError) as exc:
        print('错误: '+str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
