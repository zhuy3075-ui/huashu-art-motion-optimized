#!/usr/bin/env python3
"""本地批量创作队列：版本确认、串行领取、失败续做；不调用云端或执行命令。"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import shutil
import uuid

from media_preferences import MediaError, locked, read_json, write_json

STAGES = ('brief', 'outline', 'script', 'storyboard', 'voice', 'pilot', 'film')
DEFAULTS = {'style_ref': '', 'character_ref': '', 'notes': '', 'voice': {
    'provider': 'unconfigured', 'account': '', 'voice_id': '', 'resource_id': ''}}
PROVIDERS = {'unconfigured', 'off', 'provided', 'volcengine', 'dubbingx'}
ID = re.compile(r'[a-z][a-z0-9-]{0,47}\Z')
RESERVED = {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)), *(f'lpt{i}' for i in range(1, 10))}


class BatchError(MediaError):
    pass


def text(value, field, limit=10000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise BatchError(f'{field}需要非空文本，最多{limit}字符')
    return value


def settings(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise BatchError('配置只接受style_ref/character_ref/notes/voice，不接受凭据或命令')
    for key, item in value.items():
        if key == 'voice':
            if not isinstance(item, dict) or set(item) - set(DEFAULTS['voice']):
                raise BatchError('voice仅接受provider/account/voice_id/resource_id')
            if 'provider' in item and item['provider'] not in PROVIDERS:
                raise BatchError('未知配音平台: ' + str(item['provider']))
            if any(not isinstance(v, str) or len(v) > 256 or '\n' in v or '\r' in v for v in item.values()):
                raise BatchError('声音字段须为至多256字符的单行字符串')
        elif not isinstance(item, str) or len(item) > 10000:
            raise BatchError(key + '须为至多10000字符的字符串')
    return value


def merge_settings(base, override):
    result = copy.deepcopy(base)
    for key, value in settings(override).items():
        if key == 'voice':
            if 'provider' in value and value['provider'] != result['voice']['provider']:
                result['voice'] = copy.deepcopy(DEFAULTS['voice'])
            result['voice'].update(value)
        else:
            result[key] = value
    return result


def manifest(value):
    if not isinstance(value, dict) or set(value) - {'schema_version', 'name', 'defaults', 'jobs'}:
        raise BatchError('清单仅接受schema_version/name/defaults/jobs')
    if type(value.get('schema_version')) is not int or value['schema_version'] != 1:
        raise BatchError('schema_version须为1')
    text(value.get('name'), 'name', 200)
    defaults = merge_settings(DEFAULTS, value.get('defaults', {}))
    jobs = value.get('jobs')
    if not isinstance(jobs, list) or not 1 <= len(jobs) <= 100:
        raise BatchError('jobs须含1至100项')
    seen = set()
    for job in jobs:
        if not isinstance(job, dict) or set(job) - {'id', 'topic', 'settings'}:
            raise BatchError('每项任务仅接受id/topic/settings')
        ident = job.get('id')
        if not isinstance(ident, str) or not ID.fullmatch(ident) or ident in RESERVED or ident in seen:
            raise BatchError('重复或无效任务id: ' + str(ident))
        seen.add(ident)
        text(job.get('topic'), 'topic')
        settings(job.get('settings', {}))
    return defaults, jobs


def state_path(project):
    project = Path(project).resolve()
    path = project / '.huashu' / 'batch.json'
    if not path.resolve().is_relative_to(project):
        raise BatchError('批量状态目录不能指向项目外')
    return path


def job_root(project, ident):
    project = Path(project).resolve()
    path = project / '.huashu' / 'batch' / 'jobs' / ident
    if not path.resolve().is_relative_to(project / '.huashu' / 'batch' / 'jobs'):
        raise BatchError('任务目录不能越界: ' + ident)
    return path.resolve()


def file_hash(path):
    hasher = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            hasher.update(chunk)
    return hasher.hexdigest()


def artifact_path(project, job, relative):
    root = job_root(project, job['id'])
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise BatchError('产物路径须相对于本任务目录')
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size == 0:
        raise BatchError('产物缺失、为空或越界: ' + relative)
    return path


def load(project):
    value = read_json(state_path(project))
    if not isinstance(value, dict) or value.get('schema_version') != 1 or type(value.get('revision')) is not int:
        raise BatchError('无效队列状态')
    manifest({'schema_version': 1, 'name': value.get('name'), 'jobs': [
        {'id': j['id'], 'topic': j['topic'], 'settings': j['settings']} for j in value['jobs']]})
    for job in value['jobs']:
        if set(job['stages']) - set(STAGES):
            raise BatchError('状态中存在未知阶段')
        for stage, record in job['stages'].items():
            if (record.get('status') not in {'pending', 'confirmed'} or
                    type(record.get('version')) is not int or record['version'] < 1 or
                    not re.fullmatch(r'[0-9a-f]{64}', record.get('sha256', ''))):
                raise BatchError('无效阶段记录: ' + stage)
    return value


def lookup(state, ident):
    for job in state['jobs']:
        if job['id'] == ident:
            return job
    raise BatchError('任务不存在: ' + str(ident))


def current(project, job):
    # A later stage cannot hide a changed earlier approval or missing output.
    for stage in STAGES:
        record = job['stages'].get(stage)
        if record:
            try:
                valid = all(file_hash(artifact_path(project, job, record[key])) == record['sha256']
                            for key in ('artifact', 'snapshot'))
            except (OSError, BatchError):
                valid = False
            if not valid:
                return {'action': 'stale_artifact', 'stage': stage}
        if not record or record['status'] != 'confirmed':
            if job['operation']:
                return {'action': 'recover_operation' if job['operation']['status'] in {'running', 'unknown'} else 'blocked',
                        'stage': stage, 'operation': copy.deepcopy(job['operation'])}
            return {'action': 'await_confirmation' if record else 'create', 'stage': stage,
                    'record': copy.deepcopy(record)}
    return {'action': 'completed', 'stage': None}


def summary(project, state):
    rows = []
    for job in state['jobs']:
        rows.append({'id': job['id'], 'topic': job['topic'], 'directory': str(job_root(project, job['id'])),
                     'settings': job['settings'], **current(project, job)})
    return {'name': state['name'], 'revision': state['revision'], 'jobs': rows,
            'note': '确认记录不是身份认证或云调用授权；真实生成由Codex按对应指南调用'}


def next_job(project, state):
    rows = summary(project, state)['jobs']
    # An unknown/running external operation must be recovered before dispatching another.
    active = next((j for j in state['jobs'] if j['operation'] and j['operation']['status'] in {'running', 'unknown'}), None)
    if active:
        return next(row for row in rows if row['id'] == active['id'])
    runnable = next((row for row in rows if row['action'] == 'create'), None)
    return runnable or {'action': 'waiting' if any(r['action'] != 'completed' for r in rows) else 'completed',
                        'jobs': rows}


def initialize(project, value):
    defaults, jobs = manifest(value)
    path = state_path(project)
    with locked(path):
        if path.exists():
            raise BatchError('队列已存在，请用status/next续做，不能覆盖')
        state = {'schema_version': 1, 'revision': 1, 'name': value['name'], 'jobs': []}
        for item in jobs:
            root = job_root(project, item['id'])
            root.mkdir(parents=True, exist_ok=True)
            state['jobs'].append({'id': item['id'], 'topic': item['topic'],
                                 'settings': merge_settings(defaults, item.get('settings', {})),
                                 'stages': {}, 'history': [], 'operation': None})
        write_json(path, state)
    return summary(project, state)


def invalidate(job, stage, evidence):
    if job['operation']:
        raise BatchError('先恢复/解决原操作，再修改阶段')
    text(evidence, '修改依据')
    for name in STAGES[STAGES.index(stage):]:
        if name in job['stages']:
            job['history'].append({'stage': name, 'record': job['stages'].pop(name), 'reason': evidence})


def approve(project, state, items, evidence, source):
    text(evidence, '用户确认原话')
    text(source, '对话位置')
    if not isinstance(items, list) or not items:
        raise BatchError('需明确确认的任务、阶段和SHA256列表')
    seen = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {'job_id', 'stage', 'sha256'}:
            raise BatchError('确认项须明确job_id/stage/sha256')
        ident, stage = item['job_id'], item['stage']
        if ident in seen:
            raise BatchError('一次确认每项任务只能包含一个当前阶段')
        seen.add(ident)
        job = lookup(state, ident)
        position = current(project, job)
        if position['action'] != 'await_confirmation' or position['stage'] != stage:
            raise BatchError('不是当前待确认版本: ' + ident)
        record = job['stages'][stage]
        if record['sha256'] != item['sha256']:
            raise BatchError('确认哈希不匹配: ' + ident)
    for item in items:
        lookup(state, item['job_id'])['stages'][item['stage']].update(
            status='confirmed', evidence=evidence, source=source)


def mutate(project, command, revision, **args):
    path = state_path(project)
    with locked(path):
        state = load(project)
        if state['revision'] != revision:
            raise BatchError('队列已改变，请重读；当前revision=' + str(state['revision']))
        if command == 'confirm':
            approve(project, state, args['items'], args['evidence'], args['source'])
        else:
            job = lookup(state, args['job_id'])
            position = current(project, job)
            if command == 'start':
                stage = args['stage']
                if position['action'] != 'create' or position['stage'] != stage:
                    raise BatchError('尚不能执行阶段: ' + stage)
                if any(j['operation'] and j['operation']['status'] in {'running', 'unknown'} for j in state['jobs']):
                    raise BatchError('已有运行中操作；串行队列须先恢复它')
                if stage in {'pilot', 'film'}:
                    voice = job['settings']['voice']
                    if voice['provider'] == 'unconfigured':
                        raise BatchError('声音方式尚未确定；先询问自定义音色或明确无旁白/沿用原音频')
                    if voice['provider'] in {'volcengine', 'dubbingx'} and (
                            not voice['voice_id'].strip() or not voice['account'].strip() or
                            (voice['provider'] == 'volcengine' and not voice['resource_id'].strip())):
                        raise BatchError('新配音须选定本平台账号和实际音色ID，火山还需resource_id')
                job['operation'] = {'token': str(uuid.uuid4()), 'stage': stage, 'status': 'running',
                                    'reference': text(args['reference'], '恢复记录位置')}
            elif command in {'finish', 'fail', 'resolve'}:
                operation = job['operation']
                if not operation or operation['token'] != args['token']:
                    raise BatchError('操作token不匹配，不能覆盖另一任务')
                if command == 'finish':
                    stage = operation['stage']
                    # Recheck earlier approved artifacts after the external call returns.
                    if position['stage'] != stage or position['action'] == 'stale_artifact':
                        raise BatchError('操作期间前置产物已变；先解决操作并回退受影响阶段')
                    artifact = artifact_path(project, job, args['artifact'])
                    version = 1 + sum(h['stage'] == stage and 'record' in h for h in job['history'])
                    root = job_root(project, job['id'])
                    snapshot = root / '.versions' / (stage + '-' + str(uuid.uuid4()) + artifact.suffix)
                    if not snapshot.resolve().is_relative_to(root):
                        raise BatchError('产物版本目录不能越界')
                    snapshot.parent.mkdir(parents=True, exist_ok=True)
                    with artifact.open('rb') as source, snapshot.open('xb') as output:
                        shutil.copyfileobj(source, output)
                    sha = file_hash(snapshot)
                    if file_hash(artifact) != sha:
                        raise BatchError('复制期间产物改变，请重新核对版本')
                    job['stages'][stage] = {'artifact': args['artifact'], 'sha256': sha,
                        'snapshot': snapshot.relative_to(root).as_posix(),
                        'version': version, 'status': 'pending', 'tool_reference': text(args['tool_reference'], '实际制作记录'),
                        'operation': copy.deepcopy(operation)}
                    job['history'].append({'operation': copy.deepcopy(operation), 'stage': stage,
                                           'reason': '完成制作，仍待用户确认'})
                    job['operation'] = None
                elif command == 'fail':
                    operation.update(status='failed' if args.get('terminal', False) else 'unknown',
                                     reason=text(args['evidence'], '失败原因'))
                else:
                    # Explicit resolution archives the operation. Never retries it itself.
                    text(args['evidence'], '原任务已核对的解决依据')
                    job['history'].append({'operation': copy.deepcopy(operation), 'stage': operation['stage'],
                                           'reason': args['evidence']})
                    job['operation'] = None
            elif command == 'invalidate':
                invalidate(job, args['stage'], args['evidence'])
            elif command == 'settings':
                changed = merge_settings(job['settings'], args['settings'])
                if changed == job['settings']:
                    raise BatchError('配置没有变化')
                if changed['notes'] != job['settings']['notes']:
                    stage = 'brief'
                elif any(changed[k] != job['settings'][k] for k in ('style_ref', 'character_ref')):
                    stage = 'storyboard'
                else:
                    stage = 'voice'
                invalidate(job, stage, args['evidence'])
                job['settings'] = changed
            else:
                raise BatchError('未知修改命令: ' + command)
        state['revision'] += 1
        write_json(path, state)
    return summary(project, state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init'); init.add_argument('--file', type=Path, required=True)
    sub.add_parser('status'); sub.add_parser('next')
    confirm = sub.add_parser('confirm')
    confirm.add_argument('--file', type=Path, required=True)
    confirm.add_argument('--evidence', required=True); confirm.add_argument('--source', required=True)
    confirm.add_argument('--expected-revision', type=int, required=True)
    for command in ('start', 'finish', 'fail', 'resolve', 'invalidate', 'settings'):
        item = sub.add_parser(command)
        item.add_argument('--job', dest='job_id', required=True)
        item.add_argument('--expected-revision', type=int, required=True)
        if command in {'start', 'invalidate'}: item.add_argument('--stage', choices=STAGES, required=True)
        if command == 'start': item.add_argument('--reference', required=True)
        if command in {'finish', 'fail', 'resolve'}: item.add_argument('--token', required=True)
        if command == 'finish':
            item.add_argument('--artifact', required=True); item.add_argument('--tool-reference', required=True)
        if command in {'fail', 'resolve', 'invalidate', 'settings'}: item.add_argument('--evidence', required=True)
        if command == 'fail': item.add_argument('--terminal', action='store_true', help='已核对原平台任务确定失败终态')
        if command == 'settings': item.add_argument('--file', type=Path, required=True)
    args = vars(parser.parse_args())
    project, command = args.pop('project'), args.pop('command')
    if command == 'init': result = initialize(project, read_json(args['file']))
    elif command == 'status': result = summary(project, load(project))
    elif command == 'next': result = next_job(project, load(project))
    else:
        revision = args.pop('expected_revision')
        if command in {'confirm', 'settings'}:
            args['items' if command == 'confirm' else 'settings'] = read_json(args.pop('file'))
        result = mutate(project, command, revision, **args)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (MediaError, OSError, ValueError, KeyError, TypeError) as exc:
        raise SystemExit('错误: ' + (str(exc) if isinstance(exc, MediaError) else type(exc).__name__))
