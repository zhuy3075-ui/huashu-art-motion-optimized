"""Shared image decoding, alpha-safe chroma key and exclusive asset writes."""
import hashlib
import io
import json
import math
from pathlib import Path
import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

MAX_PIXELS = 40_000_000


class AssetError(ValueError):
    pass


def read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise AssetError(f'JSON重复字段: {key}')
            result[key] = value
        return result
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'), object_pairs_hook=unique,
                          parse_constant=lambda value: (_ for _ in ()).throw(AssetError('非法JSON数值')))
    except (OSError, ValueError) as exc:
        raise AssetError(f'不能读取JSON: {path}') from exc


def write_json(path, value):
    encoded=(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n').encode('utf-8')
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as output:
        output.write(encoded)


def sha256(path):
    with Path(path).open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256')
    return digest.hexdigest()


def read_image(path, allow_animated=False):
    try:
        with Image.open(path) as source:
            if source.width * source.height > MAX_PIXELS:
                raise AssetError('图片超过4000万像素，请先确认所需尺寸')
            frames = getattr(source, 'n_frames', 1)
            if frames > 1 and not allow_animated:
                raise AssetError('多帧文件需显式提取帧，不能仅取第一帧当完整素材')
            original = {'format': source.format, 'source_size': list(source.size), 'frames': frames,
                        'orientation': source.getexif().get(274, 1)}
            image = ImageOps.exif_transpose(source).convert('RGBA')
            image.load()
        return image, original
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise AssetError(f'图片不能解码: {path}') from exc


def inspect_image(path):
    image, original = read_image(path, allow_animated=True)
    alpha = np.asarray(image)[..., 3]
    bbox=image.getchannel('A').getbbox()
    return {**original, 'size': list(image.size), 'exif_transposed_for_inspection': original['orientation'] != 1,
            'has_transparency': bool((alpha < 255).any()), 'has_semitransparency': bool(((alpha > 0) & (alpha < 255)).any()),
            'visible_bbox': list(bbox) if bbox else None,
            'checks': ['尚需实际查看画面内容、出镜尺寸和边缘；解码检查不等于用户认可']}


def prepare_pixels(image, mode='auto', soft=30, hard=100):
    pixels = np.asarray(image).copy()
    has_alpha = bool((pixels[..., 3] < 255).any())
    if mode == 'auto':
        if not has_alpha:
            raise AssetError('不透明图片请明确选择--mode green/white/preserve；普通照片不会自动抠图')
        mode = 'preserve'
    if mode not in {'preserve', 'green', 'white'}:
        raise AssetError(f'未知背景模式: {mode}')
    if mode == 'green':
        if not all(math.isfinite(v) for v in (soft, hard)) or not 0 <= soft < hard <= 255:
            raise AssetError('绿幕阈值必须满足0 <= soft < hard <= 255')
        r, g, b = (pixels[..., i].astype(np.float32) for i in range(3))
        spill = g - np.maximum(r, b)
        keyed = 1 - np.clip((spill-soft)/(hard-soft), 0, 1)
        pixels[..., 3] = np.rint(pixels[..., 3].astype(np.float32)*keyed).astype(np.uint8)
        corrected = np.where(spill > 0, np.minimum(g, np.maximum(r,b)+.25*np.clip(spill,0,30)),g)
        pixels[..., 1] = np.rint(corrected).astype(np.uint8)
    mask = pixels[..., 3] > 0
    if mode == 'white':
        rgb = pixels[..., :3].astype(np.int16)
        mask &= (rgb.min(axis=2) < 225) | (rgb.max(axis=2)-rgb.min(axis=2) > 40)
    return pixels, mask, mode


def bounds(mask, pad=0):
    if type(pad) is not int or pad < 0:
        raise AssetError('裁切留边须为非负整数')
    ys, xs = np.where(mask)
    if not len(xs):
        raise AssetError('没有可见主体，不能跳过后改变帧序号')
    height, width = mask.shape
    return max(0,int(xs.min())-pad), max(0,int(ys.min())-pad), min(width,int(xs.max())+pad+1), min(height,int(ys.max())+pad+1)


def save_png(path, pixels):
    encoded = io.BytesIO()
    Image.fromarray(pixels).save(encoded, format='PNG')
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as output:
        output.write(encoded.getvalue())
