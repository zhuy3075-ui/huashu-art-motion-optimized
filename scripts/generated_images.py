# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow"]
# ///
"""真实宿主生图计划与产物验收；不调用生图服务，不替代视觉评审。"""
import argparse
import copy
import hashlib
import io
import json
from pathlib import Path
import time
import uuid
import media_preferences as prefs

MAX_PIXELS = 40_000_000


def validate_dimensions(data):
    for field in ('min_width', 'min_height'):
        if type(data.get(field)) is not int or not 1 <= data[field] <= 16384:
            raise prefs.MediaError(field + '必须为1–16384的整数')
    if data['min_width'] * data['min_height'] > MAX_PIXELS:
        raise prefs.MediaError('要求的最小图片面积超过4000万像素验收上限，不能创建无法验收的任务')


def inspect(path):
    from PIL import Image, ImageOps
    raw = Path(path).read_bytes()
    try:
        with Image.open(io.BytesIO(raw)) as source:
            if source.format not in {'PNG', 'JPEG', 'WEBP'} or getattr(source, 'n_frames', 1) != 1:
                raise prefs.MediaError('生成素材只接受静态PNG/JPEG/WebP')
            fmt, size = source.format.lower(), source.size
            if size[0]*size[1] > MAX_PIXELS:
                raise prefs.MediaError('图片超过4000万像素')
            source.verify()
        with Image.open(io.BytesIO(raw)) as source:
            image = ImageOps.exif_transpose(source).convert('RGBA')
            alpha = image.getchannel('A').getextrema()
            return {'format': fmt, 'width': image.width, 'height': image.height,
                    'source_size': list(size), 'has_transparency': alpha[0] < 255,
                    'has_visible_pixels': alpha[1] > 0, 'sha256': hashlib.sha256(raw).hexdigest()}
    except prefs.MediaError:
        raise
    except Exception as exc:
        raise prefs.MediaError('图片无法完整解码: ' + type(exc).__name__) from None


def tools_snapshot(path, session):
    data = prefs.read_json(path)
    if not isinstance(data, dict) or data.get('session_id') != session or not session:
        raise prefs.MediaError('工具快照不属于本次会话')
    observed = data.get('observed_at')
    if type(observed) not in (int, float) or not 0 <= time.time()-observed <= 900:
        raise prefs.MediaError('实际工具快照须在15分钟内，不能改旧快照假装已刷新')
    entries = data.get('tools')
    if not isinstance(entries, list):
        raise prefs.MediaError('快照须有tools列表；没有工具时填空数组')
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry)-{'id', 'operations', 'reference_images', 'transparent_background', 'network', 'cost'}:
            raise prefs.MediaError('工具声明包含不支持字段')
        identifier = entry.get('id')
        if not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 128 or identifier in seen:
            raise prefs.MediaError('工具ID为空、过长或重复')
        seen.add(identifier)
        if entry.get('network') not in {'cloud', 'local'} or entry.get('cost') not in {'subscription', 'paid_api', 'unknown', 'none'}:
            raise prefs.MediaError('工具须声明实际网络与计费范围')
        operations = entry.get('operations')
        if not isinstance(operations, list) or not operations or any(v not in {'generate', 'edit'} for v in operations):
            raise prefs.MediaError('工具operations须为generate/edit数组')
        if any(type(entry.get(k)) is not bool for k in ('reference_images', 'transparent_background')):
            raise prefs.MediaError('工具须声明参考图和透明背景支持情况')
    return data


def request(path):
    data = prefs.read_json(path)
    allowed = {'prompt', 'operation', 'references', 'transparent_background', 'min_width', 'min_height'}
    if not isinstance(data, dict) or set(data)-allowed:
        raise prefs.MediaError('图片请求含不支持字段')
    if not isinstance(data.get('prompt'), str) or not data['prompt'].strip():
        raise prefs.MediaError('prompt不能为空，内容必须是已确定的实际图片要求')
    data.setdefault('operation', 'generate'); data.setdefault('references', [])
    data.setdefault('transparent_background', False); data.setdefault('min_width', 1); data.setdefault('min_height', 1)
    if data['operation'] not in {'generate', 'edit'} or type(data['transparent_background']) is not bool:
        raise prefs.MediaError('图片operation/transparent_background无效')
    validate_dimensions(data)
    if not isinstance(data['references'], list) or any(not isinstance(v, str) or not v.strip() for v in data['references']):
        raise prefs.MediaError('references须为本地图片路径数组')
    if data['operation'] == 'edit' and not data['references']:
        raise prefs.MediaError('改图必须提供实际参考图片')
    references = []
    for value in data['references']:
        file = Path(value).expanduser()
        if not file.is_absolute(): file = Path(path).resolve().parent/file
        meta = inspect(file)
        if not meta['has_visible_pixels']:
            raise prefs.MediaError('参考图不能全透明: ' + str(file))
        references.append({'path': str(file.resolve()), 'sha256': meta['sha256']})
    data['references'] = references
    return data


