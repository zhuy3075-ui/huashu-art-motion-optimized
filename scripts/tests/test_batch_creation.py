import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import batch_creation as batch


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='批量 创作 ')
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / '系列 工程'
        self.manifest = {'schema_version': 1, 'name': '系列测试', 'defaults': {'voice': {'provider': 'off'}},
                         'jobs': [{'id': 'one', 'topic': '选题一'}, {'id': 'two', 'topic': '选题二'}]}
        batch.initialize(self.project, self.manifest)

    def state(self):
        return batch.load(self.project)

    def do(self, command, **args):
        return batch.mutate(self.project, command, self.state()['revision'], **args)

    def start(self, ident, stage):
        self.do('start', job_id=ident, stage=stage, reference='本任务/制作记录.json')
        return batch.lookup(self.state(), ident)['operation']['token']

    def finish(self, ident, stage, token=None):
        token = token or self.start(ident, stage)
        root = batch.job_root(self.project, ident)
        name = f'{stage}-{self.state()["revision"]}.md'
        (root / name).write_text('测试实际产物：' + stage, encoding='utf-8')
        self.do('finish', job_id=ident, token=token, artifact=name, tool_reference='离线fixture生成记录')
        return batch.lookup(self.state(), ident)['stages'][stage]

    def confirmation(self, ident, stage):
        record = batch.lookup(self.state(), ident)['stages'][stage]
        return {'job_id': ident, 'stage': stage, 'sha256': record['sha256']}

    def confirm(self, ident, stage):
        self.do('confirm', items=[self.confirmation(ident, stage)], evidence='确认这个实际版本', source='测试对话#1')

    def through(self, stage):
        for name in batch.STAGES[:batch.STAGES.index(stage) + 1]:
            self.finish('one', name)
            self.confirm('one', name)

    def test_two_jobs_have_independent_directories(self):
        rows = batch.summary(self.project, self.state())['jobs']
        self.assertNotEqual(rows[0]['directory'], rows[1]['directory'])
        self.assertTrue(all(Path(row['directory']).is_dir() for row in rows))

    def test_cannot_initialize_over_existing_queue(self):
        with self.assertRaises(batch.BatchError): batch.initialize(self.project, self.manifest)
        self.assertEqual(1, self.state()['revision'])

    def test_rejects_ids_and_secret_fields(self):
        for invalid in ('../escape', 'CON', 'con', 'one/../../', 'nul', 'one.'):
            value = copy.deepcopy(self.manifest)
            value['jobs'][0]['id'] = invalid
            with self.assertRaises(batch.BatchError): batch.manifest(value)
        with self.assertRaises(batch.BatchError): batch.settings({'api_key': 'never-store-this'})
        with self.assertRaises(batch.BatchError): batch.settings({'voice': {'api_key': 'never-store-this'}})

    def test_rejects_duplicate_job(self):
        value = copy.deepcopy(self.manifest)
        value['jobs'][1]['id'] = 'one'
        with self.assertRaises(batch.BatchError): batch.manifest(value)

    def test_rejects_empty_and_oversize_batch(self):
        for jobs in ([], [{}] * 101):
            with self.assertRaises(batch.BatchError): batch.manifest({**self.manifest, 'jobs': jobs})

    def test_platform_override_clears_old_binding(self):
        original = batch.merge_settings(batch.DEFAULTS, {'voice': {'provider': 'volcengine', 'voice_id': 'fire-id',
                                                               'account': 'fire-account', 'resource_id': 'seed-icl-2.0'}})
        changed = batch.merge_settings(original, {'voice': {'provider': 'dubbingx'}})
        self.assertEqual('', changed['voice']['voice_id'])
        self.assertEqual('', changed['voice']['account'])
        self.assertEqual('', changed['voice']['resource_id'])
        self.assertEqual('fire-id', original['voice']['voice_id'])

    def test_next_read_only_and_first_stage(self):
        before = batch.state_path(self.project).read_bytes()
        item = batch.next_job(self.project, self.state())
        self.assertEqual(('one', 'brief', 'create'), (item['id'], item['stage'], item['action']))
        self.assertEqual(before, batch.state_path(self.project).read_bytes())

    def test_cannot_skip_unconfirmed_text(self):
        self.through('outline')
        self.finish('one', 'script')
        with self.assertRaises(batch.BatchError): self.start('one', 'storyboard')
        with self.assertRaises(batch.BatchError): self.start('one', 'film')

    def test_finished_draft_needs_actual_confirmation(self):
        self.finish('one', 'brief')
        item = batch.summary(self.project, self.state())['jobs'][0]
        self.assertEqual('await_confirmation', item['action'])
        with self.assertRaises(batch.BatchError):
            self.do('confirm', items=[self.confirmation('one', 'brief')], evidence='', source='对话')
        with self.assertRaises(batch.BatchError):
            self.do('confirm', items=[self.confirmation('one', 'brief')], evidence='确认', source='')

    def test_can_draft_second_job_while_first_waits(self):
        self.finish('one', 'brief')
        self.assertEqual('two', batch.next_job(self.project, self.state())['id'])

    def test_atomic_batch_confirmation(self):
        self.finish('one', 'brief'); self.finish('two', 'brief')
        items = [self.confirmation('one', 'brief'), self.confirmation('two', 'brief')]
        items[1]['sha256'] = '0' * 64
        before = batch.state_path(self.project).read_bytes()
        with self.assertRaises(batch.BatchError): self.do('confirm', items=items, evidence='确认这两项', source='对话#2')
        self.assertEqual(before, batch.state_path(self.project).read_bytes())
        items[1] = self.confirmation('two', 'brief')
        self.do('confirm', items=items, evidence='确认这两项', source='对话#2')
        self.assertEqual('outline', batch.next_job(self.project, self.state())['stage'])

    def test_duplicate_batch_confirmation_rejected(self):
        self.finish('one', 'brief')
        item = self.confirmation('one', 'brief')
        with self.assertRaises(batch.BatchError): self.do('confirm', items=[item, item], evidence='确认', source='对话')

    def test_wrong_hash_not_confirmed(self):
        self.finish('one', 'brief')
        item = self.confirmation('one', 'brief'); item['sha256'] = 'f' * 64
        with self.assertRaises(batch.BatchError): self.do('confirm', items=[item], evidence='确认', source='对话')

    def test_confirmed_file_changed_blocks_future(self):
        self.through('script')
        job = batch.lookup(self.state(), 'one')
        path = batch.artifact_path(self.project, job, job['stages']['brief']['artifact'])
        path.write_text('修改了需求', encoding='utf-8')
        self.assertEqual('stale_artifact', batch.current(self.project, job)['action'])
        with self.assertRaises(batch.BatchError): self.start('one', 'storyboard')

    def test_missing_pending_file_cannot_confirm(self):
        record = self.finish('one', 'brief')
        (batch.job_root(self.project, 'one') / record['artifact']).unlink()
        with self.assertRaises(batch.BatchError): self.confirm('one', 'brief')

    def test_artifact_outside_or_empty_rejected(self):
        token = self.start('one', 'brief')
        outside = self.project / 'outside.md'; outside.write_text('外部文件', encoding='utf-8')
        root = batch.job_root(self.project, 'one'); (root / 'empty.md').touch()
        for name in (str(outside), '../../../../outside.md', 'empty.md', 'missing.md'):
            with self.assertRaises(batch.BatchError):
                self.do('finish', job_id='one', token=token, artifact=name, tool_reference='实际记录')

    def test_single_running_operation_and_recovery(self):
        token = self.start('one', 'brief')
        with self.assertRaises(batch.BatchError): self.start('two', 'brief')
        item = batch.next_job(self.project, self.state())
        self.assertEqual('recover_operation', item['action'])
        self.assertEqual(token, item['operation']['token'])

    def test_wrong_token_does_not_finish(self):
        self.start('one', 'brief')
        with self.assertRaises(batch.BatchError):
            self.do('fail', job_id='one', token='wrong', evidence='失败')

    def test_failed_operation_preserved_other_job_can_continue(self):
        token = self.start('one', 'brief')
        self.do('fail', job_id='one', token=token, evidence='原平台查询已返回确定Failed终态', terminal=True)
        self.assertEqual('two', batch.next_job(self.project, self.state())['id'])
        with self.assertRaises(batch.BatchError): self.start('one', 'brief')
        self.assertEqual(token, batch.lookup(self.state(), 'one')['operation']['token'])

    def test_unknown_failure_keeps_serial_slot(self):
        token = self.start('one', 'brief')
        self.do('fail', job_id='one', token=token, evidence='HTTP超时，平台状态未知')
        self.assertEqual('one', batch.next_job(self.project, self.state())['id'])
        self.assertEqual('recover_operation', batch.next_job(self.project, self.state())['action'])
        with self.assertRaises(batch.BatchError): self.start('two', 'brief')

    def test_resume_original_failed_operation_without_new_submit(self):
        token = self.start('one', 'brief')
        self.do('fail', job_id='one', token=token, evidence='结果尚未下载')
        self.finish('one', 'brief', token)
        self.assertEqual('await_confirmation', batch.summary(self.project, self.state())['jobs'][0]['action'])

    def test_resolution_requires_evidence_and_archives_operation(self):
        token = self.start('one', 'brief')
        with self.assertRaises(batch.BatchError): self.do('resolve', job_id='one', token=token, evidence='')
        self.do('resolve', job_id='one', token=token, evidence='已查原任务未提交；用户要求重新制作')
        job = batch.lookup(self.state(), 'one')
        self.assertIsNone(job['operation'])
        self.assertEqual(token, job['history'][0]['operation']['token'])

    def test_stale_revision_rejected(self):
        self.start('one', 'brief')
        with self.assertRaises(batch.BatchError):
            batch.mutate(self.project, 'start', 1, job_id='two', stage='brief', reference='记录')

    def test_changed_predecessor_during_operation_cannot_finish(self):
        self.through('brief'); token = self.start('one', 'outline')
        job = batch.lookup(self.state(), 'one')
        batch.artifact_path(self.project, job, job['stages']['brief']['artifact']).write_text('新简报', encoding='utf-8')
        with self.assertRaises(batch.BatchError): self.finish('one', 'outline', token)

    def test_style_change_keeps_text_and_invalidates_visuals(self):
        self.through('film')
        self.do('settings', job_id='one', settings={'style_ref': 'paper@2'}, evidence='改为纸纹画风')
        job = batch.lookup(self.state(), 'one')
        self.assertEqual({'brief', 'outline', 'script'}, set(job['stages']))
        self.assertEqual('storyboard', batch.current(self.project, job)['stage'])
        self.assertTrue(any('record' in row and row['stage'] == 'film' for row in job['history']))

    def test_voice_change_keeps_storyboard_and_requires_new_pilot(self):
        self.through('film')
        self.do('settings', job_id='one', settings={'voice': {'provider': 'dubbingx'}}, evidence='改配音平台')
        job = batch.lookup(self.state(), 'one')
        self.assertEqual('voice', batch.current(self.project, job)['stage'])
        self.assertIn('storyboard', job['stages'])

    def test_unconfigured_voice_cannot_start_pilot(self):
        self.through('voice')
        self.do('settings', job_id='one', settings={'voice': {'provider': 'unconfigured'}}, evidence='还未选声音')
        self.finish('one', 'voice'); self.confirm('one', 'voice')
        with self.assertRaises(batch.BatchError): self.start('one', 'pilot')

    def test_new_voice_requires_platform_id_and_account(self):
        self.through('voice')
        self.do('settings', job_id='one', settings={'voice': {'provider': 'volcengine', 'voice_id': 'real-id'}}, evidence='选火山')
        self.finish('one', 'voice'); self.confirm('one', 'voice')
        with self.assertRaises(batch.BatchError): self.start('one', 'pilot')

    def test_cannot_invalidate_running_operation(self):
        self.start('one', 'brief')
        with self.assertRaises(batch.BatchError): self.do('invalidate', job_id='one', stage='brief', evidence='改简报')

    def test_revision_after_invalidate_retains_previous_artifact(self):
        record = self.finish('one', 'brief'); self.confirm('one', 'brief')
        self.do('invalidate', job_id='one', stage='brief', evidence='修改需求')
        self.assertTrue((batch.job_root(self.project, 'one') / record['artifact']).is_file())
        self.assertEqual(2, self.finish('one', 'brief')['version'])

    def test_reused_source_path_retains_immutable_old_snapshot(self):
        record = self.finish('one', 'brief'); self.confirm('one', 'brief')
        root = batch.job_root(self.project, 'one')
        old_bytes = (root / record['snapshot']).read_bytes()
        self.do('invalidate', job_id='one', stage='brief', evidence='修改需求')
        token = self.start('one', 'brief')
        (root / record['artifact']).write_text('新版简报', encoding='utf-8')
        self.do('finish', job_id='one', token=token, artifact=record['artifact'], tool_reference='新制作记录')
        self.assertEqual(old_bytes, (root / record['snapshot']).read_bytes())
        newest = batch.lookup(self.state(), 'one')['stages']['brief']
        self.assertNotEqual(record['snapshot'], newest['snapshot'])
        self.assertNotEqual(record['sha256'], newest['sha256'])

    def test_all_stages_confirmed_completed(self):
        self.through('film')
        self.assertEqual('completed', batch.summary(self.project, self.state())['jobs'][0]['action'])

    def test_cli_paths_with_spaces_and_json_output(self):
        result = subprocess.run([sys.executable, str(SCRIPTS/'batch_creation.py'), '--project', str(self.project), 'next'],
                                capture_output=True, encoding='utf-8', env={**__import__('os').environ, 'PYTHONUTF8': '1'})
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('brief', json.loads(result.stdout)['stage'])


if __name__ == '__main__':
    unittest.main()
