#!/usr/bin/env python3
"""Local, versioned Style DNA storage. Standard library only; no rendering."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import tempfile

DIMENSIONS = (
    'identity', 'palette', 'value_contrast', 'shape_language', 'linework',
    'materials', 'lighting', 'composition', 'space_camera', 'characters',
    'objects_background', 'typography', 'motion', 'transitions', 'postprocessing',
    'consistency',
)
APPEARANCE_FIELDS = ('dna', 'prompts', 'implementation', 'reuse')
KNOWN = {'observed', 'user_defined', 'inferred', 'mixed'}
STATES = KNOWN | {'unknown', 'not_applicable'}
CONFIDENCE = {'high', 'medium', 'low', 'unknown'}
TOP_FIELDS = {
    'schema_version', 'style_id', 'name', 'aliases', 'summary', 'origin',
    'evidence', 'dna', 'prompts', 'implementation', 'reuse', 'approval',
    'validation', 'revision', 'saved_at',
}


class StyleError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise StyleError(message)


def string(value, label, nonempty=False):
    require(isinstance(value, str), f'{label} 必须是字符串')
    require(not nonempty or bool(value.strip()), f'{label} 不能为空')


def strings(value, label):
    require(isinstance(value, list), f'{label} 必须是字符串数组')
    for item in value:
        string(item, label, True)


def choice(value, choices, label):
    require(isinstance(value, str) and value in choices, f'{label}: 无效值 {value!r}')


def record(value, label, required, optional=()):
    require(isinstance(value, dict), f'{label} 必须是对象')
    missing = set(required) - value.keys()
    extra = value.keys() - set(required) - set(optional)
    require(not missing, f'{label} 缺字段: {sorted(missing)}')
    require(not extra, f'{label} 未知字段: {sorted(extra)}')


def integer(value, label):
    require(type(value) is int and value > 0, f'{label} 必须是正整数，收到 {value!r}')


def artifact_key(value):
    return os.path.normcase(os.path.normpath(value.split('#', 1)[0]))


def same_appearance(first, second):
    return all(first[field] == second[field] for field in APPEARANCE_FIELDS)


def unique_records(items, label, required, optional=()):
    require(isinstance(items, list), f'{label} 必须是数组')
    indexed = {}
    for item in items:
        record(item, label, required, optional)
        string(item['id'], f'{label}.id', True)
        require(item['id'] not in indexed, f'{label} 重复id: {item["id"]}')
        indexed[item['id']] = item
    return indexed


def validate_profile(profile):
    record(profile, 'profile', TOP_FIELDS - {'revision', 'saved_at'}, {'revision', 'saved_at'})
    require(type(profile['schema_version']) is int and profile['schema_version'] == 1,
            f'不支持的schema_version: {profile["schema_version"]!r}')
    style_id = profile['style_id']
    require(isinstance(style_id, str) and bool(re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', style_id))
            and len(style_id) <= 64, f'无效style_id: {style_id!r}；使用小写字母、数字和连字符')
    string(profile['name'], 'name', True)
    strings(profile['aliases'], 'aliases')
    names = [style_id, profile['name'], *profile['aliases']]
    for name in names:
        require('@' not in name and name == name.strip(), f'名称不能含@或首尾空格: {name!r}')
    require(len({name.casefold() for name in profile['aliases']}) == len(profile['aliases']),
            'aliases存在重复名称')
    string(profile['summary'], 'summary', True)
    if 'revision' in profile:
        integer(profile['revision'], 'revision')
    if 'saved_at' in profile:
        string(profile['saved_at'], 'saved_at', True)
    origin = profile['origin']
    record(origin, 'origin', {'kind', 'references'})
    choice(origin['kind'], {'reference', 'author_defined', 'hybrid'}, 'origin.kind')
    refs = unique_records(origin['references'], 'origin.references',
                          {'id', 'kind', 'locator', 'access', 'notes'})
    for ref in refs.values():
        choice(ref['kind'], {'image', 'video', 'sequence', 'text', 'code'}, 'reference.kind')
        choice(ref['access'], {'read', 'user_statement', 'unavailable'}, 'reference.access')
        string(ref['locator'], 'reference.locator', True)
        string(ref['notes'], 'reference.notes')
    evidence = unique_records(profile['evidence'], 'evidence',
                              {'id', 'source_id', 'location', 'observation', 'method', 'confidence'})
    for item in evidence.values():
        string(item['source_id'], 'evidence.source_id', True)
        require(item['source_id'] in refs, f'证据引用不存在的素材: {item["source_id"]}')
        require(refs[item['source_id']]['access'] != 'unavailable', '不可访问素材不能作为已取得证据')
        for field in ('location', 'observation'):
            string(item[field], f'evidence.{field}', True)
        choice(item['method'], {'visual', 'measured', 'user_statement', 'code_read'}, 'evidence.method')
        choice(item['confidence'], CONFIDENCE, 'evidence.confidence')
        ref = refs[item['source_id']]
        if item['method'] in {'visual', 'measured'}:
            require(ref['kind'] in {'image', 'video', 'sequence'} and ref['access'] == 'read',
                    '视觉/测量证据需要实际读取的图像、视频或连续帧')
        if item['method'] == 'user_statement':
            require(ref['kind'] == 'text' and ref['access'] == 'user_statement', '用户定义证据需要用户原话来源')
        if item['method'] == 'code_read':
            require(ref['kind'] == 'code' and ref['access'] == 'read', '代码证据需要实际读取的代码来源')
    record(profile['dna'], 'dna', set(DIMENSIONS))
    for dimension, block in profile['dna'].items():
        record(block, f'dna.{dimension}', {'status', 'description', 'rules', 'avoid', 'evidence_ids', 'confidence'})
        choice(block['status'], STATES, f'{dimension}.status')
        choice(block['confidence'], CONFIDENCE, f'{dimension}.confidence')
        string(block['description'], f'{dimension}.description', True)
        strings(block['avoid'], f'{dimension}.avoid')
        strings(block['evidence_ids'], f'{dimension}.evidence_ids')
        require(set(block['evidence_ids']) <= evidence.keys(), f'{dimension}: 证据id不存在')
        require(isinstance(block['rules'], list), f'{dimension}.rules 必须是数组')
        if block['status'] in KNOWN:
            require(bool(block['evidence_ids']), f'{dimension}: 已知特征必须有证据或用户定义依据')
            require(bool(block['rules']), f'{dimension}: 已知特征必须有可执行规则')
        else:
            require(not block['rules'], f'{dimension}: 未知/不适用维度不能夹带确定规则')
        if block['status'] == 'user_defined':
            require(any(evidence[eid]['method'] == 'user_statement' for eid in block['evidence_ids']),
                    f'{dimension}: user_defined需要用户定义依据')
        if block['status'] == 'observed':
            require(any(evidence[eid]['method'] != 'user_statement' for eid in block['evidence_ids']),
                    f'{dimension}: observed需要实际视觉、测量或代码观察')
        for rule in block['rules']:
            record(rule, f'{dimension}.rule', {'feature', 'value', 'unit', 'tolerance', 'priority', 'evidence_ids'})
            for field in ('feature', 'value'):
                string(rule[field], f'{dimension}.rule.{field}', True)
            for field in ('unit', 'tolerance'):
                string(rule[field], f'{dimension}.rule.{field}')
            choice(rule['priority'], {'must', 'prefer', 'flexible'}, 'rule.priority')
            strings(rule['evidence_ids'], 'rule.evidence_ids')
            require(bool(rule['evidence_ids']) and set(rule['evidence_ids']) <= set(block['evidence_ids']),
                    f'{dimension}: 规则必须引用本维度的有效证据')
            methods = {evidence[eid]['method'] for eid in rule['evidence_ids']}
            if block['status'] == 'observed':
                require(bool(methods - {'user_statement'}), f'{dimension}: observed规则不能只有用户定义证据')
            if block['status'] == 'user_defined':
                require('user_statement' in methods, f'{dimension}: 每条user_defined规则都需要用户定义依据')
            if dimension in {'motion', 'transitions'}:
                temporal = any(refs[evidence[eid]['source_id']]['kind'] in {'video', 'sequence'}
                               and evidence[eid]['method'] in {'visual', 'measured'} for eid in rule['evidence_ids'])
                authored = 'user_statement' in methods
                supported = authored if block['status'] == 'user_defined' else temporal
                if block['status'] == 'mixed':
                    supported = temporal or authored
                require(supported, f'{dimension}: 每条动作/转场规则需要时间材料或对应用户定义；静帧推测应保留unknown')
    prompts = profile['prompts']
    record(prompts, 'prompts', {'style_only', 'image_template', 'motion_template', 'avoid', 'notes'})
    for field in ('style_only', 'image_template', 'motion_template'):
        string(prompts[field], f'prompts.{field}')
    for field in ('avoid', 'notes'):
        strings(prompts[field], f'prompts.{field}')
    if prompts['motion_template'].strip():
        require(profile['dna']['motion']['status'] in KNOWN,
                'motion未分析或不适用时不能输出确定的motion_template')
    implementation = profile['implementation']
    record(implementation, 'implementation', {'route', 'code_mapping', 'asset_requirements', 'limitations'})
    choice(implementation['route'], {'code', 'assets', 'hybrid', 'undecided'}, 'implementation.route')
    for field in ('code_mapping', 'asset_requirements', 'limitations'):
        strings(implementation[field], f'implementation.{field}')
    reuse = profile['reuse']
    record(reuse, 'reuse', {'locked_dimensions', 'adaptable_dimensions', 'content_exclusions', 'conflict_policy'})
    for field in ('locked_dimensions', 'adaptable_dimensions'):
        strings(reuse[field], f'reuse.{field}')
        require(set(reuse[field]) <= set(DIMENSIONS), f'reuse.{field} 含未知维度')
    require(not set(reuse['locked_dimensions']) & set(reuse['adaptable_dimensions']), '锁定与可适配维度不能重叠')
    require(all(profile['dna'][dim]['status'] in KNOWN for dim in reuse['locked_dimensions']), '不能锁定未知维度')
    strings(reuse['content_exclusions'], 'reuse.content_exclusions')
    string(reuse['conflict_policy'], 'reuse.conflict_policy', True)
    approval = profile['approval']
    record(approval, 'approval', {'state', 'statement', 'reference'})
    choice(approval['state'], {'draft', 'confirmed'}, 'approval.state')
    string(approval['statement'], 'approval.statement')
    string(approval['reference'], 'approval.reference')
    if approval['state'] == 'confirmed':
        string(approval['statement'], 'approval.statement', True)
        string(approval['reference'], 'approval.reference', True)
        for dimension in ('identity', 'palette', 'shape_language', 'composition'):
            require(profile['dna'][dimension]['status'] in KNOWN, f'确认风格前需明确核心维度: {dimension}')
        string(prompts['style_only'], 'prompts.style_only', True)
        string(prompts['image_template'], 'prompts.image_template', True)
    else:
        require(not approval['statement'] and not approval['reference'],
                'draft的当前确认字段必须清空；历史批准保留在旧版，保存草稿的原话可记入origin')
    validation = profile['validation']
    record(validation, 'validation', {'state', 'samples', 'checks', 'known_gaps'})
    choice(validation['state'], {'untested', 'static_tested', 'motion_tested'}, 'validation.state')
    strings(validation['samples'], 'validation.samples')
    sample_keys = {artifact_key(sample) for sample in validation['samples']}
    require(len(sample_keys) == len(validation['samples']), 'validation.samples存在重复路径')
    strings(validation['known_gaps'], 'validation.known_gaps')
    require(isinstance(validation['checks'], list), 'validation.checks必须是数组')
    for check in validation['checks']:
        record(check, 'validation.check', {'dimension', 'result', 'evidence', 'notes'})
        choice(check['dimension'], DIMENSIONS, 'check.dimension')
        choice(check['result'], {'pass', 'fail', 'not_tested'}, 'check.result')
        if check['result'] == 'pass':
            require(profile['dna'][check['dimension']]['status'] in KNOWN, '不能把未知/不适用维度标成验证通过')
        string(check['evidence'], 'check.evidence', check['result'] != 'not_tested')
        if check['result'] != 'not_tested':
            require(artifact_key(check['evidence']) in sample_keys,
                    'check.evidence必须定位当前validation.samples中的样稿，可用#标帧号/区域')
        string(check['notes'], 'check.notes')
    if validation['state'] != 'untested':
        require(bool(validation['samples']) and any(c['result'] == 'pass' for c in validation['checks']),
                '已测试状态必须有样片位置和通过的检查记录')
        if validation['state'] == 'static_tested':
            require(any(c['dimension'] not in {'motion', 'transitions'} and c['result'] == 'pass'
                        for c in validation['checks']), 'static_tested需要通过的静态检查')
    if validation['state'] == 'motion_tested':
        require(any(c['dimension'] == 'motion' and c['result'] == 'pass' for c in validation['checks']),
                'motion_tested需要通过的动态检查')
    return profile


def no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f'JSON重复键: {key}')
        result[key] = value
    return result


def reject_constant(value):
    raise StyleError(f'JSON不能含{value}')


def read_json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'), object_pairs_hook=no_duplicates,
                          parse_constant=reject_constant)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StyleError(f'无法读取JSON {path}: {exc}') from exc


def default_root():
    codex_root = Path(os.environ['CODEX_HOME']).expanduser() if os.environ.get('CODEX_HOME') else Path.home() / '.codex'
    return (codex_root / 'config' / 'huashu-art-motion').resolve()


def initialize(root):
    root.mkdir(parents=True, exist_ok=True)
    marker = root / 'library.json'
    if marker.exists():
        require(read_json(marker) == {'schema_version': 1}, f'不支持的风格库配置: {marker}')
    else:
        try:
            with marker.open('x', encoding='utf-8') as file:
                file.write('{"schema_version": 1}\n')
        except FileExistsError:
            require(read_json(marker) == {'schema_version': 1}, f'不支持的风格库配置: {marker}')
    (root / 'styles').mkdir(exist_ok=True)
    require(not (root / 'styles').is_symlink(), f'styles目录不能是符号链接: {root / "styles"}')


@contextmanager
def write_lock(root):
    initialize(root)
    lock = root / '.write.lock'
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise StyleError(f'风格库正在写入或上次中断留下锁: {lock}；请核实后处理，不自动覆盖') from exc
    try:
        os.write(descriptor, str(os.getpid()).encode('ascii'))
        yield
    finally:
        os.close(descriptor)
        lock.unlink()


def versions(root, style_id):
    directory = root / 'styles' / style_id
    if not directory.exists():
        return []
    require(directory.is_dir() and not directory.is_symlink(), f'无效风格目录: {directory}')
    found = []
    for path in directory.iterdir():
        match = re.fullmatch(r'v([1-9][0-9]*)\.json', path.name)
        require(bool(match) and path.is_file() and not path.is_symlink(), f'风格目录含无效文件: {path}')
        found.append((int(match[1]), path))
    return sorted(found)


def load_version(style_id, revision, path):
    profile = validate_profile(read_json(path))
    require(profile['style_id'] == style_id and profile.get('revision') == revision,
            f'档案ID/版本与路径不一致: {path}')
    return profile


def catalog(root):
    if not root.exists():
        return {}
    require((root / 'library.json').exists(), f'风格库缺少library.json: {root}')
    require(read_json(root / 'library.json') == {'schema_version': 1}, '不支持的风格库版本')
    require((root / 'styles').is_dir(), f'风格库缺少styles目录: {root}')
    require(not (root / 'styles').is_symlink(), f'styles目录不能是符号链接: {root / "styles"}')
    result = {}
    for directory in sorted((root / 'styles').iterdir()):
        require(bool(re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', directory.name)) and len(directory.name) <= 64,
                f'无效风格目录名称: {directory.name}')
        found = versions(root, directory.name)
        require(bool(found), f'风格目录没有版本文件: {directory}')
        revision, path = found[-1]
        result[directory.name] = load_version(directory.name, revision, path)
    claimed = {}
    for style_id, profile in result.items():
        for name in {style_id, profile['name'], *profile['aliases']}:
            folded = name.casefold()
            require(folded not in claimed or claimed[folded] == style_id,
                    f'风格名称或别名冲突: {name!r}')
            claimed[folded] = style_id
    return result


def resolve(root, selector):
    name, separator, suffix = selector.rpartition('@')
    if not separator:
        name, revision = selector, None
    else:
        require(suffix.isascii() and suffix.isdigit() and int(suffix) > 0, f'无效版本调用: {selector!r}')
        revision = int(suffix)
    require(bool(name.strip()), '风格名称不能为空')
    matches = [profile for profile in catalog(root).values()
               if name.casefold() in {value.casefold() for value in [profile['style_id'], profile['name'], *profile['aliases']]}]
    require(len(matches) == 1, f'找不到唯一风格: {selector!r}；先用list查看，不自动替换')
    profile = matches[0]
    if revision is None:
        return profile
    available = dict(versions(root, profile['style_id']))
    require(revision in available, f'风格 {profile["style_id"]} 没有版本 {revision}')
    return load_version(profile['style_id'], revision, available[revision])


def save_profile(root, profile, base_version=None):
    validate_profile(profile)
    with write_lock(root):
        existing = catalog(root)
        current = existing.get(profile['style_id'])
        if current:
            require(type(base_version) is int and base_version == current['revision'],
                    f'更新 {profile["style_id"]} 必须指定当前 --base-version {current["revision"]}；收到 {base_version!r}')
            history = [load_version(profile['style_id'], old_revision, old_path)
                       for old_revision, old_path in versions(root, profile['style_id'])]
            appearance_changed = not same_appearance(profile, current)
            newly_confirmed = profile['approval']['state'] == 'confirmed' and current['approval']['state'] == 'draft'
            if profile['approval']['state'] == 'confirmed' and (appearance_changed or newly_confirmed):
                previous_refs = {old['approval']['reference'] for old in history}
                require(profile['approval']['reference'] not in previous_refs,
                        '改变规则或draft转confirmed需要本次确认/委托位置，不能复用历史批准；也可存draft')
            if appearance_changed:
                validation = profile['validation']
                if validation['state'] == 'untested':
                    require(not validation['samples'] and all(c['result'] == 'not_tested' for c in validation['checks']),
                            '改变规则后请清除当前版本的旧样稿/旧检查，历史证据仍保留在旧版本')
                else:
                    old_samples = {artifact_key(sample) for sample in current['validation']['samples']}
                    new_samples = {artifact_key(sample) for sample in validation['samples']}
                    require(not old_samples & new_samples,
                            '改变规则后当前测试样稿不能复用旧路径；提供当前版本独立样稿或标untested')
                    require(validation['checks'] != current['validation']['checks'],
                            '改变规则后不能原样沿用旧检查记录')
                    changed_dimensions = {dim for dim in DIMENSIONS if profile['dna'][dim] != current['dna'][dim]
                                          and profile['dna'][dim]['status'] in KNOWN}
                    if validation['state'] == 'static_tested':
                        changed_dimensions -= {'motion', 'transitions'}
                    checked = {c['dimension'] for c in validation['checks'] if c['result'] != 'not_tested'}
                    require(changed_dimensions <= checked,
                            f'已改变维度缺少当前样稿复验: {sorted(changed_dimensions - checked)}；可标untested')
            # Evidence belongs to the visual definition it tested, even across draft revisions.
            validation = profile['validation']
            tested_history = [old for old in history if old['validation']['state'] != 'untested']
            if validation['state'] != 'untested' and tested_history:
                incompatible_samples = {artifact_key(sample) for old in tested_history
                                        if not same_appearance(profile, old)
                                        for sample in old['validation']['samples']}
                current_samples = {artifact_key(sample) for sample in validation['samples']}
                require(not incompatible_samples & current_samples,
                        '当前测试引用了不同视觉定义历史版本的样稿；草稿过渡不能恢复旧pass')
                last_tested = tested_history[-1]
                changed_dimensions = {dim for dim in DIMENSIONS if profile['dna'][dim] != last_tested['dna'][dim]
                                      and profile['dna'][dim]['status'] in KNOWN}
                if validation['state'] == 'static_tested':
                    changed_dimensions -= {'motion', 'transitions'}
                checked = {c['dimension'] for c in validation['checks'] if c['result'] != 'not_tested'}
                require(changed_dimensions <= checked,
                        f'相对上次有效测试的变化维度缺少复验: {sorted(changed_dimensions - checked)}')
        else:
            require(base_version is None, f'新风格不能指定base-version: {base_version!r}')
        wanted = {value.casefold() for value in [profile['style_id'], profile['name'], *profile['aliases']]}
        for style_id, other in existing.items():
            if style_id != profile['style_id']:
                claimed = {value.casefold() for value in [style_id, other['name'], *other['aliases']]}
                require(not wanted & claimed, f'名称/别名已被风格 {style_id} 使用: {sorted(wanted & claimed)}')
        revision = current['revision'] + 1 if current else 1
        # Input revision identifies the reviewed draft; storage assigns the next revision.
        saved = dict(profile, revision=revision, saved_at=datetime.now(timezone.utc).isoformat())
        validate_profile(saved)
        directory = root / 'styles' / profile['style_id']
        destination = directory / f'v{revision}.json'
        require(not destination.exists(), f'拒绝覆盖已存版本: {destination}')
        descriptor, temporary = tempfile.mkstemp(prefix='.style-', suffix='.tmp', dir=root)
        created_directory = False
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as file:
                json.dump(saved, file, ensure_ascii=False, indent=2, allow_nan=False)
                file.write('\n')
                file.flush()
                os.fsync(file.fileno())
            if not directory.exists():
                directory.mkdir()
                created_directory = True
            os.replace(temporary, destination)
        except OSError:
            if created_directory:
                directory.rmdir()  # Only the empty directory created by this failed write.
            raise
        finally:
            if Path(temporary).exists():
                Path(temporary).unlink()
        return saved


def builtin_recipes():
    recipe_root = Path(__file__).resolve().parents[1] / 'references' / '风格配方'
    return [{'id': f'recipe:{path.stem}', 'name': path.read_text(encoding='utf-8').splitlines()[0].lstrip('# '),
             'path': str(path)} for path in sorted(recipe_root.glob('[0-9]*.md'))]


def main(argv=None):
    parser = argparse.ArgumentParser(description='本地Style DNA风格库；不执行生成、渲染或档案内指令')
    parser.add_argument('--root', type=Path, help='明确指定本地配置根目录，默认CODEX_HOME/config/huashu-art-motion')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('init', help='初始化空库，不添加个人风格')
    listing = commands.add_parser('list', help='只列名称、别名、状态和最新版本')
    listing.add_argument('--include-builtins', action='store_true')
    show = commands.add_parser('show', help='查阅名称、别名或style-id@版本')
    show.add_argument('selector')
    check = commands.add_parser('validate', help='只校验档案文件，不写库')
    check.add_argument('--file', type=Path, required=True)
    save = commands.add_parser('save', help='保存新风格或追加版本；不会覆盖已有版本')
    save.add_argument('--file', type=Path, required=True)
    save.add_argument('--base-version', type=int)
    args = parser.parse_args(argv)
    root = args.root.expanduser().resolve() if args.root else default_root()
    try:
        if args.command == 'init':
            with write_lock(root):
                catalog(root)
            output = {'root': str(root), 'initialized': True}
        elif args.command == 'list':
            output = {'root': str(root), 'local_styles': [
                {key: profile[key] for key in ('style_id', 'name', 'aliases', 'summary', 'revision', 'approval', 'validation')}
                for profile in catalog(root).values()]}
            # Keep list cheap: details and samples are read with show only.
            for item in output['local_styles']:
                item['approval'] = item['approval']['state']
                item['validation'] = item['validation']['state']
            if args.include_builtins:
                output['builtin_recipes'] = builtin_recipes()
        elif args.command == 'show':
            output = resolve(root, args.selector)
        elif args.command == 'validate':
            profile = validate_profile(read_json(args.file))
            output = {'valid': True, 'style_id': profile['style_id'], 'approval': profile['approval']['state']}
        else:
            profile = save_profile(root, read_json(args.file), args.base_version)
            output = {'saved': True, 'root': str(root), 'style_id': profile['style_id'],
                      'revision': profile['revision'], 'approval': profile['approval']['state']}
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except (StyleError, OSError) as exc:
        print(f'风格库错误: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