def choose(configuration, snapshot, req=None):
    choice = configuration['preferences']['image']
    mode = choice['mode']
    if mode in {'off', 'existing'}:
        return {'status': 'skipped' if mode == 'off' else 'existing', 'note': '不调用生图；原图按15号导入'}
    if mode == 'unconfigured':
        return {'status': 'needs_selection', 'note': '先沿用本次明确选择，未定再给现有图/生成/跳过选项'}
    if 'image_generation' in configuration['denied']:
        return {'status': 'blocked', 'denied': ['image_generation']}
    if snapshot is None:
        return {'status': 'needs_tool_snapshot', 'note': '仅认当前会话实际暴露的工具，不扫描Key猜能力'}
    eligible = []
    blocked = []
    for tool in snapshot['tools']:
        if choice['tool_id'] not in {'auto', tool['id']}:
            continue
        if req and (req['operation'] not in tool['operations'] or
                    (req['references'] and not tool['reference_images']) or
                    (req['transparent_background'] and not tool['transparent_background'])):
            continue
        needs = ['image_generation']
        if tool['network'] == 'cloud': needs.append('cloud')
        if req and req['references'] and tool['network'] == 'cloud': needs.append('reference_upload')
        if tool['cost'] == 'paid_api': needs.append('paid_api')
        if tool['cost'] == 'unknown': needs.append('unknown_cost')
        denied = sorted(set(needs) & set(configuration['denied']))
        if denied:
            blocked.append({'tool_id': tool['id'], 'denied': denied})
        else:
            eligible.append((tool, needs))
    if len(eligible) > 1:
        return {'status': 'needs_selection', 'candidates': [tool['id'] for tool, _ in eligible]}
    if not eligible:
        return {'status': 'blocked' if blocked else 'unavailable', 'details': blocked,
                'note': '无符合当前要求的实际工具，不切换供应商绕过选择'}
    tool, needs = eligible[0]
    return {'status': 'host_action_required', 'tool': copy.deepcopy(tool), 'required_authorizations': needs}


def image_fingerprint(configuration):
    return prefs.digest({'image': configuration['preferences']['image'], 'denied': configuration['denied']})


def authorize(decision, approval, grants):
    if decision['status'] != 'host_action_required':
        raise prefs.MediaError('当前不能生图: ' + decision['status'])
    if not isinstance(approval, str) or not approval.strip():
        raise prefs.MediaError('需要已存在的真实生成范围/工具授权依据')
    missing = set(decision['required_authorizations'])-{'image_generation'}-set(grants)
    if missing:
        raise prefs.MediaError('缺少已取得的本次授权记录: ' + ', '.join(sorted(missing)))


def make_plan(configuration, req, snapshot, session, out, approval, grants=()):
    validate_dimensions(req)
    decision = choose(configuration, snapshot, req)
    authorize(decision, approval, grants)
    plan = {'schema_version': 1, 'kind': 'generated_image_plan', 'plan_id': str(uuid.uuid4()),
            'created_at': time.time(), 'session_id': session, 'tool': decision['tool'],
            'request': req, 'request_sha256': prefs.digest(req), 'preferences_sha256': image_fingerprint(configuration),
            'approval': approval, 'note': 'Agent按实际schema调用工具；此计划不是执行器或平台认证'}
    prefs.write_json(out, plan, exclusive=True)
    return plan


