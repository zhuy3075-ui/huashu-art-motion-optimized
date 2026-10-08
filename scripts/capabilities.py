#!/usr/bin/env python3
"""只读按需能力检查；不联网、不解密凭据、不调用媒体服务。"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import media_preferences as prefs


def voice_status(configuration, root=prefs.ROOT, audio=None, audio_format='wav'):
    voice = configuration['preferences']['voice']
    provider = 'provided' if audio else voice['provider']
    result = {'capability': 'voice', 'provider': provider, 'cloud_verified': False, 'reasons': []}
    if provider in {'off', 'unconfigured'}:
        result['status'] = 'skipped' if provider == 'off' else 'needs_selection'
        return result
    if provider == 'provided':
        result['status'] = 'provided' if audio and Path(audio).is_file() else 'needs_audio'
        result['note'] = '沿用已有音轨；本检查不证明解码或听感已通过'
        return result
    if set(configuration['denied']) & {'cloud', 'paid_api'}:
        result.update(status='blocked', denied=sorted(set(configuration['denied']) & {'cloud', 'paid_api'}))
        return result
    account = voice['account']
    if not account:
        if provider == 'volcengine': account = 'default'
        else:
            active = Path(root)/'dubbingx/active-account.json'
            active_data = prefs.read_json(active, missing=True)
            if not isinstance(active_data, dict): raise prefs.MediaError('活跃账号记录须为对象')
            account = active_data.get('account', 'main')
    if not isinstance(account, str) or not re.fullmatch('[A-Za-z0-9_-]{1,128}', account):
        raise prefs.MediaError('账号别名只能是1–128位字母数字下划线连字符')
    directory = Path(root)/provider/account
    variable = 'VOLC_TTS_API_KEY' if provider == 'volcengine' else 'DUBBINGX_API_KEY'
    result['account'] = account
    result['credentials_present'] = bool(os.environ.get(variable)) or (directory/'credentials.dpapi.json').is_file()
    if not result['credentials_present']: result['reasons'].append('缺少所选平台环境变量或本机加密凭据')
    if not voice['voice_id']: result['reasons'].append('尚未选择本平台具体音色ID，不能默认用官方声音')
    if provider == 'volcengine' and voice['resource_id'] not in {'seed-tts-2.0', 'seed-icl-2.0'}:
        result['reasons'].append('火山须明确官方/复刻resource_id')
    required = ('ffmpeg', 'ffprobe') if provider == 'volcengine' else (('ffprobe',) if audio_format == 'mp3' else ())
    missing = [command for command in required if not shutil.which(command)]
    if missing: result['reasons'].append('缺少本地音频工具: ' + ', '.join(missing))
    result['status'] = 'needs_configuration' if result['reasons'] else 'configured_unverified'
    result['note'] = '只检查指定配置位置与依赖；未核验Key/音色权限、音质或生成授权。DubbingX WAV使用原生校验，ffprobe只为MP3。'
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=prefs.ROOT)
    parser.add_argument('--project', type=Path); parser.add_argument('--task', type=Path)
    sub = parser.add_subparsers(dest='command', required=True)
    status = sub.add_parser('status')
    status.add_argument('--capability', choices=['voice', 'image'], required=True)
    status.add_argument('--audio-format', choices=['wav', 'mp3'], default='wav', help='DubbingX原生WAV不需ffprobe；火山适配器固定MP3')
    status.add_argument('--audio', type=Path); status.add_argument('--tools', type=Path); status.add_argument('--session')
    args = parser.parse_args()
    resolved = prefs.effective(args.root, args.project, prefs.read_json(args.task) if args.task else None)
    if args.capability == 'voice':
        result = voice_status(resolved['configuration'], args.root, args.audio, args.audio_format)
    else:
        import generated_images as images
        snapshot = images.tools_snapshot(args.tools, args.session) if args.tools else None
        result = dict(capability='image', **images.choose(resolved['configuration'], snapshot))
    result['sources'] = {k: v for k, v in resolved['sources'].items() if k.startswith(args.capability+'.')}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (prefs.MediaError, OSError, ValueError, TypeError, KeyError) as exc:
        raise SystemExit('错误: ' + (str(exc) if isinstance(exc, prefs.MediaError) else type(exc).__name__))
