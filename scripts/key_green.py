#!/usr/bin/env python3
# /// script
# dependencies = ["pillow", "numpy"]
# ///
"""Crop transparent or explicitly keyed green assets, preserving canvas coordinates."""
import argparse
import glob
from pathlib import Path
import sys
from image_io import AssetError, bounds, prepare_pixels, read_image, save_png, sha256, write_json


def crop_assets(files, output, mode='auto', soft=30, hard=100, pad=4):
    output=Path(output)
    if output.exists():
        raise AssetError('导出目录已存在；请用新目录，避免覆盖已有sprites.json或图片')
    plans=[]
    names=set()
    for source in files:
        source=Path(source)
        name=source.stem
        if name.casefold() in names:
            raise AssetError('同名素材会冲突，请分别导出或先确认命名')
        names.add(name.casefold())
        image,info=read_image(source)
        pixels,mask,used_mode=prepare_pixels(image,mode,soft,hard)
        x0,y0,x1,y1=bounds(mask,pad)
        plans.append((name,pixels[y0:y1,x0:x1].copy(),
            {'x':x0,'y':y0,'w':x1-x0,'h':y1-y0,'source_size':list(image.size),
             'source_orientation':info['orientation'],'mode':used_mode,'source_sha256':sha256(source)}))
    if not plans:
        raise AssetError('没有输入素材')
    output.mkdir(parents=True,exist_ok=False)
    meta={}
    for name,pixels,record in plans:
        target=output/(name+'.png')
        save_png(target,pixels)
        meta[name]={**record,'file':target.name,'sha256':sha256(target)}
    write_json(output/'sprites.json',meta)
    return meta


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('files',nargs='+'); parser.add_argument('--out',required=True,type=Path)
    parser.add_argument('--mode',choices=['auto','preserve','green'],default='auto')
    parser.add_argument('--soft',type=float,default=30); parser.add_argument('--hard',type=float,default=100)
    parser.add_argument('--pad',type=int,default=4)
    args=parser.parse_args(argv)
    try:
        files=[]
        for pattern in args.files:
            matches=glob.glob(pattern) if glob.has_magic(pattern) else [pattern]
            if not matches:
                raise AssetError(f'输入模式没有匹配文件: {pattern}')
            files.extend(sorted(matches))
        meta=crop_assets(files,args.out,args.mode,args.soft,args.hard,args.pad)
        print(f'裁切成功：{len(meta)}项，坐标保存在{args.out}/sprites.json')
        return 0
    except (AssetError,OSError) as exc:
        print('错误: '+str(exc),file=sys.stderr); return 1


if __name__=='__main__':
    raise SystemExit(main())

