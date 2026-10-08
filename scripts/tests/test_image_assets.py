import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
import numpy as np
from PIL import Image

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import image_assets
import image_io
import key_green
import key_split


class ImageAssetsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
    def tearDown(self): self.temp.cleanup()
    def image(self,name='image.png',pixels=None):
        if pixels is None:
            pixels=np.zeros((16,16,4),dtype=np.uint8)
            pixels[4:12,3:10]=[0,220,0,128]
        path=self.root/name; Image.fromarray(pixels).save(path); return path
    def sheet(self,empty=None,green=False):
        pixels=np.zeros((40,80,4),dtype=np.uint8)
        if green: pixels[:]=[0,255,0,255]
        for i in range(8):
            if i==empty: continue
            x=(i%4)*20+3; y=(i//4)*20+4
            pixels[y:y+10,x:x+8]=[i*25,20,180,128 if not green else 255]
        return self.image('sheet.png',pixels)
    def test_inspect_reports_alpha_and_does_not_modify(self):
        source=self.image(); digest=image_io.sha256(source)
        record=image_assets.inspect_files([source])[0]
        self.assertEqual(record['image']['size'],[16,16])
        self.assertTrue(record['image']['has_semitransparency'])
        self.assertEqual(record['usage']['treatment'],'unconfirmed')
        self.assertEqual(image_io.sha256(source),digest)
    def test_stage_preserves_original_bytes_and_duplicate_names(self):
        source=self.image(); folder=self.root/'other'; folder.mkdir()
        other=folder/source.name; other.write_bytes(source.read_bytes())
        value=image_assets.stage_files([source,other],self.root/'imported')
        self.assertEqual(value['state'],'awaiting_usage_confirmation')
        for asset in value['assets']:
            self.assertEqual((self.root/'imported'/asset['file']).read_bytes(),source.read_bytes())
        self.assertNotEqual(value['assets'][0]['file'],value['assets'][1]['file'])
    def test_stage_existing_directory_not_overwritten(self):
        target=self.root/'keep'; target.mkdir(); (target/'private.txt').write_text('mine')
        with self.assertRaises(image_io.AssetError): image_assets.stage_files([self.image()],target)
        self.assertEqual((target/'private.txt').read_text(),'mine')
    def test_invalid_input_does_not_create_stage(self):
        bad=self.root/'bad.png'; bad.write_bytes(b'not png')
        with self.assertRaises(image_io.AssetError): image_assets.stage_files([bad],self.root/'out')
        self.assertFalse((self.root/'out').exists())
    def test_auto_preserves_green_clothes_and_half_alpha(self):
        source=self.image()
        meta=key_green.crop_assets([source],self.root/'crop',pad=0)
        output=np.asarray(Image.open(self.root/'crop/image.png'))
        self.assertEqual(output.shape,(8,7,4))
        self.assertTrue((output==[0,220,0,128]).all())
        self.assertEqual((meta['image']['x'],meta['image']['y']),(3,4))
        self.assertEqual(meta['image']['mode'],'preserve')
    def test_low_alpha_pixels_preserved(self):
        pixels=np.zeros((5,5,4),dtype=np.uint8); pixels[2,2]=[1,2,3,1]
        source=self.image(pixels=pixels)
        key_green.crop_assets([source],self.root/'crop',pad=0)
        self.assertEqual(Image.open(self.root/'crop/image.png').getpixel((0,0)),(1,2,3,1))
    def test_opaque_photo_requires_explicit_mode(self):
        source=self.root/'photo.jpg'; Image.new('RGB',(20,20),'green').save(source)
        with self.assertRaises(image_io.AssetError): key_green.crop_assets([source],self.root/'crop')
        self.assertFalse((self.root/'crop').exists())
        key_green.crop_assets([source],self.root/'preserved',mode='preserve')
        with Image.open(self.root/'preserved/photo.png') as image:
            self.assertEqual(image.size,(20,20))
    def test_green_mode_multiplies_original_alpha(self):
        image=Image.new('RGBA',(2,1)); image.putdata([(255,0,0,128),(0,255,0,128)])
        pixels,mask,_=image_io.prepare_pixels(image,'green')
        self.assertEqual(pixels[0,0,3],128); self.assertEqual(pixels[0,1,3],0)
        self.assertEqual(mask.tolist(),[[True,False]])
    def test_green_invalid_thresholds_rejected(self):
        for soft,hard in [(100,30),(-1,30),(30,float('nan')),(30,300)]:
            with self.subTest(soft=soft,hard=hard):
                with self.assertRaises(image_io.AssetError): image_io.prepare_pixels(Image.new('RGBA',(2,2)),'green',soft,hard)
    def test_four_by_two_grid_eight_frames_correct_row_order(self):
        source=self.sheet(); original=source.read_bytes()
        meta=key_split.split_sheet(source,self.root/'walk',grid='4x2')
        self.assertEqual(len(meta['frames']),8)
        self.assertEqual(meta['order'],'row_major')
        for i,frame in enumerate(meta['frames']):
            self.assertEqual(Image.open(self.root/frame['file']).getpixel((0,0)),(i*25,20,180,128))
            self.assertEqual(frame['source_box'],[(i%4)*20+3,(i//4)*20+4,(i%4)*20+11,(i//4)*20+14])
        self.assertEqual(source.read_bytes(),original)
        self.assertTrue(meta['review_required'])
    def test_green_grid_eight_frames(self):
        meta=key_split.split_sheet(self.sheet(green=True),self.root/'walk',mode='green',grid='4x2')
        self.assertEqual(len(meta['frames']),8)
    def test_empty_cell_rejected_before_any_output(self):
        with self.assertRaises(image_io.AssetError): key_split.split_sheet(self.sheet(empty=3),self.root/'walk',grid='4x2')
        self.assertEqual(list(self.root.glob('walk*')),[])
    def test_grid_invalid_or_not_divisible(self):
        source=self.sheet()
        for grid in ['4X2','0x2','3x2','4x3']:
            with self.subTest(grid=grid):
                with self.assertRaises(image_io.AssetError): key_split.split_sheet(source,self.root/'walk',grid=grid)
    def test_pick_keeps_source_index(self):
        meta=key_split.split_sheet(self.sheet(),self.root/'single',grid='4x2',pick=5)
        self.assertEqual(meta['frames'][0]['index'],5)
        self.assertEqual(meta['frames'][0]['file'],'single.png')
        self.assertEqual(Image.open(self.root/'single.png').getpixel((0,0)),(125,20,180,128))
    def test_strip_finds_small_sprites_without_40_pixel_requirement(self):
        pixels=np.zeros((16,50,4),dtype=np.uint8)
        pixels[2:8,2:8]=[1,2,3,255]; pixels[2:8,35:41]=[4,5,6,255]
        meta=key_split.split_sheet(self.image(pixels=pixels),self.root/'strip')
        self.assertEqual(len(meta['frames']),2)
    def test_output_collision_protects_entire_sheet(self):
        source=self.sheet(); (self.root/'walk_6.png').write_bytes(b'user-file')
        with self.assertRaises(image_io.AssetError): key_split.split_sheet(source,self.root/'walk',grid='4x2')
        self.assertFalse((self.root/'walk_0.png').exists())
        self.assertEqual((self.root/'walk_6.png').read_bytes(),b'user-file')
    def test_white_crop_preserves_opaque_white_for_multiply(self):
        pixels=np.full((20,20,4),255,dtype=np.uint8); pixels[3:15,4:12,:3]=0
        meta=key_split.split_sheet(self.image(pixels=pixels),self.root/'white',mode='white',pad=1)
        out=np.asarray(Image.open(self.root/meta['frames'][0]['file']))
        self.assertTrue((out[...,3]==255).all()); self.assertEqual(out[0,0].tolist(),[255,255,255,255])
    def test_duplicate_crop_basenames_rejected(self):
        source=self.image(); folder=self.root/'other'; folder.mkdir(); copy=folder/source.name; copy.write_bytes(source.read_bytes())
        with self.assertRaises(image_io.AssetError): key_green.crop_assets([source,copy],self.root/'out')
        self.assertFalse((self.root/'out').exists())
    def test_palette_transparency_preserved(self):
        im=Image.new('P',(16,16),0); im.putpalette([0,0,0,255,0,0]+[0]*762)
        for x in range(4,8):
            for y in range(3,9): im.putpixel((x,y),1)
        source=self.root/'palette.png'; im.save(source,transparency=bytes([0,128]+[255]*254))
        key_green.crop_assets([source],self.root/'crop',pad=0)
        self.assertEqual(Image.open(self.root/'crop/palette.png').getpixel((0,0)),(255,0,0,128))
    def test_exif_orientation_inspected_without_changing_original(self):
        source=self.root/'oriented.jpg'; im=Image.new('RGB',(30,10),'red'); exif=im.getexif(); exif[274]=6; im.save(source,exif=exif)
        digest=image_io.sha256(source); info=image_io.inspect_image(source)
        self.assertEqual(info['size'],[10,30]); self.assertEqual(info['source_size'],[30,10]); self.assertEqual(image_io.sha256(source),digest)
    def test_animated_input_identified_but_not_silently_first_frame(self):
        source=self.root/'animated.gif'; Image.new('RGB',(16,16),'red').save(source,save_all=True,append_images=[Image.new('RGB',(16,16),'blue')],duration=50,loop=0)
        self.assertEqual(image_io.inspect_image(source)['frames'],2)
        with self.assertRaises(image_io.AssetError): key_split.split_sheet(source,self.root/'walk',mode='preserve')
    def test_cli_white_and_pick_conflict_or_out_of_range(self):
        source=self.sheet()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(key_split.main([str(source),'--out',str(self.root/'x'),'--white','--mode','green']),1)
            self.assertEqual(key_split.main([str(source),'--out',str(self.root/'x'),'--grid','4x2','--pick','8']),1)


if __name__=='__main__': unittest.main()
