#!/usr/bin/env python3
# /// script
# dependencies = ["pillow", "numpy"]
# ///
"""Private versioned character identity, original assets and reviewed action metadata."""
import argparse
import copy
from contextlib import contextmanager
from datetime import datetime,timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
from image_io import AssetError, inspect_image, read_json, sha256, write_json

ROLES={'master','reference','view','expression','sprite_sheet','frame'}
EXTENSIONS={'PNG':'.png','JPEG':'.jpg','WEBP':'.webp','GIF':'.gif','BMP':'.bmp','TIFF':'.tif'}


def require(condition,message):
    if not condition:
        raise AssetError(message)


def text(value,label):
    require(isinstance(value,str) and bool(value.strip()),f'{label}须为非空文本')


def identifier(value,label):
    require(isinstance(value,str) and len(value)<=64 and bool(re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*',value)),f'{label}须为小写字母/数字/连字符')
    require(value.upper() not in {'CON','PRN','AUX','NUL',*(f'COM{i}' for i in range(1,10)),*(f'LPT{i}' for i in range(1,10))},f'{label}不能使用Windows保留名称: {value}')


def fields(value,required,optional=()):
    require(isinstance(value,dict) and set(required)<=value.keys() and not value.keys()-set(required)-set(optional),'档案对象存在缺失或未知字段')


def finite(value,label,minimum=0):
    require(type(value) in {int,float} and math.isfinite(value) and value>=minimum,f'{label}须为有限数值且>= {minimum}')


def validate_profile(profile,base):
    fields(profile,{'schema_version','character_id','name','aliases','summary','identity','assets','actions','approval','sample_review'}, {'revision','saved_at'})
    require(type(profile['schema_version']) is int and profile['schema_version']==1,'schema_version须为1')
    identifier(profile['character_id'],'character_id'); text(profile['name'],'name'); text(profile['summary'],'summary')
    require(isinstance(profile['aliases'],list),'aliases须为数组')
    for name in [profile['name'],*profile['aliases']]:
        text(name,'名称'); require('@' not in name and name==name.strip(),'名称不能含@或首尾空白')
    require(len({a.casefold() for a in profile['aliases']})==len(profile['aliases']),'重复别名')
    fields(profile['identity'],{'preserve','allowed_changes','forbidden_changes','unknown'})
    for values in profile['identity'].values():
        require(isinstance(values,list),'身份约束须为文本数组')
        for value in values: text(value,'身份约束')
    fields(profile['approval'],{'status','reference'})
    require(profile['approval']['status'] in {'draft','confirmed'},'确认状态无效')
    require(isinstance(profile['approval']['reference'],str),'确认依据须为文本')
    if profile['approval']['status']=='confirmed':
        text(profile['approval']['reference'],'真实确认依据')
        require(bool(profile['identity']['preserve']),'确认档案必须明确身份保留项')
    else:
        require(not profile['approval']['reference'],'草稿不能携带旧确认依据')
    fields(profile['sample_review'],{'status','reference','scope'})
    require(profile['sample_review']['scope'] in {'static','all_actions'},'样稿范围须为static或all_actions')
    require(profile['sample_review']['status'] in {'not_reviewed','accepted','changes_requested'},'样稿反馈状态无效')
    require(isinstance(profile['sample_review']['reference'],str),'样稿依据须为文本')
    if profile['sample_review']['status']=='not_reviewed':
        require(not profile['sample_review']['reference'],'未审样稿不能携带旧反馈')
    else:
        text(profile['sample_review']['reference'],'样稿及用户反馈依据')
        require(profile['approval']['status']=='confirmed','样稿反馈须绑定已确认角色档案')
    assets={}
    require(isinstance(profile['assets'],list) and bool(profile['assets']),'须有实际角色素材')
    for asset in profile['assets']:
        fields(asset,{'id','role','file'}, {'notes','sha256','image'})
        identifier(asset['id'],'asset.id'); text(asset['file'],'asset.file')
        require(asset['id'] not in assets and asset['role'] in ROLES,'重复素材ID或未知角色')
        path=(Path(base)/asset['file']).resolve()
        info=inspect_image(path)
        require(info['visible_bbox'] is not None,'素材完全透明')
        require(info['format'] in EXTENSIONS,'不支持此图像格式')
        require(info['frames']==1,'角色档案需静态素材；动图需先明确提取帧')
        digest=sha256(path)
        if 'sha256' in asset: require(asset['sha256']==digest,'素材哈希不符，不能复用已变化文件')
        if 'notes' in asset: require(isinstance(asset['notes'],str),'notes须为文本')
        assets[asset['id']]={'source':path,'sha256':digest,'image':info}
    require(any(a['role']=='master' for a in profile['assets']),'须指定一张母版master，其他图不自动决定角色身份')
    require(isinstance(profile['actions'],list),'actions须为数组')
    if profile['sample_review']['status']=='accepted' and profile['sample_review']['scope']=='all_actions':
        require(bool(profile['actions']),'动作认可范围须有实际动作')
    actions={}
    for action in profile['actions']:
        fields(action,{'name','frame_ids','metadata_file','fps','loop'}, {'metadata_sha256'})
        identifier(action['name'],'动作名称')
        require(action['name'] not in actions,'动作名重复')
        finite(action['fps'],'fps',.01); require(action['fps']<=120 and type(action['loop']) is bool,'动作fps/loop无效')
        ids=action['frame_ids']
        require(isinstance(ids,list) and bool(ids) and len(set(ids))==len(ids),'动作须有不重复帧ID')
        frame_assets={a['id']:a for a in profile['assets'] if a['role']=='frame'}
        require(all(i in frame_assets for i in ids),'动作引用缺失或不是frame的素材')
        text(action['metadata_file'],'动作元数据')
        path=(Path(base)/action['metadata_file']).resolve()
        if 'metadata_sha256' in action: require(sha256(path)==action['metadata_sha256'],'动作元数据发生变化')
        meta=read_json(path)
        require(isinstance(meta,dict) and isinstance(meta.get('frames'),list) and len(meta['frames'])==len(ids),'动作meta帧数不匹配')
        finite(meta.get('ref_h'),'站立身高',.01)
        if profile['sample_review']['status']=='accepted' and profile['sample_review']['scope']=='all_actions':
            require(meta.get('review_required') is False,'自动锚点候选尚未核对，不能标样稿通过')
        for frame,aid in zip(meta['frames'],ids):
            require(isinstance(frame,dict) and frame.get('file')==assets[aid]['source'].name,'动作meta文件名/顺序与frame_ids不匹配')
            size=assets[aid]['image']['size']
            require([frame.get('w'),frame.get('h')]==size,'动作meta尺寸与PNG不匹配')
            for key,limit in [('ax',size[0]),('ay',size[1])]:
                finite(frame.get(key),key); require(frame[key]<=limit,'锚点超出帧尺寸')
            finite(frame.get('air',0),'离地量')
            if 'sha256' in frame: require(frame['sha256']==assets[aid]['sha256'],'动作帧哈希不匹配')
        actions[action['name']]={'source':path,'meta':meta}
    return assets,actions


def definitions(profile):
    return {'identity':profile['identity'],
            'assets':[{k:a.get(k) for k in ('id','role','sha256')} for a in profile['assets']],
            'actions':[{k:a.get(k) for k in ('name','frame_ids','metadata_sha256','fps','loop')} for a in profile['actions']]}


def names(profile):
    return {s.casefold() for s in [profile['character_id'],profile['name'],*profile['aliases']]}


def history(root):
    return [read_json(p) for p in sorted((Path(root)/'characters').glob('*/r*/profile.json'))]


def resolve_profile(root,query):
    name,sep,revision=query.rpartition('@')
    if not sep: name=query; revision=None
    else: require(revision.isdigit() and int(revision)>0,'版本须为正整数')
    values=history(root)
    matching=[p for p in values if name.casefold() in names(p)]
    ids={p['character_id'] for p in matching}
    require(len(ids)==1,'角色名称未找到或有冲突；先list，不静默换角色')
    matching=[p for p in values if p['character_id'] in ids and (revision is None or p['revision']==int(revision))]
    require(bool(matching),'请求的角色版本不存在')
    profile=max(matching,key=lambda p:p['revision'])
    path=Path(root)/'characters'/profile['character_id']/f"r{profile['revision']:04}"/'profile.json'
    validate_profile(profile,path.parent)
    return profile,path


@contextmanager
def library_lock(root):
    directory=Path(root)/'characters'; directory.mkdir(parents=True,exist_ok=True)
    path=directory/'.write.lock'
    try:
        with path.open('x') as file: file.write(str(os.getpid()))
    except FileExistsError as exc:
        raise AssetError('角色库正被写入；遗留锁须核对进程后人工移除') from exc
    try: yield
    finally: path.unlink()


def copy_original(source,target,expected):
    with Path(source).open('rb') as src, Path(target).open('xb') as dest:
        shutil.copyfileobj(src,dest)
    require(sha256(target)==expected,'素材复制时变化；该快照未完成')


def save_profile(root,profile_path):
    incoming=read_json(profile_path)
    assets,actions=validate_profile(incoming,Path(profile_path).resolve().parent)
    stored=copy.deepcopy(incoming)
    for asset in stored['assets']:
        detail=assets[asset['id']]
        asset['file']='assets/'+asset['id']+EXTENSIONS[detail['image']['format']]
        asset['sha256']=detail['sha256']; asset['image']=detail['image']
    rewritten={}
    for action in stored['actions']:
        meta=copy.deepcopy(actions[action['name']]['meta'])
        for frame,aid in zip(meta['frames'],action['frame_ids']):
            frame['file']=Path(next(a['file'] for a in stored['assets'] if a['id']==aid)).name
            frame['sha256']=assets[aid]['sha256']
        action['metadata_file']='assets/'+action['name']+'.meta.json'
        encoded=(json.dumps(meta,ensure_ascii=False,indent=2,allow_nan=False)+'\n').encode('utf-8')
        action['metadata_sha256']=hashlib.sha256(encoded).hexdigest()
        rewritten[action['name']]=meta
    with library_lock(root):
        previous=history(root)
        for old in previous:
            if old['character_id']!=stored['character_id']:
                require(not names(old)&names(stored),'角色名/别名与其他角色历史冲突')
            else:
                if stored['sample_review']['reference'] and stored['sample_review']['reference']==old['sample_review']['reference']:
                    require(stored['sample_review']['scope']==old['sample_review']['scope'],'同一反馈不能扩大静帧/动作认可范围')
                if definitions(old)!=definitions(stored):
                    for field in ('approval','sample_review'):
                        reference=stored[field]['reference']
                        require(not reference or reference!=old[field]['reference'],'角色身份/素材变化不能复用其他定义的旧确认或样稿反馈')
        directory=Path(root)/'characters'/stored['character_id']
        directory.mkdir(exist_ok=True)
        numbers=[int(p.name[1:]) for p in directory.iterdir() if p.is_dir() and re.fullmatch(r'r[0-9]+',p.name)]
        stored['revision']=max(numbers,default=0)+1
        stored['saved_at']=datetime.now(timezone.utc).isoformat()
        snapshot=directory/f"r{stored['revision']:04}"; snapshot.mkdir(exist_ok=False)
        (snapshot/'assets').mkdir()
        for asset in stored['assets']:
            copy_original(assets[asset['id']]['source'],snapshot/asset['file'],asset['sha256'])
        for action in stored['actions']:
            write_json(snapshot/action['metadata_file'],rewritten[action['name']])
        validate_profile(stored,snapshot)
        write_json(snapshot/'profile.json',stored)  # Publish only after all original assets validate.
    return stored


def export_profile(root,query,output):
    profile,path=resolve_profile(root,query)
    output=Path(output)
    require(not output.exists(),'项目导出目录已存在；不能覆盖')
    output.mkdir(parents=True,exist_ok=False); (output/'assets').mkdir()
    for asset in profile['assets']:
        copy_original(path.parent/asset['file'],output/asset['file'],asset['sha256'])
    for action in profile['actions']:
        copy_original(path.parent/action['metadata_file'],output/action['metadata_file'],action['metadata_sha256'])
    write_json(output/'profile.json',profile)
    return {'character':f"{profile['character_id']}@{profile['revision']}",'path':str(output.resolve()),
            'approval':profile['approval'],'sample_review':profile['sample_review']}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(os.getenv('CODEX_HOME',str(Path.home()/'.codex')))/'config/huashu-art-motion')
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('list')
    cmd=sub.add_parser('show'); cmd.add_argument('character')
    for name in ('validate','save'):
        cmd=sub.add_parser(name); cmd.add_argument('--file',type=Path,required=True)
    cmd=sub.add_parser('export'); cmd.add_argument('character'); cmd.add_argument('--out',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        if args.command=='list':
            latest={}
            for p in history(args.root):
                if p['revision']>latest.get(p['character_id'],{}).get('revision',0): latest[p['character_id']]=p
            value=[{k:p[k] for k in ('character_id','name','aliases','revision','summary','approval','sample_review')}
                   for p in latest.values()]
        elif args.command=='show': value=resolve_profile(args.root,args.character)[0]
        elif args.command=='validate':
            validate_profile(read_json(args.file),args.file.resolve().parent); value={'valid':True}
        elif args.command=='save':
            p=save_profile(args.root,args.file); value={k:p[k] for k in ('character_id','revision','approval','sample_review')}
        else: value=export_profile(args.root,args.character,args.out)
        print(json.dumps(value,ensure_ascii=False,indent=2)); return 0
    except (AssetError,OSError,ValueError,TypeError,KeyError) as exc:
        print('错误: '+str(exc),file=sys.stderr); return 1


if __name__=='__main__':
    raise SystemExit(main())
