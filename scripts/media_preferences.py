#!/usr/bin/env python3
"""本次/项目/用户媒体偏好；不保存凭据，不授予云调用权限。"""
import argparse
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile

DEFAULTS = {'schema_version': 1, 'revision': 0, 'preferences': {
    'voice': {'provider': 'unconfigured', 'account': '', 'voice_id': '', 'resource_id': ''},
    'image': {'mode': 'unconfigured', 'tool_id': 'auto'}}, 'denied': []}
DENIALS = {'cloud', 'paid_api', 'image_generation', 'reference_upload', 'unknown_cost'}
ROOT = Path(os.getenv('CODEX_HOME', str(Path.home()/'.codex'))) / 'config/huashu-art-motion'


class MediaError(ValueError):
    pass


def read_json(path, missing=False):
    path = Path(path)
    if missing and not path.exists():
        return {}
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise MediaError('重复JSON字段: ' + key)
            result[key] = value
        return result
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'), object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(MediaError('JSON不能含非有限数值')))
    except (OSError, ValueError) as exc:
        raise MediaError('不能读取有效JSON: ' + str(path)) from exc


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode()).hexdigest()


def validate(patch):
    if not isinstance(patch, dict) or set(patch)-{'schema_version', 'revision', 'preferences', 'denied', 'evidence'}:
        raise MediaError('偏好只接受schema_version/revision/preferences/denied/evidence，不接受凭据或授权字段')
    if 'schema_version' in patch and (type(patch['schema_version']) is not int or patch['schema_version'] != 1):
        raise MediaError('schema_version必须为1')
    if 'revision' in patch and (type(patch['revision']) is not int or patch['revision'] < 0):
        raise MediaError('revision必须为非负整数')
    if 'evidence' in patch and (not isinstance(patch['evidence'], str) or not patch['evidence'].strip()):
        raise MediaError('evidence需要真实选择依据')
    denied = patch.get('denied', [])
    if not isinstance(denied, list) or any(not isinstance(v, str) or v not in DENIALS for v in denied) or len(set(denied)) != len(denied):
        raise MediaError('denied必须为不重复的受支持禁止项列表')
    prefs = patch.get('preferences', {})
    if not isinstance(prefs, dict) or set(prefs)-{'voice', 'image'}:
        raise MediaError('preferences仅支持voice/image')
    for capability, fields in prefs.items():
        if not isinstance(fields, dict) or set(fields)-set(DEFAULTS['preferences'][capability]):
            raise MediaError('不支持的偏好字段: ' + capability)
        if any(not isinstance(v, str) or '\n' in v or '\r' in v or len(v) > 128 for v in fields.values()):
            raise MediaError('偏好值须为至多128字符的单行字符串')
        if capability == 'voice':
            if 'provider' in fields and fields['provider'] not in {'unconfigured', 'off', 'provided', 'volcengine', 'dubbingx'}:
                raise MediaError('不支持的声音平台: ' + fields['provider'])
            for field in ('account', 'voice_id', 'resource_id'):
                if field in fields and fields[field] != fields[field].strip():
                    raise MediaError(field + '不能有首尾空白')
        else:
            if 'mode' in fields and fields['mode'] not in {'unconfigured', 'off', 'existing', 'generate'}:
                raise MediaError('不支持的图片模式: ' + fields['mode'])
            if 'tool_id' in fields and not fields['tool_id'].strip():
                raise MediaError('tool_id不能为空')
    return patch


def effective(root=ROOT, project=None, task=None):
    result = copy.deepcopy(DEFAULTS)
    sources = {f'{cap}.{key}': 'public_default' for cap, fields in result['preferences'].items() for key in fields}
    revisions = {}
    denied = set()
    layers = [('user', Path(root)/'media-preferences.json')]
    if project:
        layers.append(('project', Path(project)/'.huashu/media-preferences.json'))
    for label, path in layers + [('task', None)]:
        patch = validate(task or {}) if label == 'task' else validate(read_json(path, missing=True))
        if label != 'task': revisions[label] = patch.get('revision', 0)
        for cap, fields in patch.get('preferences', {}).items():
            target = result['preferences'][cap]
            # Platform changes must not accidentally inherit another platform's ID/account.
            if cap == 'voice' and 'provider' in fields and fields['provider'] != target['provider']:
                target.update(account='', voice_id='', resource_id='')
                sources.update({f'voice.{k}': label for k in ('account', 'voice_id', 'resource_id')})
            for field, value in fields.items():
                target[field] = value
                sources[f'{cap}.{field}'] = label
        denied.update(patch.get('denied', []))
    result['denied'] = sorted(denied)
    return {'configuration': result, 'sources': sources, 'revisions': revisions,
            'note': '偏好不是调用授权；禁止项跨层合并收紧；expected-revision使用对应范围的revisions值'}


