import copy
from pathlib import Path
import sys
import tempfile
import unittest
import numpy as np
from PIL import Image

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import character_library as library
from image_io import AssetError,read_json,sha256,write_json
import key_split


class CharacterLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.store=self.root/'private-config'; self.profile_path=self.root/'draft.json'
        self.master=self.root/'master.png'; Image.new('RGBA',(30,40),(200,30,0,128)).save(self.master)
        self.profile={'schema_version':1,'character_id':'my-actor','name':'我的角色','aliases':['小橙'],
            'summary':'用于故事旁白','identity':{'preserve':['眼镜和脸型'],'allowed_changes':['姿势'],
             'forbidden_changes':['换脸'],'unknown':['背面细节']},
            'assets':[{'id':'master','role':'master','file':str(self.master)}], 'actions':[],
            'approval':{'status':'draft','reference':''},'sample_review':{'status':'not_reviewed','reference':'','scope':'static'}}
    def tearDown(self): self.temp.cleanup()
    def save(self,profile=None):
        if self.profile_path.exists(): self.profile_path.unlink()
        write_json(self.profile_path,profile or self.profile)
        return library.save_profile(self.store,self.profile_path)
    def confirmed(self,ref='用户确认角色v1'):
        self.profile['approval']={'status':'confirmed','reference':ref}
        return self.profile
    def add_action(self):
        pixels=np.zeros((20,40,4),dtype=np.uint8)
        pixels[3:16,3:12]=[200,20,0,255]; pixels[4:17,24:34]=[100,20,0,255]
        sheet=self.root/'sheet.png'; Image.fromarray(pixels).save(sheet)
        meta=key_split.split_sheet(sheet,self.root/'walk',grid='2x1')
        for i,frame in enumerate(meta['frames']):
            self.profile['assets'].append({'id':f'walk-{i}','role':'frame','file':str(self.root/frame['file'])})
        self.profile['actions']=[{'name':'walk','frame_ids':['walk-0','walk-1'],
                         'metadata_file':str(self.root/'walk.meta.json'),'fps':9,'loop':True}]
        return meta
    def test_save_copies_real_assets_no_auto_confirmation(self):
        p=self.save(); saved=self.store/'characters/my-actor/r0001'
        self.assertEqual(p['revision'],1); self.assertEqual(p['approval']['status'],'draft')
        self.assertEqual((saved/p['assets'][0]['file']).read_bytes(),self.master.read_bytes())
        self.assertEqual(p['assets'][0]['sha256'],sha256(self.master))
    def test_reuse_survives_temporary_source_removal(self):
        self.save(); self.master.unlink(); self.profile_path.unlink()
        p,_=library.resolve_profile(self.store,'小橙@1')
        self.assertEqual(p['character_id'],'my-actor')
        out=self.root/'project-role'; library.export_profile(self.store,'我的角色@1',out)
        library.validate_profile(read_json(out/'profile.json'),out)
    def test_append_version_preserves_older_bytes(self):
        self.save(); first=self.store/'characters/my-actor/r0001/profile.json'; original=first.read_bytes()
        self.profile['summary']='另一段旁白'; p=self.save()
        self.assertEqual(p['revision'],2); self.assertEqual(first.read_bytes(),original)
        self.assertEqual(library.resolve_profile(self.store,'my-actor@1')[0]['summary'],'用于故事旁白')
    def test_names_unknown_revision_and_duplicate_alias(self):
        self.save()
        for query in ['missing','my-actor@9','my-actor@-1']:
            with self.subTest(query=query):
                with self.assertRaises(AssetError): library.resolve_profile(self.store,query)
        self.profile['aliases']=['小橙','小橙']
        with self.assertRaises(AssetError): self.save()
    def test_cross_character_alias_collision(self):
        self.save(); self.profile['character_id']='other-actor'; self.profile['name']='另一角色'
        with self.assertRaises(AssetError): self.save()
    def test_existing_export_preserves_user_assets(self):
        self.save(); target=self.root/'existing'; target.mkdir(); (target/'user.txt').write_text('mine')
        with self.assertRaises(AssetError): library.export_profile(self.store,'my-actor',target)
        self.assertEqual((target/'user.txt').read_text(),'mine')
    def test_missing_master_or_preserve_rules_rejected(self):
        self.profile['assets'][0]['role']='reference'
        with self.assertRaises(AssetError): self.save()
        self.profile['assets'][0]['role']='master'; self.confirmed(); self.profile['identity']['preserve']=[]
        with self.assertRaises(AssetError): self.save()
    def test_missing_source_no_publish(self):
        self.master.unlink()
        with self.assertRaises(AssetError): self.save()
        self.assertEqual(library.history(self.store),[])
    def test_saved_asset_tampering_rejected(self):
        p=self.save(); asset=self.store/'characters/my-actor/r0001'/p['assets'][0]['file']
        Image.new('RGBA',(30,40),'blue').save(asset)
        with self.assertRaises(AssetError): library.resolve_profile(self.store,'my-actor')
    def test_changed_identity_cannot_reuse_old_approval(self):
        self.confirmed(); self.save(); self.profile['identity']['preserve']=['新脸型']
        with self.assertRaises(AssetError): self.save()
        self.confirmed('用户确认新脸型v2'); self.assertEqual(self.save()['revision'],2)
    def test_draft_then_old_approval_cannot_be_reactivated(self):
        self.confirmed(); self.save(); old_ref=self.profile['approval']['reference']
        self.profile['identity']['preserve']=['新脸型']; self.profile['approval']={'status':'draft','reference':''}; self.save()
        self.profile['approval']={'status':'confirmed','reference':old_ref}
        with self.assertRaises(AssetError): self.save()
    def test_same_character_metadata_revision_keeps_valid_approval(self):
        self.confirmed(); self.save(); self.profile['name']='新名称'
        self.assertEqual(self.save()['approval']['reference'],'用户确认角色v1')
    def test_changed_asset_cannot_keep_old_sample_acceptance(self):
        self.confirmed(); self.profile['sample_review']={'status':'accepted','reference':'用户认可样稿A','scope':'static'}; self.save()
        Image.new('RGBA',(30,40),'blue').save(self.master)
        self.confirmed('用户确认母版B')
        with self.assertRaises(AssetError): self.save()
        self.profile['sample_review']={'status':'not_reviewed','reference':'','scope':'static'}; self.save()
    def test_action_metadata_rewritten_for_saved_and_exported_assets(self):
        self.add_action(); p=self.save()
        stored=self.store/'characters/my-actor/r0001'; meta=read_json(stored/p['actions'][0]['metadata_file'])
        self.assertEqual([f['file'] for f in meta['frames']],['walk-0.png','walk-1.png'])
        self.assertTrue(meta['review_required'])
        self.master.unlink(); (self.root/'walk_0.png').unlink(); (self.root/'walk_1.png').unlink()
        target=self.root/'export'; library.export_profile(self.store,'小橙@1',target)
        library.validate_profile(read_json(target/'profile.json'),target)
    def test_unreviewed_anchor_cannot_be_marked_accepted(self):
        self.add_action(); self.confirmed(); self.profile['sample_review']={'status':'accepted','reference':'用户样稿认可','scope':'all_actions'}
        with self.assertRaises(AssetError): self.save()
    def test_static_sample_acceptance_does_not_accept_actions(self):
        self.add_action(); self.confirmed()
        self.profile['sample_review']={'status':'accepted','reference':'仅确认静帧A','scope':'static'}
        self.assertEqual(self.save()['sample_review']['scope'],'static')
        # Even after anchor review, the same static feedback cannot authorize actions.
        path=self.root/'walk.meta.json'; meta=read_json(path); meta['review_required']=False
        path.unlink(); write_json(path,meta)
        self.profile['sample_review']['scope']='all_actions'
        with self.assertRaises(AssetError): self.save()
    def test_static_feedback_cannot_upgrade_scope_for_identical_definition(self):
        self.add_action(); path=self.root/'walk.meta.json'; meta=read_json(path)
        meta['review_required']=False; path.unlink(); write_json(path,meta)
        self.confirmed(); self.profile['sample_review']={'status':'accepted','reference':'只确认静帧','scope':'static'}
        self.save(); self.profile['sample_review']['scope']='all_actions'
        with self.assertRaises(AssetError): self.save()
        self.profile['sample_review']['reference']='新的全部动作反馈'
        self.assertEqual(self.save()['sample_review']['scope'],'all_actions')
    def test_all_actions_scope_without_actions_rejected(self):
        self.confirmed(); self.profile['sample_review']={'status':'accepted','reference':'认可动作','scope':'all_actions'}
        with self.assertRaises(AssetError): self.save()
    def test_anchor_outside_frame_or_invalid_fps_rejected(self):
        self.add_action(); path=self.root/'walk.meta.json'; meta=read_json(path)
        meta['frames'][0]['ax']=999; path.unlink(); write_json(path,meta)
        with self.assertRaises(AssetError): self.save()
        meta['frames'][0]['ax']=4; path.unlink(); write_json(path,meta)
        self.profile['actions'][0]['fps']=float('inf')
        with self.assertRaises(AssetError): library.validate_profile(self.profile,self.root)
    def test_frame_order_and_dimension_mismatch_rejected(self):
        self.add_action(); self.profile['actions'][0]['frame_ids'].reverse()
        with self.assertRaises(AssetError): self.save()
    def test_confirmed_has_reference_draft_has_none(self):
        self.profile['approval']={'status':'confirmed','reference':''}
        with self.assertRaises(AssetError): self.save()
        self.profile['approval']={'status':'draft','reference':'旧的确认'}
        with self.assertRaises(AssetError): self.save()
    def test_lock_blocks_concurrent_save(self):
        with library.library_lock(self.store):
            with self.assertRaises(AssetError): self.save()
    def test_character_id_path_escape_rejected(self):
        self.profile['character_id']='../escape'
        with self.assertRaises(AssetError): self.save()
        self.assertFalse((self.root/'escape').exists())


if __name__=='__main__': unittest.main()
