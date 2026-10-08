import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import media_preferences as prefs
import generated_images as images
import capabilities
from PIL import Image

TOOL = {'id': 'actual-session-tool', 'operations': ['generate', 'edit'], 'reference_images': True,
        'transparent_background': True, 'network': 'cloud', 'cost': 'subscription'}


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='媒体 适配测试 ')
        self.root = Path(self.temp.name)
        self.project = self.root/'project'
        self.config = prefs.effective(self.root, task={'preferences': {'image': {'mode': 'generate'}}})['configuration']
        self.snapshot = {'session_id': 'test-session', 'observed_at': time.time(), 'tools': [copy.deepcopy(TOOL)]}
        self.png = self.root/'实际图.png'
        im = Image.new('RGBA', (64, 48), (20, 40, 80, 255))
        im.putpixel((0, 0), (0, 0, 0, 0)); im.save(self.png)
        self.request_path = self.root/'请求.json'
        self.request_data = {'prompt': '用户选择的角色动作，保持母版身份', 'transparent_background': True,
                             'min_width': 64, 'min_height': 48, 'references': []}
        prefs.write_json(self.request_path, self.request_data)
        self.plan_path = self.root/'计划.json'
        self.receipt_path = self.root/'回执.json'
        self.output = self.project/'正式图.png'

    def tearDown(self):
        self.temp.cleanup()

    def plan(self, configuration=None, snapshot=None, grants=('cloud',)):
        plan = images.make_plan(configuration or self.config, images.request(self.request_path), snapshot or self.snapshot,
                                'test-session', self.plan_path, '用户明确生成这个动作的真实位置', grants)
        prefs.write_json(self.receipt_path, {'status': 'success', 'plan_id': plan['plan_id'], 'session_id': 'test-session',
            'tool_id': TOOL['id'], 'output_sha256': hashlib.sha256(self.png.read_bytes()).hexdigest(),
            'tool_call_reference': '实际工具调用记录/离线测试模拟，不代表真实生图'})
        return plan

    def accept(self, configuration=None, snapshot=None, session='test-session', grants=('cloud',)):
        return images.accept(configuration or self.config, self.plan_path, self.receipt_path, self.png, self.output,
                             snapshot or self.snapshot, session, '同一已确认生成任务的实际依据', grants)

    def test_task_selection_never_persists_user_defaults(self):
        result = prefs.save(self.root, {'preferences': {'image': {'mode': 'generate'}}}, 'task', '用户这次生成')
        self.assertFalse(result['persisted'])
        self.assertFalse((self.root/'media-preferences.json').exists())

    def test_scope_precedence_and_sources(self):
        prefs.save(self.root, {'preferences': {'image': {'mode': 'existing'}}}, 'user', '以后沿用原图')
        prefs.save(self.root, {'preferences': {'image': {'mode': 'off'}}}, 'project', '本片不用图片', self.project)
        resolved = prefs.effective(self.root, self.project, {'preferences': {'image': {'mode': 'generate'}}})
        self.assertEqual('generate', resolved['configuration']['preferences']['image']['mode'])
        self.assertEqual('task', resolved['sources']['image.mode'])

    def test_project_and_task_cannot_remove_user_denial(self):
        prefs.save(self.root, {'denied': ['cloud']}, 'user', '禁止云端')
        prefs.save(self.root, {'denied': []}, 'project', '只改项目偏好', self.project)
        self.assertEqual(['cloud'], prefs.effective(self.root, self.project, {'denied': []})['configuration']['denied'])

    def test_platform_change_clears_inherited_voice_binding(self):
        prefs.save(self.root, {'preferences': {'voice': {'provider': 'dubbingx', 'account': 'mine', 'voice_id': '123'}}}, 'user', '记住我的DX声音')
        result = prefs.effective(self.root, task={'preferences': {'voice': {'provider': 'volcengine'}}})
        self.assertEqual('', result['configuration']['preferences']['voice']['voice_id'])
        self.assertEqual('', result['configuration']['preferences']['voice']['account'])

    def test_saved_platform_change_clears_voice_binding(self):
        prefs.save(self.root, {'preferences': {'voice': {'provider': 'dubbingx', 'voice_id': '123'}}}, 'user', '记住DX')
        prefs.save(self.root, {'preferences': {'voice': {'provider': 'volcengine'}}}, 'user', '改默认到火山')
        self.assertEqual('', prefs.effective(self.root)['configuration']['preferences']['voice']['voice_id'])

    def test_skip_and_reset_preserve_other_capability_and_denials(self):
        prefs.save(self.root, {'preferences': {'voice': {'provider': 'dubbingx'}, 'image': {'mode': 'off'}}, 'denied': ['cloud']}, 'user', '我的偏好')
        prefs.save(self.root, {}, 'user', '重置图片选择', reset='image')
        result = prefs.effective(self.root)['configuration']
        self.assertEqual('dubbingx', result['preferences']['voice']['provider'])
        self.assertEqual('unconfigured', result['preferences']['image']['mode'])
        self.assertEqual(['cloud'], result['denied'])

    def test_revision_rejects_stale_writer(self):
        prefs.save(self.root, {}, 'user', '记住配置', expected_revision=0)
        with self.assertRaises(prefs.MediaError): prefs.save(self.root, {}, 'user', '旧修改', expected_revision=0)

    def test_show_exposes_correct_scope_revision(self):
        prefs.save(self.root, {}, 'user', '长期记住')
        prefs.save(self.root, {}, 'project', '本片选择', self.project)
        prefs.save(self.root, {}, 'user', '长期更新', expected_revision=1)
        self.assertEqual({'user': 2, 'project': 1}, prefs.effective(self.root, self.project)['revisions'])

    def test_no_credentials_or_authorizations_in_preferences(self):
        for value in [{'api_key': 'fake-key'}, {'policy': {'cloud': 'allow'}}, {'preferences': {'voice': {'token': 'fake'}}}]:
            with self.subTest(value=value), self.assertRaises(prefs.MediaError): prefs.validate(value)

    def test_duplicate_and_nonfinite_json_rejected(self):
        for raw in ['{"preferences":{},"preferences":{}}', '{"revision":NaN}']:
            file = self.root/'invalid.json'; file.write_text(raw)
            with self.assertRaises(prefs.MediaError): prefs.read_json(file)

    def test_save_requires_real_evidence_field(self):
        with self.assertRaises(prefs.MediaError): prefs.save(self.root, {}, 'user', '')

    def test_concurrent_writes_preserve_distinct_fields(self):
        files = []
        for name, data in [('voice', {'preferences': {'voice': {'provider': 'provided'}}}),
                           ('image', {'preferences': {'image': {'mode': 'existing'}}})]:
            file = self.root/(name+'.json'); prefs.write_json(file, data); files.append(file)
        runs = [subprocess.Popen([sys.executable, str(SCRIPTS/'media_preferences.py'), '--root', str(self.root),
                 'set', '--scope', 'user', '--file', str(file), '--evidence', '用户真实记住选择'],
                 stdout=subprocess.PIPE, stderr=subprocess.PIPE) for file in files]
        for run in runs:
            stdout, stderr = run.communicate(timeout=15)
            self.assertEqual(0, run.returncode, stderr.decode(errors='replace'))
        result = prefs.effective(self.root)['configuration']['preferences']
        self.assertEqual('provided', result['voice']['provider']); self.assertEqual('existing', result['image']['mode'])

    def test_status_with_empty_root_is_read_only(self):
        result = capabilities.voice_status(prefs.effective(self.root)['configuration'], self.root)
        self.assertEqual('needs_selection', result['status'])
        self.assertFalse((self.root/'media-preferences.json').exists())

    def test_existing_audio_does_not_probe_credentials(self):
        configuration = copy.deepcopy(self.config); configuration['preferences']['voice']['provider'] = 'volcengine'
        with patch.object(capabilities.os.environ, 'get', side_effect=AssertionError('must not read credentials')):
            self.assertEqual('provided', capabilities.voice_status(configuration, self.root, self.png)['status'])

    def test_cloud_denied_before_credentials(self):
        configuration = copy.deepcopy(self.config); configuration['preferences']['voice']['provider'] = 'dubbingx'; configuration['denied'] = ['cloud']
        with patch.object(capabilities.os.environ, 'get', side_effect=AssertionError('must not read credentials')):
            self.assertEqual('blocked', capabilities.voice_status(configuration, self.root)['status'])

    def test_selected_provider_only_checks_its_credential(self):
        configuration = copy.deepcopy(self.config)
        configuration['preferences']['voice'].update(provider='dubbingx', voice_id='123', account='mine')
        with patch.dict(os.environ, {'VOLC_TTS_API_KEY': 'fire-test-key'}, clear=True):
            result = capabilities.voice_status(configuration, self.root)
        self.assertFalse(result['credentials_present'])

    def test_local_credentials_are_only_presence_unverified(self):
        directory = self.root/'volcengine/mine'; directory.mkdir(parents=True)
        (directory/'credentials.dpapi.json').write_text('not decrypted or validated')
        configuration = copy.deepcopy(self.config)
        configuration['preferences']['voice'].update(provider='volcengine', voice_id='voice', account='mine', resource_id='seed-icl-2.0')
        with patch.dict(os.environ, {}, clear=True), patch.object(capabilities.shutil, 'which', return_value='installed'):
            result = capabilities.voice_status(configuration, self.root)
        self.assertEqual('configured_unverified', result['status']); self.assertFalse(result['cloud_verified'])

    def test_dubbingx_wav_does_not_require_ffmpeg(self):
        configuration = copy.deepcopy(self.config)
        configuration['preferences']['voice'].update(provider='dubbingx', voice_id='123', account='mine')
        with patch.dict(os.environ, {'DUBBINGX_API_KEY': 'test-only'}, clear=True), patch.object(capabilities.shutil, 'which', return_value=None):
            self.assertEqual('configured_unverified', capabilities.voice_status(configuration, self.root)['status'])
            self.assertEqual('needs_configuration', capabilities.voice_status(configuration, self.root, audio_format='mp3')['status'])

    def test_bad_account_cannot_escape_config_root(self):
        configuration = copy.deepcopy(self.config); configuration['preferences']['voice'].update(provider='dubbingx', account='../outside')
        with self.assertRaises(prefs.MediaError): capabilities.voice_status(configuration, self.root)

    def test_generate_without_real_tools_cannot_invent_one(self):
        self.assertEqual('needs_tool_snapshot', images.choose(self.config, None)['status'])
        self.assertEqual('unavailable', images.choose(self.config, dict(self.snapshot, tools=[]))['status'])

    def test_multiple_tools_need_selection(self):
        snapshot = dict(self.snapshot, tools=[TOOL, dict(TOOL, id='another-actual-tool')])
        self.assertEqual('needs_selection', images.choose(self.config, snapshot)['status'])

    def test_selected_missing_tool_cannot_fall_back(self):
        config = copy.deepcopy(self.config); config['preferences']['image']['tool_id'] = 'chosen-unavailable-tool'
        self.assertEqual('unavailable', images.choose(config, self.snapshot)['status'])

    def test_existing_or_off_do_not_need_snapshot(self):
        config = copy.deepcopy(self.config)
        for mode, status in [('existing', 'existing'), ('off', 'skipped')]:
            config['preferences']['image']['mode'] = mode
            self.assertEqual(status, images.choose(config, None)['status'])

    def test_cloud_and_upload_grants_must_be_recorded(self):
        with self.assertRaises(prefs.MediaError): self.plan(grants=())
        self.request_data['references'] = [str(self.png)]; prefs.write_json(self.request_path, self.request_data)
        with self.assertRaises(prefs.MediaError): self.plan()
        self.plan(grants=('cloud', 'reference_upload'))

    def test_paid_and_unknown_cost_are_explicit(self):
        for cost, grant in [('paid_api', 'paid_api'), ('unknown', 'unknown_cost')]:
            snapshot = dict(self.snapshot, tools=[dict(TOOL, cost=cost)])
            with self.assertRaises(prefs.MediaError): self.plan(snapshot=snapshot)
            plan = images.make_plan(self.config, images.request(self.request_path), snapshot, 'test-session',
                                    self.root/(cost+'.json'), '已取得本次生成和费用范围授权', ('cloud', grant))
            self.assertEqual(cost, plan['tool']['cost'])

    def test_denied_cannot_be_overridden_by_grants(self):
        config = copy.deepcopy(self.config); config['denied'] = ['cloud']
        with self.assertRaises(prefs.MediaError): self.plan(configuration=config, grants=('cloud', 'paid_api', 'reference_upload'))

    def test_tools_snapshot_other_session_or_stale_rejected(self):
        file = self.root/'tools.json'
        for snapshot in [dict(self.snapshot, session_id='other'), dict(self.snapshot, observed_at=time.time()-901)]:
            prefs.write_json(file, snapshot)
            with self.assertRaises(prefs.MediaError): images.tools_snapshot(file, 'test-session')

    def test_incompatible_transparency_tool_not_selected(self):
        snapshot = dict(self.snapshot, tools=[dict(TOOL, transparent_background=False)])
        with self.assertRaises(prefs.MediaError): self.plan(snapshot=snapshot)

    def test_success_preserves_exact_bytes_and_is_visual_pending(self):
        self.plan(); result = self.accept()
        self.assertEqual(self.png.read_bytes(), self.output.read_bytes())
        self.assertTrue(result['review_required'])
        metadata = prefs.read_json(result['metadata'])
        self.assertEqual('agent_reported_tool_call', metadata['provenance'])

    def test_reference_mutation_invalidates_plan(self):
        self.request_data['references'] = [str(self.png)]; prefs.write_json(self.request_path, self.request_data)
        self.plan(grants=('cloud', 'reference_upload'))
        Image.new('RGBA', (64, 48), 'red').save(self.png)
        with self.assertRaises(prefs.MediaError): self.accept(grants=('cloud', 'reference_upload'))

    def test_output_mutation_invalidates_receipt(self):
        self.plan(); Image.new('RGBA', (64, 48), 'red').save(self.png)
        with self.assertRaises(prefs.MediaError): self.accept()

    def test_wrong_dimensions_rejected(self):
        self.request_data['min_width'] = 65; prefs.write_json(self.request_path, self.request_data); self.plan()
        with self.assertRaises(prefs.MediaError): self.accept()

    def test_impossible_minimum_area_rejected_before_plan(self):
        self.request_data.update(min_width=16384, min_height=16384)
        prefs.write_json(self.request_path, self.request_data)
        with self.assertRaises(prefs.MediaError): self.plan()
        self.assertFalse(self.plan_path.exists())
        normalized = dict(self.request_data, references=[])
        with self.assertRaises(prefs.MediaError):
            images.make_plan(self.config, normalized, self.snapshot, 'test-session', self.plan_path,
                             '离线实际范围模拟', ('cloud',))
        self.assertFalse(self.plan_path.exists())

    def test_opaque_result_rejected_when_transparency_requested(self):
        Image.new('RGBA', (64, 48), 'red').save(self.png); self.plan()
        with self.assertRaises(prefs.MediaError): self.accept()

    def test_all_transparent_result_rejected(self):
        Image.new('RGBA', (64, 48), (0, 0, 0, 0)).save(self.png); self.plan()
        with self.assertRaises(prefs.MediaError): self.accept()

    def test_configuration_revoked_after_plan(self):
        self.plan(); config = copy.deepcopy(self.config); config['denied'] = ['image_generation']
        with self.assertRaises(prefs.MediaError): self.accept(configuration=config)

    def test_tool_change_after_plan_rejected(self):
        self.plan(); snapshot = dict(self.snapshot, tools=[dict(TOOL, cost='paid_api')])
        with self.assertRaises(prefs.MediaError): self.accept(snapshot=snapshot, grants=('cloud', 'paid_api'))

    def test_different_session_after_plan_rejected(self):
        self.plan()
        with self.assertRaises(prefs.MediaError): self.accept(session='different-session')

    def test_expired_plan_rejected(self):
        plan = self.plan(); plan['created_at'] -= 3601; prefs.write_json(self.plan_path, plan)
        with self.assertRaises(prefs.MediaError): self.accept()

    def test_changed_request_rejected(self):
        plan = self.plan(); plan['request']['prompt'] += 'changed'; prefs.write_json(self.plan_path, plan)
        with self.assertRaises(prefs.MediaError): self.accept()

    def test_missing_call_evidence_rejected(self):
        self.plan(); receipt = prefs.read_json(self.receipt_path); receipt.pop('tool_call_reference'); prefs.write_json(self.receipt_path, receipt)
        with self.assertRaises(prefs.MediaError): self.accept()

    def test_no_overwrite_image_or_sidecar(self):
        self.plan(); self.accept()
        original = self.output.read_bytes()
        with self.assertRaises(prefs.MediaError): self.accept()
        self.assertEqual(original, self.output.read_bytes())

    def test_output_extension_matches_encoding(self):
        self.plan(); self.output = self.project/'incorrect.jpg'
        with self.assertRaises(prefs.MediaError): self.accept()

    def test_reference_path_relative_to_request_file(self):
        self.request_data['references'] = [self.png.name]; prefs.write_json(self.request_path, self.request_data)
        self.assertEqual(str(self.png.resolve()), images.request(self.request_path)['references'][0]['path'])

    def test_plan_not_overwritten(self):
        self.plan(); original = self.plan_path.read_bytes()
        with self.assertRaises(FileExistsError): self.plan()
        self.assertEqual(original, self.plan_path.read_bytes())


if __name__ == '__main__': unittest.main()