def accept(configuration, plan_path, receipt_path, file, out, snapshot, session, approval, grants=()):
    plan, receipt = prefs.read_json(plan_path), prefs.read_json(receipt_path)
    if not isinstance(plan, dict) or plan.get('schema_version') != 1 or plan.get('kind') != 'generated_image_plan':
        raise prefs.MediaError('不是受支持的生图计划')
    created = plan.get('created_at')
    if type(created) not in (int, float) or not 0 <= time.time()-created <= 3600 or plan.get('session_id') != session:
        raise prefs.MediaError('计划过期或会话不匹配；重新建计划，不改旧计划')
    req = plan.get('request')
    if not isinstance(req, dict) or plan.get('request_sha256') != prefs.digest(req):
        raise prefs.MediaError('计划请求已变化')
    validate_dimensions(req)
    if plan.get('preferences_sha256') != image_fingerprint(configuration):
        raise prefs.MediaError('图片偏好/禁止项已变化，需要重新建计划')
    for reference in req.get('references', []):
        if inspect(reference['path'])['sha256'] != reference['sha256']:
            raise prefs.MediaError('参考图已变化，需要重新建计划')
    decision = choose(configuration, snapshot, req)
    authorize(decision, approval, grants)
    if plan.get('tool') != decision['tool']:
        raise prefs.MediaError('实际工具声明或选择已变化')
    meta = inspect(file)
    expected = {'status': 'success', 'plan_id': plan.get('plan_id'), 'session_id': session,
                'tool_id': decision['tool']['id'], 'output_sha256': meta['sha256']}
    if not isinstance(receipt, dict) or any(receipt.get(k) != v for k, v in expected.items()):
        raise prefs.MediaError('回执与计划/实际产物不匹配')
    if not isinstance(receipt.get('tool_call_reference'), str) or not receipt['tool_call_reference'].strip():
        raise prefs.MediaError('回执必须引用实际工具调用记录；不能从PNG元数据推断来源')
    if not meta['has_visible_pixels']:
        raise prefs.MediaError('生图结果全透明')
    if meta['width'] < req['min_width'] or meta['height'] < req['min_height']:
        raise prefs.MediaError('实际图片尺寸小于计划要求')
    if req['transparent_background'] and not meta['has_transparency']:
        raise prefs.MediaError('计划要求透明背景，结果没有实际透明像素')
    out = Path(out)
    if {'.png': 'png', '.jpg': 'jpeg', '.jpeg': 'jpeg', '.webp': 'webp'}.get(out.suffix.lower()) != meta['format']:
        raise prefs.MediaError('输出扩展名必须与实际图片格式一致，本工具不转码')
    sidecar = out.with_name(out.name+'.json')
    raw = Path(file).read_bytes()
    if hashlib.sha256(raw).hexdigest() != meta['sha256']:
        raise prefs.MediaError('验收期间图片文件被修改')
    meta.update(plan_id=plan['plan_id'], tool_id=decision['tool']['id'], source_path=str(Path(file).resolve()),
                tool_call_reference=receipt['tool_call_reference'], provenance='agent_reported_tool_call',
                review_required=True, approval=approval, receipt_sha256=prefs.digest(receipt),
                note='技术验收通过；来源非平台签名认证，身份/画风/内容仍需看图与用户评审')
    with prefs.locked(out):
        if out.exists() or sidecar.exists():
            raise prefs.MediaError('输出图片或旁车已存在，不覆盖现有版本')
        with out.open('xb') as output: output.write(raw)
        prefs.write_json(sidecar, meta, exclusive=True)
    return {'status': 'accepted_technical', 'output': str(out.resolve()), 'metadata': str(sidecar.resolve()),
            'review_required': True, 'sha256': meta['sha256']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=prefs.ROOT)
    parser.add_argument('--project', type=Path)
    parser.add_argument('--task', type=Path)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('plan', 'accept'):
        item = sub.add_parser(name)
        item.add_argument('--tools', type=Path, required=True); item.add_argument('--session', required=True)
        item.add_argument('--out', type=Path, required=True); item.add_argument('--approval', required=True)
        for grant in ('cloud', 'reference-upload', 'paid-api', 'unknown-cost'):
            item.add_argument('--allow-'+grant, action='store_true', help='只表达已经取得的本次授权，不覆盖禁止项')
        if name == 'plan': item.add_argument('--request', type=Path, required=True)
        else:
            for field in ('plan', 'receipt', 'file'): item.add_argument('--'+field, type=Path, required=True)
    args = parser.parse_args()
    configuration = prefs.effective(args.root, args.project, prefs.read_json(args.task) if args.task else None)['configuration']
    snapshot = tools_snapshot(args.tools, args.session)
    grants = [key for key in ('cloud', 'reference_upload', 'paid_api', 'unknown_cost') if getattr(args, 'allow_'+key)]
    if args.command == 'plan':
        result = make_plan(configuration, request(args.request), snapshot, args.session, args.out, args.approval, grants)
    else:
        result = accept(configuration, args.plan, args.receipt, args.file, args.out, snapshot, args.session, args.approval, grants)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (prefs.MediaError, OSError, ValueError, KeyError, TypeError, ImportError) as exc:
        raise SystemExit('错误: ' + (str(exc) if isinstance(exc, prefs.MediaError) else type(exc).__name__))
