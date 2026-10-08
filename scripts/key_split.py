#!/usr/bin/env python3
# /// script
# dependencies = ["pillow", "numpy"]
# ///
"""Split transparent/green/white sprite sheets; --grid 4x2 means columns x rows."""
import argparse
from pathlib import Path
import re
import sys
from image_io import AssetError, bounds, prepare_pixels, read_image, save_png, sha256, write_json


def split_sheet(source, prefix, mode='auto', grid=None, pick=None, pad=0, gap=12):
    image, info = read_image(source)
    pixels, mask, mode = prepare_pixels(image, mode)
    width, height = image.size
    cells = []
    if grid is not None:
        if not isinstance(grid,str) or not re.fullmatch(r'[1-9][0-9]*x[1-9][0-9]*',grid):
            raise AssetError('网格须为列x行，例如4x2')
        cols, rows = map(int,grid.split('x'))
        if cols*rows>256:
            raise AssetError('单张帧图最多256格，请按动作拆分')
        if width % cols or height % rows:
            raise AssetError('图片尺寸不能整除网格；请先核对布局，不自动丢像素')
        cw, ch = width//cols, height//rows
        cells = [(col*cw,row*ch,(col+1)*cw,(row+1)*ch) for row in range(rows) for col in range(cols)]
    else:
        if type(gap) is not int or gap < 1:
            raise AssetError('列间检测gap须为正整数')
        occupied = mask.any(axis=0)
        indices = occupied.nonzero()[0].tolist()
        if not indices:
            raise AssetError('帧图没有可见主体')
        left = previous = indices[0]
        for index in indices[1:]:
            if index-previous > gap:
                cells.append((left,0,previous+1,height)); left=index
            previous=index
        cells.append((left,0,previous+1,height))
    if pick is not None and (type(pick) is not int or not 0 <= pick < len(cells)):
        raise AssetError('pick超出实际帧序号')
    prefix = Path(prefix)
    plans = []
    for index,(left,top,right,bottom) in enumerate(cells):
        if pick is not None and pick != index:
            continue
        x0,y0,x1,y1 = bounds(mask[top:bottom,left:right],pad)
        target = Path(str(prefix)+('' if pick is not None else '_'+str(index))+'.png')
        crop = pixels[top+y0:top+y1,left+x0:left+x1].copy()
        plans.append((target,crop,{'file':target.name,'index':index,'w':x1-x0,'h':y1-y0,
                     'source_box':[left+x0,top+y0,left+x1,top+y1], 'cell_box':[left,top,right,bottom],
                     'ax':(x1-x0)/2,'ay':y1-y0-1,'air':0}))
    meta_path = Path(str(prefix)+'.meta.json')
    if len({str(p[0].resolve()) for p in plans}) != len(plans) or any(p[0].exists() for p in plans) or meta_path.exists():
        raise AssetError('输出已存在，不能覆盖；请换新前缀')
    for target,crop,frame in plans:
        save_png(target,crop)
        frame['sha256'] = sha256(target)
    meta = {'schema_version':1,'source_sha256':sha256(source),'source_size':list(image.size),
            'source_orientation':info['orientation'],'mode':mode,'grid':grid,
            'order':'row_major' if grid else 'left_to_right_single_strip',
            'frames':[p[2] for p in plans], 'ref_h':plans[0][2]['h'],
            'review_required':True,'anchor_source':'bbox_bottom_candidate',
            'note':'ax/ay、air与ref_h是候选，须用合成样稿核对脚底、站立身高及离地量；不是已确认动作'}
    write_json(meta_path,meta)
    return meta


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('src',type=Path); parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--grid'); parser.add_argument('--pick',type=int)
    parser.add_argument('--mode',choices=['auto','preserve','green','white'],default='auto')
    parser.add_argument('--white',action='store_true',help='兼容旧参数：白底线稿只裁不抠')
    parser.add_argument('--pad',type=int,default=0); parser.add_argument('--gap',type=int,default=12)
    args=parser.parse_args(argv)
    try:
        if args.white and args.mode not in {'auto','white'}:
            raise AssetError('--white与mode冲突')
        meta=split_sheet(args.src,args.out,'white' if args.white else args.mode,args.grid,args.pick,args.pad,args.gap)
        print(f"切帧成功：{len(meta['frames'])}帧，元数据{args.out}.meta.json；锚点待检查")
        return 0
    except (AssetError,OSError) as exc:
        print('错误: '+str(exc),file=sys.stderr); return 1


if __name__=='__main__':
    raise SystemExit(main())