@contextmanager
def locked(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name+'.lock').open('a+b') as lock:
        if os.name == 'nt':
            import msvcrt
            if lock.seek(0, 2) == 0:
                lock.write(b'0'); lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock.seek(0); msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def write_json(path, value, exclusive=False):
    path = Path(path)
    encoded = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n').encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    if exclusive:
        with path.open('xb') as output:
            output.write(encoded)
        return
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.media-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(encoded); output.flush(); os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def save(root, patch, scope, evidence, project=None, expected_revision=None, reset=None):
    validate(patch)
    if not isinstance(evidence, str) or not evidence.strip():
        raise MediaError('需要引用用户真实选择/记住/重置的依据，不能自造确认')
    if scope == 'task':
        if reset:
            raise MediaError('本次重置请删除当前任务覆盖项，不改长期配置')
        return {'scope': 'task', 'patch': copy.deepcopy(patch), 'persisted': False,
                'evidence': evidence, 'note': '将此覆盖项留在当前创作记录；没有写用户默认'}
    if scope not in {'user', 'project'} or (scope == 'project' and not project):
        raise MediaError('项目范围必须明确--project；scope只支持task/project/user')
    path = Path(root)/'media-preferences.json' if scope == 'user' else Path(project)/'.huashu/media-preferences.json'
    with locked(path):
        old = validate(read_json(path, missing=True))
        revision = old.get('revision', 0)
        if expected_revision is not None and revision != expected_revision:
            raise MediaError('配置已被其他进程修改，请重读；当前revision=' + str(revision))
        updated = copy.deepcopy(old)
        prefs = updated.setdefault('preferences', {})
        for cap, fields in patch.get('preferences', {}).items():
            previous = prefs.setdefault(cap, {})
            if cap == 'voice' and 'provider' in fields and fields['provider'] != previous.get('provider'):
                previous.update(account='', voice_id='', resource_id='')
            previous.update(fields)
        if 'denied' in patch:
            updated['denied'] = patch['denied']
        if reset:
            if reset not in {'voice', 'image'}:
                raise MediaError('reset仅支持voice/image，不重置禁止项')
            prefs.pop(reset, None)
        updated.update(schema_version=1, revision=revision+1, evidence=evidence)
        validate(updated)
        write_json(path, updated)
    return {'scope': scope, 'persisted': True, 'path': str(path.resolve()), 'revision': revision+1}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--project', type=Path)
    sub = parser.add_subparsers(dest='command', required=True)
    show = sub.add_parser('show'); show.add_argument('--task', type=Path)
    for command in ('set', 'skip', 'reset'):
        item = sub.add_parser(command)
        item.add_argument('--scope', choices=['task', 'project', 'user'], required=True)
        item.add_argument('--evidence', required=True)
        item.add_argument('--expected-revision', type=int)
        if command == 'set': item.add_argument('--file', type=Path, required=True)
        else: item.add_argument('--capability', choices=['voice', 'image'], required=True)
    args = parser.parse_args()
    if args.command == 'show':
        result = effective(args.root, args.project, read_json(args.task) if args.task else None)
    else:
        patch = read_json(args.file) if args.command == 'set' else {}
        if args.command == 'skip':
            patch = {'preferences': {args.capability: {'provider': 'off'} if args.capability == 'voice' else {'mode': 'off'}}}
        result = save(args.root, patch, args.scope, args.evidence, args.project, args.expected_revision,
                      args.capability if args.command == 'reset' else None)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (MediaError, OSError, ValueError, TypeError) as exc:
        raise SystemExit('错误: ' + (str(exc) if isinstance(exc, MediaError) else type(exc).__name__))
