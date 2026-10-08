#!/usr/bin/env python3
# /// script
# dependencies = ["pillow", "numpy"]
# ///
"""Inspect user images and copy originals into a new private project asset folder."""
import argparse
import json
from pathlib import Path
import shutil
import sys
from image_io import AssetError, inspect_image, sha256, write_json


def inspect_files(files):
    if not files:
        raise AssetError('没有输入图片')
    result=[]
    for index,path in enumerate(files,1):
        path=Path(path).resolve()
        result.append({'id':f'A{index:03}','original_name':path.name,'source':str(path),
                       'sha256':sha256(path),'image':inspect_image(path),
                       'usage':{'role':'unassigned','treatment':'unconfirmed','approval_reference':''}})
    return result


def stage_files(files,output):
    output=Path(output)
    if output.exists():
        raise AssetError('素材导入目录已存在；请使用新目录，不覆盖原件或旧清单')
    records=inspect_files(files)
    output.mkdir(parents=True,exist_ok=False)
    originals=output/'originals'; originals.mkdir()
    for record in records:
        filename=record['id']+Path(record['source']).suffix.lower()
        target=originals/filename
        with Path(record['source']).open('rb') as source, target.open('xb') as destination:
            shutil.copyfileobj(source,destination)
        if sha256(target)!=record['sha256']:
            raise AssetError('输入图片在导入时发生变化；导入未完成，不作为确认素材')
        record['file']='originals/'+filename
    manifest={'schema_version':1,'state':'awaiting_usage_confirmation','assets':records,
              'note':'原件字节保留；用途/改绘/裁切/外部服务授权待核对，不因导入自动生成或渲染'}
    write_json(output/'asset_inventory.json',manifest)
    return manifest


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    cmd=sub.add_parser('inspect'); cmd.add_argument('files',nargs='+',type=Path)
    cmd=sub.add_parser('stage'); cmd.add_argument('files',nargs='+',type=Path); cmd.add_argument('--out',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        value=inspect_files(args.files) if args.command=='inspect' else stage_files(args.files,args.out)
        print(json.dumps(value,ensure_ascii=False,indent=2)); return 0
    except (AssetError,OSError,ValueError) as exc:
        print('错误: '+str(exc),file=sys.stderr); return 1


if __name__=='__main__':
    raise SystemExit(main())
