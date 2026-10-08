"""Behavior checks for persisted styles; every write uses a temporary root."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'style_library.py'
SPEC = importlib.util.spec_from_file_location('style_library', SCRIPT)
LIB = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LIB)
TEMPLATE = SCRIPT.parents[1] / 'assets' / 'style-dna.template.json'


def defined_profile(style_id='paper-style', name='纸纹手绘', aliases=None):
    profile = LIB.read_json(TEMPLATE)
    profile.update(style_id=style_id, name=name, aliases=aliases or ['我的手绘'], summary='单元测试中的用户定义风格，不是真实识别结果。')
    profile['origin']['references'] = [{'id': 'R1', 'kind': 'text', 'locator': '测试用户原话',
                                      'access': 'user_statement', 'notes': '合成测试输入'}]
    profile['evidence'] = [{'id': 'E1', 'source_id': 'R1', 'location': '测试对话第1句',
                            'observation': '用户定义米色平涂、简化形状、留白构图。',
                            'method': 'user_statement', 'confidence': 'high'}]
    for dimension in ('identity', 'palette', 'shape_language', 'composition'):
        profile['dna'][dimension] = {
            'status': 'user_defined', 'description': f'测试用户定义的{dimension}，未做视觉测量。',
            'rules': [{'feature': dimension, 'value': '米色平涂、简化形状或主体留白的具体目标规则',
                       'unit': '', 'tolerance': '可按新主题适配', 'priority': 'prefer', 'evidence_ids': ['E1']}],
            'avoid': [], 'evidence_ids': ['E1'], 'confidence': 'high',
        }
    profile['prompts'].update(style_only='米色背景，平涂简化形状与明确留白。',
                              image_template='{subject}，{scene}，米色平涂，留白构图。')
    profile['approval'] = {'state': 'confirmed', 'statement': '测试：确认这份定义并保存。', 'reference': '测试对话第2句'}
    profile['reuse']['locked_dimensions'] = ['palette']
    return profile


class StyleLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='风格 [测试] ')
        self.directory = Path(self.temporary.name)
        self.root = self.directory / "配置 '库'"

    def tearDown(self):
        self.temporary.cleanup()

    def cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), '--root', str(self.root), *args],
                              capture_output=True, text=True, encoding='utf-8',
                              env=dict(os.environ, PYTHONUTF8='1'), timeout=15)

    def input_file(self, profile):
        path = self.directory / '风格草稿.json'
        path.write_text(json.dumps(profile, ensure_ascii=False), encoding='utf-8')
        return path

    def test_list_without_library_is_read_only(self):
        result = self.cli('list')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['local_styles'], [])
        self.assertFalse(self.root.exists())

    def test_template_is_draft_and_not_confirmable_without_core(self):
        profile = LIB.read_json(TEMPLATE)
        LIB.validate_profile(profile)
        profile['approval'].update(state='confirmed', statement='确认', reference='测试')
        with self.assertRaises(LIB.StyleError):
            LIB.validate_profile(profile)

    def test_chinese_name_alias_and_versions_survive_roundtrip(self):
        initial = defined_profile()
        result = self.cli('save', '--file', str(self.input_file(initial)))
        self.assertEqual(result.returncode, 0, result.stderr)
        first_path = self.root / 'styles/paper-style/v1.json'
        original_bytes = first_path.read_bytes()
        updated = LIB.resolve(self.root, '我的手绘')
        updated['dna']['palette']['description'] = '第二版：用户定义更浅的米色。'
        updated['approval'].update(statement='测试：确认新版本。', reference='测试对话第3句')
        second = LIB.save_profile(self.root, updated, 1)
        self.assertEqual(second['revision'], 2)
        self.assertEqual(first_path.read_bytes(), original_bytes)
        self.assertEqual(LIB.resolve(self.root, '纸纹手绘@1')['dna']['palette']['description'], initial['dna']['palette']['description'])
        self.assertEqual(LIB.resolve(self.root, '我的手绘')['revision'], 2)
        shown = self.cli('show', '我的手绘@1')
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual(json.loads(shown.stdout)['revision'], 1)

    def test_update_requires_current_base_and_cannot_overwrite(self):
        profile = defined_profile()
        LIB.save_profile(self.root, profile)
        for base in (None, 0, 2, True):
            with self.subTest(base=base), self.assertRaises(LIB.StyleError):
                LIB.save_profile(self.root, profile, base)
        LIB.save_profile(self.root, profile, 1)
        with self.assertRaises(LIB.StyleError):
            LIB.save_profile(self.root, profile, 1)
        self.assertEqual(len(LIB.versions(self.root, 'paper-style')), 2)

    def test_name_alias_id_collisions_do_not_create_styles(self):
        LIB.save_profile(self.root, defined_profile(aliases=['Paper']))
        for name, alias in [('另一个', 'paper'), ('纸纹手绘', 'new'), ('paper-style', 'new')]:
            with self.subTest(name=name, alias=alias), self.assertRaises(LIB.StyleError):
                LIB.save_profile(self.root, defined_profile('second-style', name, [alias]))
        self.assertEqual(set(LIB.catalog(self.root)), {'paper-style'})
        self.assertFalse((self.root / 'styles/second-style').exists())

    def test_invalid_ids_and_schema_never_write(self):
        for style_id in ('../escape', '/absolute', 'A-style', 'a/b', '.', 'a' * 65):
            with self.subTest(style_id=style_id), self.assertRaises(LIB.StyleError):
                LIB.save_profile(self.root, defined_profile(style_id))
        bad = defined_profile()
        del bad['dna']['materials']
        with self.assertRaises(LIB.StyleError):
            LIB.save_profile(self.root, bad)
        self.assertFalse(self.root.exists())

    def test_duplicate_json_keys_and_nan_are_rejected(self):
        for content in ('{"name":"a","name":"b"}', '{"value":NaN}'):
            path = self.directory / 'bad.json'
            path.write_text(content, encoding='utf-8')
            with self.assertRaises(LIB.StyleError):
                LIB.read_json(path)

    def test_malformed_enum_reports_cli_error_without_traceback(self):
        profile = defined_profile()
        profile['dna']['palette']['status'] = ['observed']
        result = self.cli('validate', '--file', str(self.input_file(profile)))
        self.assertEqual(result.returncode, 2)
        self.assertIn('palette.status', result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_still_image_cannot_prove_motion_or_transitions(self):
        for dimension in ('motion', 'transitions'):
            profile = defined_profile()
            profile['origin']['references'].append({'id': 'R2', 'kind': 'image', 'locator': '测试关键帧.png',
                                                    'access': 'read', 'notes': '仅静帧测试依据'})
            profile['evidence'].append({'id': 'E2', 'source_id': 'R2', 'location': 'frame1',
                                        'observation': '只能看到姿势', 'method': 'visual', 'confidence': 'medium'})
            block = copy.deepcopy(profile['dna']['identity'])
            block.update(status='observed', evidence_ids=['E2'])
            block['rules'][0]['evidence_ids'] = ['E2']
            profile['dna'][dimension] = block
            with self.subTest(dimension=dimension), self.assertRaises(LIB.StyleError):
                LIB.validate_profile(profile)

    def test_video_evidence_and_user_defined_motion_are_distinct(self):
        profile = defined_profile()
        profile['dna']['motion'] = copy.deepcopy(profile['dna']['identity'])
        LIB.validate_profile(profile)  # Authored motion is possible without observed video.
        profile['dna']['motion']['status'] = 'observed'
        with self.assertRaises(LIB.StyleError):
            LIB.validate_profile(profile)
        profile['origin']['references'][0].update(kind='sequence', access='read')
        profile['evidence'][0].update(method='visual')
        for dimension in ('identity', 'palette', 'shape_language', 'composition', 'motion'):
            profile['dna'][dimension]['status'] = 'observed'
        LIB.validate_profile(profile)

    def test_unknown_evidence_and_unavailable_source_rejected(self):
        profile = defined_profile()
        profile['dna']['palette']['evidence_ids'] = ['missing']
        with self.assertRaises(LIB.StyleError):
            LIB.validate_profile(profile)
        profile = defined_profile()
        profile['origin']['references'][0]['access'] = 'unavailable'
        with self.assertRaises(LIB.StyleError):
            LIB.validate_profile(profile)

    def test_unknown_motion_cannot_leak_into_prompt_or_lock(self):
        for field in ('prompt', 'lock'):
            profile = defined_profile()
            if field == 'prompt':
                profile['prompts']['motion_template'] = '相机持续推进'
            else:
                profile['reuse']['locked_dimensions'].append('motion')
            with self.subTest(field=field), self.assertRaises(LIB.StyleError):
                LIB.validate_profile(profile)

    def test_confirmation_and_visual_validation_are_separate(self):
        saved = LIB.save_profile(self.root, defined_profile())
        self.assertEqual(saved['approval']['state'], 'confirmed')
        self.assertEqual(saved['validation']['state'], 'untested')
        saved['validation']['state'] = 'motion_tested'
        with self.assertRaises(LIB.StyleError):
            LIB.validate_profile(saved)

    def test_changed_rules_cannot_inherit_previous_confirmation(self):
        prior = LIB.save_profile(self.root, defined_profile())
        changed = copy.deepcopy(prior)
        changed['dna']['palette']['rules'][0]['value'] = '改成浅蓝'
        with self.assertRaises(LIB.StyleError):
            LIB.save_profile(self.root, changed, 1)
        changed['approval'] = {'state': 'draft', 'statement': '', 'reference': ''}
        saved = LIB.save_profile(self.root, changed, 1)
        self.assertEqual(saved['approval']['state'], 'draft')
        self.assertEqual(LIB.resolve(self.root, 'paper-style@1'), prior)

    def test_changed_rules_cannot_inherit_previous_visual_test(self):
        profile = defined_profile()
        profile['validation'] = {'state': 'static_tested', 'samples': ['v1/sample.png'],
                                 'checks': [{'dimension': 'palette', 'result': 'pass', 'evidence': 'v1/sample.png', 'notes': '测试样稿'}],
                                 'known_gaps': []}
        LIB.save_profile(self.root, profile)
        profile['dna']['palette']['rules'][0]['value'] = '新规则'
        profile['approval']['reference'] = '测试新确认位置'
        with self.assertRaises(LIB.StyleError):
            LIB.save_profile(self.root, profile, 1)
        profile['validation'] = {'state': 'untested', 'samples': [], 'checks': [], 'known_gaps': ['新版尚未复验']}
        LIB.save_profile(self.root, profile, 1)
        self.assertEqual(LIB.resolve(self.root, 'paper-style')['validation']['state'], 'untested')

    def test_metadata_only_edit_preserves_valid_confirmation(self):
        profile = defined_profile()
        LIB.save_profile(self.root, profile)
        profile['name'] = '重新命名的风格'
        LIB.save_profile(self.root, profile, 1)
        self.assertEqual(LIB.resolve(self.root, '重新命名的风格')['approval']['state'], 'confirmed')

    def test_draft_cannot_retain_confirmation_or_reactivate_historical_approval(self):
        first = LIB.save_profile(self.root, defined_profile())
        changed = copy.deepcopy(first)
        changed['dna']['palette']['rules'][0]['value'] = '第二版规则'
        changed['approval']['state'] = 'draft'
        with self.assertRaises(LIB.StyleError):
            LIB.save_profile(self.root, changed, 1)
        changed['approval'] = {'state': 'draft', 'statement': '', 'reference': ''}
        LIB.save_profile(self.root, changed, 1)
        changed['approval'] = copy.deepcopy(first['approval'])
        with self.assertRaises(LIB.StyleError):
            LIB.save_profile(self.root, changed, 2)
        changed['approval']['reference'] = '测试第三轮新的确认'
        final = LIB.save_profile(self.root, changed, 2)
        self.assertEqual(final['revision'], 3)
        self.assertEqual(final['approval']['state'], 'confirmed')

    def test_sample_shuffle_or_append_cannot_reuse_old_tests(self):
        profile = defined_profile()
        profile['validation'] = {'state': 'static_tested', 'samples': ['v1.png', 'v1-b.png'],
                                 'checks': [{'dimension': 'palette', 'result': 'pass', 'evidence': 'v1.png', 'notes': ''}],
                                 'known_gaps': []}
        LIB.save_profile(self.root, profile)
        profile['dna']['palette']['rules'][0]['value'] = '新色板'
        profile['approval']['reference'] = '新确认'
        for samples in (['v1-b.png', 'v1.png'], ['v1.png', 'v1-b.png', 'new.png'], ['new.png']):
            candidate = copy.deepcopy(profile)
            candidate['validation']['samples'] = samples
            with self.subTest(samples=samples), self.assertRaises(LIB.StyleError):
                LIB.save_profile(self.root, candidate, 1)
        profile['validation']['samples'] = ['v2.png']
        profile['validation']['checks'][0]['evidence'] = 'v2.png#frame=0'
        LIB.save_profile(self.root, profile, 1)
        self.assertEqual(LIB.resolve(self.root, 'paper-style')['validation']['state'], 'static_tested')

    def test_changed_dimension_cannot_be_tested_only_with_unrelated_check(self):
        profile = defined_profile()
        LIB.save_profile(self.root, profile)
        profile['dna']['palette']['rules'][0]['value'] = '修改色彩'
        profile['approval']['reference'] = '新确认'
        profile['validation'] = {'state': 'static_tested', 'samples': ['new.png'],
                                 'checks': [{'dimension': 'composition', 'result': 'pass', 'evidence': 'new.png', 'notes': ''}],
                                 'known_gaps': []}
        with self.assertRaises(LIB.StyleError):
            LIB.save_profile(self.root, profile, 1)

    def test_draft_transition_cannot_restore_test_of_different_appearance(self):
        first = defined_profile()
        first['validation'] = {'state': 'static_tested', 'samples': ['v1.png'],
                               'checks': [{'dimension': 'palette', 'result': 'pass', 'evidence': 'v1.png', 'notes': ''}],
                               'known_gaps': []}
        LIB.save_profile(self.root, first)
        changed = copy.deepcopy(first)
        changed['dna']['palette']['rules'][0]['value'] = '色板B'
        changed['approval'] = {'state': 'draft', 'statement': '', 'reference': ''}
        changed['validation'] = {'state': 'untested', 'samples': [], 'checks': [], 'known_gaps': ['等待复验']}
        LIB.save_profile(self.root, changed, 1)
        changed['approval'] = {'state': 'confirmed', 'statement': '批准B', 'reference': '真实新的确认位置'}
        changed['validation'] = copy.deepcopy(first['validation'])
        with self.assertRaises(LIB.StyleError):
            LIB.save_profile(self.root, changed, 2)
        changed['validation']['samples'] = ['v3-B.png']
        changed['validation']['checks'][0]['evidence'] = 'v3-B.png'
        saved = LIB.save_profile(self.root, changed, 2)
        self.assertEqual(saved['revision'], 3)

    def test_draft_transition_retests_dimensions_changed_since_last_test(self):
        first = defined_profile()
        first['validation'] = {'state': 'static_tested', 'samples': ['A.png'],
                               'checks': [{'dimension': 'palette', 'result': 'pass', 'evidence': 'A.png', 'notes': ''}],
                               'known_gaps': []}
        LIB.save_profile(self.root, first)
        changed = copy.deepcopy(first)
        changed['dna']['palette']['rules'][0]['value'] = '色板B'
        changed['approval'] = {'state': 'draft', 'statement': '', 'reference': ''}
        changed['validation'] = {'state': 'untested', 'samples': [], 'checks': [], 'known_gaps': []}
        LIB.save_profile(self.root, changed, 1)
        changed['approval'] = {'state': 'confirmed', 'statement': '确认', 'reference': '新位置'}
        changed['validation'] = {'state': 'static_tested', 'samples': ['B.png'],
                                 'checks': [{'dimension': 'composition', 'result': 'pass', 'evidence': 'B.png', 'notes': ''}],
                                 'known_gaps': []}
        with self.assertRaises(LIB.StyleError):
            LIB.save_profile(self.root, changed, 2)

    def test_history_tests_can_be_reused_for_identical_appearance(self):
        first = defined_profile()
        first['validation'] = {'state': 'static_tested', 'samples': ['same-style.png'],
                               'checks': [{'dimension': 'palette', 'result': 'pass', 'evidence': 'same-style.png', 'notes': ''}],
                               'known_gaps': []}
        LIB.save_profile(self.root, first)
        draft = copy.deepcopy(first)
        draft['approval'] = {'state': 'draft', 'statement': '', 'reference': ''}
        draft['validation'] = {'state': 'untested', 'samples': [], 'checks': [], 'known_gaps': []}
        LIB.save_profile(self.root, draft, 1)
        draft['name'] = '相同视觉规则的新名称'
        draft['approval'] = {'state': 'confirmed', 'statement': '重新采用', 'reference': '当前确认位置'}
        draft['validation'] = copy.deepcopy(first['validation'])
        saved = LIB.save_profile(self.root, draft, 2)
        self.assertEqual(saved['validation']['state'], 'static_tested')

    def test_still_inference_cannot_become_executable_motion(self):
        profile = defined_profile()
        block = copy.deepcopy(profile['dna']['identity'])
        block['status'] = 'inferred'
        profile['dna']['motion'] = block
        profile['prompts']['motion_template'] = '镜头匀速推进2秒'
        with self.assertRaises(LIB.StyleError):
            LIB.validate_profile(profile)

    def test_observed_cannot_be_supported_only_by_user_words(self):
        profile = defined_profile()
        profile['dna']['identity']['status'] = 'observed'
        with self.assertRaises(LIB.StyleError):
            LIB.validate_profile(profile)

    def test_each_motion_rule_needs_its_own_supported_evidence(self):
        profile = defined_profile()
        profile['origin']['references'].append({'id': 'R2', 'kind': 'image', 'locator': 'still.png', 'access': 'read', 'notes': ''})
        profile['evidence'].append({'id': 'E2', 'source_id': 'R2', 'location': 'frame', 'observation': '只能看到姿态', 'method': 'visual', 'confidence': 'low'})
        block = copy.deepcopy(profile['dna']['identity'])
        block['evidence_ids'].append('E2')
        unsupported = copy.deepcopy(block['rules'][0])
        unsupported.update(value='镜头推进2秒', evidence_ids=['E2'])
        block['rules'].append(unsupported)
        profile['dna']['motion'] = block
        with self.assertRaises(LIB.StyleError):
            LIB.validate_profile(profile)

    def test_atomic_write_failure_preserves_previous_and_catalog(self):
        LIB.save_profile(self.root, defined_profile())
        prior = (self.root / 'styles/paper-style/v1.json').read_bytes()
        with patch.object(LIB.os, 'replace', side_effect=OSError('模拟磁盘写入失败')):
            with self.assertRaises(OSError):
                LIB.save_profile(self.root, defined_profile(), 1)
            with self.assertRaises(OSError):
                LIB.save_profile(self.root, defined_profile('new-style', '新风格', ['New']))
        self.assertEqual((self.root / 'styles/paper-style/v1.json').read_bytes(), prior)
        self.assertFalse((self.root / 'styles/new-style').exists())
        self.assertEqual(set(LIB.catalog(self.root)), {'paper-style'})
        self.assertFalse((self.root / '.write.lock').exists())
        self.assertFalse(list(self.root.glob('.style-*.tmp')))

    def test_existing_write_lock_not_removed(self):
        LIB.initialize(self.root)
        lock = self.root / '.write.lock'
        lock.write_text('another writer', encoding='utf-8')
        with self.assertRaises(LIB.StyleError):
            LIB.save_profile(self.root, defined_profile())
        self.assertEqual(lock.read_text(encoding='utf-8'), 'another writer')

    def test_latest_draft_is_visible_without_fallback(self):
        profile = defined_profile()
        LIB.save_profile(self.root, profile)
        profile['approval'] = {'state': 'draft', 'statement': '', 'reference': ''}
        LIB.save_profile(self.root, profile, 1)
        self.assertEqual(LIB.resolve(self.root, '我的手绘')['approval']['state'], 'draft')
        self.assertEqual(LIB.resolve(self.root, '我的手绘@1')['approval']['state'], 'confirmed')

    def test_missing_name_version_and_corrupt_library_fail_loudly(self):
        LIB.save_profile(self.root, defined_profile())
        for selector in ('不存在', '纸纹手绘@5', '纸纹手绘@x', '纸纹手绘@0'):
            with self.subTest(selector=selector), self.assertRaises(LIB.StyleError):
                LIB.resolve(self.root, selector)
        (self.root / 'styles/paper-style/v1.json').write_text('{bad json', encoding='utf-8')
        result = self.cli('list')
        self.assertEqual(result.returncode, 2)
        self.assertIn('v1.json', result.stderr)

    def test_default_root_respects_codex_home(self):
        with patch.dict(os.environ, {'CODEX_HOME': str(self.directory)}):
            self.assertEqual(LIB.default_root(), (self.directory / 'config/huashu-art-motion').resolve())

    def test_builtins_are_separate_and_do_not_create_personal_styles(self):
        result = self.cli('list', '--include-builtins')
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertTrue(data['builtin_recipes'])
        self.assertEqual(data['local_styles'], [])
        self.assertTrue(all(item['id'].startswith('recipe:') for item in data['builtin_recipes']))
        self.assertFalse(self.root.exists())


if __name__ == '__main__':
    unittest.main()
