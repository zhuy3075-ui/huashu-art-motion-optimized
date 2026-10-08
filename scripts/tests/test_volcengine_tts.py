import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('volcengine_tts', Path(__file__).resolve().parents[1]/'volcengine_tts.py')
tts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tts)


def request():
    return {'speaker':'S_test_voice', 'resource_id':'seed-icl-2.0',
            'segments':[{'text':'你好。'},{'text':'世界！'}],
            'audio_params':{'format':'mp3','sample_rate':24000,'speech_rate':0,
                            'loudness_rate':0,'enable_timestamp':True}}


def result():
    return {'task_status':2,'audio_url':'https://example.com/test.mp3',
            'sentences':[{'text':'你好。世界！','startTime':0.2,'endTime':1.6,
                          'words':[{'word':c,'startTime':s,'endTime':e,'confidence':0.9}
                                   for c,s,e in [('你',.2,.5),('好。',.5,.8),('世',.8,1.2),('界！',1.2,1.6)]]}]}


class FakeClient:
    key, timeout = 'offline-test-key', 30
    def __init__(self):
        self.calls=[]
        self.response=result()
        self.submit_error=False
    def post(self, action, payload, resource):
        self.calls.append((action,copy.deepcopy(payload),resource))
        if action=='submit':
            self.task_id=payload['unique_id']
            if self.submit_error:
                raise tts.TtsError('unknown')
            return {'task_id':self.task_id,'task_status':1}
        return {**copy.deepcopy(self.response),'task_id':payload['task_id']}


class VolcTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.directory=tts.account_dir(self.root,'test',FakeClient.key)
        tts.import_voices(self.directory,[{'speaker':'S_test_voice','name':'测试声音',
                              'resource_id':'seed-icl-2.0','source':'custom'}])
        self.client=FakeClient()
        self.job=self.root/'job.json'
        self.audio=self.root/'audio.mp3'
    def tearDown(self):
        self.temp.cleanup()
    def submit(self):
        return tts.submit(self.client,request(),self.job,self.directory,'test','用户确认全文v1、ID和这次试听')
    def fake_download(self,url,path,fmt,timeout):
        Path(path).write_bytes(b'mocked-mp3')
        return {'path':str(Path(path).resolve()),'sha256':tts.hashlib.sha256(b'mocked-mp3').hexdigest(),
                'duration_seconds':2.0}
    def wait(self,seconds=0):
        with patch.object(tts,'download_audio',side_effect=self.fake_download):
            return tts.wait_job(self.client,self.job,self.directory,'test',self.audio,seconds)

    def test_request_sends_real_text_and_true_timestamp(self):
        job=self.submit()
        action,body,resource=self.client.calls[0]
        self.assertEqual(action,'submit')
        self.assertEqual(body['unique_id'],job['task_id'])
        self.assertEqual(body['req_params']['text'],'你好。世界！')
        self.assertIs(body['req_params']['audio_params']['enable_timestamp'],True)
        self.assertEqual(body['req_params']['model'],'seed-tts-2.0-standard')
        self.assertEqual(resource,'seed-icl-2.0')
    def test_official_resource_without_clone_model(self):
        voice={'speaker':'official_id','name':'旁白','resource_id':'seed-tts-2.0','source':'official'}
        tts.import_voices(self.directory,[voice])
        payload=request(); payload.update(speaker=voice['speaker'],resource_id=voice['resource_id'])
        tts.submit(self.client,payload,self.job,self.directory,'test','真实授权')
        self.assertNotIn('model',self.client.calls[0][1]['req_params'])
    def test_invalid_request_parameters(self):
        for key,value in [('format','wav'),('enable_timestamp',False),('sample_rate',123),
                          ('sample_rate',True),('speech_rate',1.2),('speech_rate',101),('loudness_rate',-51)]:
            with self.subTest(key=key,value=value):
                payload=request(); payload['audio_params'][key]=value
                with self.assertRaises(tts.TtsError): tts.validate_request(payload)
    def test_invalid_text_and_unknown_fields(self):
        for segments in [[],[{'text':''}],[{'text':'\x01你好'}],[{'text':'你'*100001}],
                         [{'spoken_text':'你好'}],[{'text':'你好','guess':1}]]:
            with self.subTest(segments=str(segments)[:30]):
                payload=request(); payload['segments']=segments
                with self.assertRaises(tts.TtsError): tts.validate_request(payload)
        payload=request(); payload['emotion']='happy'
        with self.assertRaises(tts.TtsError): tts.validate_request(payload)
    def test_placeholder_rejected(self):
        payload=request(); payload['speaker']='YOUR_SPEAKER_ID'
        with self.assertRaises(tts.TtsError): tts.validate_request(payload)
    def test_srt_empty_lines_rejected_before_paid_request(self):
        payload=request(); payload['segments']=[{'text':'Hello\n\nworld'}]
        with self.assertRaises(tts.TtsError): tts.submit(self.client,payload,self.job,self.directory,'test','approved')
        self.assertEqual(self.client.calls,[])
        self.assertFalse(self.job.exists())
    def test_api_key_header_injection_or_invalid_encoding_rejected(self):
        for key in ['',' key ','key\r\nInjected: header','中文key']:
            with self.subTest(key='invalid'):
                with self.assertRaises(tts.TtsError): tts.Client(key)
    def test_offline_timeline_output_extension_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()):
            status=tts.main(['--root',str(self.root),'timeline','--request','missing.json',
                 '--result','missing.json','--audio','missing.mp3','--out',str(self.root/'out.srt')])
        self.assertEqual(status,1)
        self.assertFalse((self.root/'out.srt').exists())
    def test_voice_required_no_silent_default(self):
        payload=request(); payload['speaker']='missing'
        with self.assertRaises(tts.TtsError): tts.submit(self.client,payload,self.job,self.directory,'test','approved')
        self.assertEqual(self.client.calls,[])
    def test_voice_source_resource_mismatch(self):
        with self.assertRaises(tts.TtsError):
            tts.import_voices(self.directory,[{'speaker':'new','name':'声','source':'custom','resource_id':'seed-tts-2.0'}])
    def test_import_is_local_and_not_history(self):
        self.assertEqual(self.client.calls,[])
        self.assertFalse((self.directory/'history').exists())
        self.assertEqual(tts.load_json(self.directory/'voices.json')['source'],'console_import_not_cloud_query')
    def test_missing_approval_no_network(self):
        with self.assertRaises(tts.TtsError): tts.submit(self.client,request(),self.job,self.directory,'test',' ')
        self.assertEqual(self.client.calls,[])
    def test_duplicate_submission_protected(self):
        self.submit()
        with self.assertRaises(tts.TtsError): self.submit()
        self.assertEqual(len(self.client.calls),1)
    def test_unknown_submit_preserves_recoverable_id(self):
        self.client.submit_error=True
        with self.assertRaises(tts.TtsError): self.submit()
        job=tts.load_json(self.job)
        self.assertEqual(job['state'],'submission_unknown')
        self.assertEqual(job['task_id'],self.client.task_id)
        self.assertEqual(self.wait()['state'],'completed')
        self.assertEqual(sum(c[0]=='submit' for c in self.client.calls),1)
    def test_completed_outputs_history_and_idempotent_wait(self):
        self.submit(); job=self.wait()
        timeline=tts.load_json(self.audio.with_suffix('.json'))
        self.assertEqual(timeline['time_unit'],'seconds')
        self.assertEqual(timeline['segmentStarts'],[.2,.8])
        self.assertIn('00:00:00,200 --> 00:00:00,800',self.audio.with_suffix('.srt').read_text(encoding='utf-8'))
        calls=len(self.client.calls)
        self.assertEqual(self.wait(),job)
        self.assertEqual(len(self.client.calls),calls)
        history=list((self.directory/'history').glob('*.json'))
        self.assertEqual(len(history),1)
        self.assertEqual(tts.load_json(history[0])['preference'],'generated_only_not_user_approved')
    def test_running_budget_returns_no_success_history(self):
        self.submit(); self.client.response={'task_status':1}
        self.assertEqual(self.wait()['state'],'running')
        self.assertFalse(self.audio.exists())
        self.assertFalse((self.directory/'history').exists())
    def test_failed_task_not_resubmitted(self):
        self.submit(); self.client.response={'task_status':3}
        with self.assertRaises(tts.TtsError): self.wait()
        self.assertEqual(tts.load_json(self.job)['state'],'failed')
        with self.assertRaises(tts.TtsError): self.wait()
        self.assertEqual(sum(c[0]=='submit' for c in self.client.calls),1)
    def test_unknown_status_rejected(self):
        self.submit(); self.client.response={'task_status':True}
        with self.assertRaises(tts.TtsError): self.wait()
    def test_missing_url_preserves_task(self):
        self.submit(); self.client.response.pop('audio_url')
        with self.assertRaises(tts.TtsError): self.wait()
        self.assertFalse((self.directory/'history').exists())
    def test_missing_words_preserves_audio_but_not_history(self):
        self.submit(); self.client.response['sentences'][0]['words']=[]
        with self.assertRaises(tts.TtsError): self.wait()
        self.assertTrue(self.audio.exists())
        self.assertTrue(Path(str(self.job)+'.result.json').exists())
        self.assertEqual(tts.load_json(self.job)['state'],'downloaded')
        self.assertFalse((self.directory/'history').exists())
        self.client.response=result()
        with patch.object(tts,'download_audio',side_effect=AssertionError('must reuse audio')):
            self.assertEqual(tts.wait_job(self.client,self.job,self.directory,'test',self.audio,0)['state'],'completed')
    def test_wrong_key_or_account_rejected_before_query(self):
        self.submit()
        for account,key in [('other',self.client.key),('test','another-key')]:
            with self.subTest(account=account):
                self.client.key=key
                with self.assertRaises(tts.TtsError):
                    tts.wait_job(self.client,self.job,self.directory,account,self.audio,0)
        self.assertEqual(len(self.client.calls),1)
    def test_request_tampering_rejected(self):
        self.submit(); job=tts.load_json(self.job); job['request']['audio_params']['speech_rate']=20
        tts.write_json(self.job,job)
        with self.assertRaises(tts.TtsError): self.wait()
    def test_existing_user_artifacts_preserved(self):
        self.submit(); self.audio.with_suffix('.srt').write_text('user subtitle')
        with self.assertRaises(tts.TtsError): self.wait()
        self.assertEqual(self.audio.with_suffix('.srt').read_text(),'user subtitle')
        self.assertEqual(len(self.client.calls),1)
    def test_completed_audio_tampering_detected(self):
        self.submit(); self.wait(); self.audio.write_bytes(b'changed')
        with self.assertRaises(tts.TtsError): self.wait()
    def test_completed_subtitle_tampering_detected(self):
        self.submit(); self.wait(); self.audio.with_suffix('.srt').write_text('changed')
        with self.assertRaises(tts.TtsError): self.wait()
    def test_job_lock_blocks_concurrent_wait(self):
        self.submit()
        with tts.job_lock(self.job):
            with self.assertRaises(tts.TtsError): self.wait()
    def test_timeline_preserves_original_and_explicit_reading(self):
        payload=request(); payload['segments']=[{'text':'3！','spoken_text':'三！'}]
        data={'sentences':[{'text':'三！','startTime':.2,'endTime':.5,
                           'words':[{'word':'三！','startTime':.2,'endTime':.5}]}]}
        timeline=tts.build_timeline(payload,data,1)
        self.assertEqual(timeline['captions'][0]['text'],'3！')
        self.assertEqual(timeline['wordList'][0]['w'],'三！')
    def test_digit_normalization_without_explicit_reading_fails(self):
        payload=request(); payload['segments']=[{'text':'3！'}]
        data={'sentences':[{'text':'三！','startTime':.2,'endTime':.5,
                           'words':[{'word':'三！','startTime':.2,'endTime':.5}]}]}
        with self.assertRaises(tts.TtsError): tts.build_timeline(payload,data,1)
    def test_meaningful_symbol_cannot_disappear_from_reading(self):
        payload=request(); payload['segments']=[{'text':'a+b'}]
        data={'sentences':[{'text':'ab','startTime':.2,'endTime':.5,
                           'words':[{'word':'ab','startTime':.2,'endTime':.5}]}]}
        with self.assertRaises(tts.TtsError): tts.build_timeline(payload,data,1)
        payload['segments'][0]['spoken_text']='a加b'
        data['sentences'][0]['text']=data['sentences'][0]['words'][0]['word']='a加b'
        self.assertEqual(tts.build_timeline(payload,data,1)['captions'][0]['text'],'a+b')
        self.assertEqual(tts.normalize('5%'),'5%')
    def test_english_word_not_divided_into_invented_character_times(self):
        payload=request(); payload['segments']=[{'text':'Hello！'}]
        data={'sentences':[{'text':'hello!','startTime':.2,'endTime':.5,
                           'words':[{'word':'hello!','startTime':.2,'endTime':.5}]}]}
        timeline=tts.build_timeline(payload,data,1)
        self.assertEqual(len(timeline['wordList']),1)
        payload['segments']=[{'text':'Hel'},{'text':'lo！'}]
        with self.assertRaises(tts.TtsError): tts.build_timeline(payload,data,1)
    def test_time_units_invalid_and_bounds(self):
        for field,value in [('startTime',-1),('startTime',float('nan')),('endTime',1600),('endTime',.1)]:
            with self.subTest(field=field,value=value):
                data=result(); data['sentences'][0][field]=value
                with self.assertRaises(tts.TtsError): tts.build_timeline(request(),data,2)
    def test_word_overlap_and_confidence_rejected(self):
        for field,value in [('startTime',.3),('endTime',3),('confidence',2)]:
            data=result(); data['sentences'][0]['words'][1][field]=value
            with self.subTest(field=field):
                with self.assertRaises(tts.TtsError): tts.build_timeline(request(),data,2)
    def test_zero_duration_words_rejected_no_history(self):
        self.submit()
        for word in self.client.response['sentences'][0]['words']:
            word['startTime']=word['endTime']=.2
        with self.assertRaises(tts.TtsError): self.wait()
        self.assertFalse((self.directory/'history').exists())
    def test_candidates_reject_mismatched_active_key_but_allow_offline(self):
        for command in ('voices','history'):
            with patch.dict(os.environ,{'VOLC_TTS_API_KEY':'other-key'}), contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(tts.main(['--root',str(self.root),'--account','test',command]),1)
            self.assertEqual(output.getvalue(),'')
        with patch.dict(os.environ,{'VOLC_TTS_API_KEY':''}), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(tts.main(['--root',str(self.root),'--account','test','voices']),0)
        self.assertIn('S_test_voice',output.getvalue())
    def test_missing_or_extra_words_fail(self):
        data=result(); data['sentences'][0]['words'].pop()
        with self.assertRaises(tts.TtsError): tts.build_timeline(request(),data,2)
        data=result(); data['sentences'][0]['text']='另一句'
        with self.assertRaises(tts.TtsError): tts.build_timeline(request(),data,2)
    def test_key_binding_rejects_rotation_under_same_alias(self):
        with self.assertRaises(tts.TtsError): tts.account_dir(self.root,'test','different')
    def test_json_duplicate_and_nonfinite_rejected(self):
        for raw in ['{"a":1,"a":2}','{"a":NaN}']:
            path=self.root/'invalid.json'; path.write_text(raw)
            with self.assertRaises(tts.TtsError): tts.load_json(path)
    def test_api_contract_headers_body_and_business_code(self):
        response=contextlib.nullcontext(io.BytesIO(json.dumps({'code':20000000,'data':{'task_status':1}}).encode()))
        client=tts.Client('secret-only-for-test')
        with patch.object(client.opener,'open',return_value=response) as call:
            client.post('query',{'task_id':'task'},'seed-tts-2.0')
        sent=call.call_args.args[0]
        self.assertEqual(sent.full_url,'https://openspeech.bytedance.com/api/v3/tts/query')
        headers={k.lower():v for k,v in sent.header_items()}
        self.assertEqual(headers['x-api-key'],client.key)
        self.assertEqual(headers['x-api-resource-id'],'seed-tts-2.0')
        self.assertNotIn('authorization',headers)
        self.assertEqual(json.loads(sent.data),{'task_id':'task'})
        response=contextlib.nullcontext(io.BytesIO(b'{"code":200,"data":{}}'))
        with patch.object(client.opener,'open',return_value=response):
            with self.assertRaises(tts.TtsError): client.post('query',{},'seed-tts-2.0')
    def test_api_redirect_rejected(self):
        with self.assertRaises(tts.TtsError): tts.NoRedirect().redirect_request(None,None,302,'',{},'https://example.com')
    def test_download_does_not_send_key(self):
        opener=unittest.mock.Mock()
        opener.open.return_value=contextlib.nullcontext(io.BytesIO(b'mp3-bytes'))
        with patch.object(tts,'public_https'), patch.object(tts.urllib.request,'build_opener',return_value=opener), patch.object(tts,'audio_duration',return_value=1):
            tts.download_audio('https://example.com/audio',self.audio,'mp3',30)
        self.assertEqual(opener.open.call_args.args,('https://example.com/audio',))
    def test_download_private_address_rejected(self):
        with patch.object(tts.socket,'getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]):
            with self.assertRaises(tts.TtsError): tts.public_https('https://example.com/a')
    def test_configure_seals_env_key_no_cloud_request_no_echo(self):
        with patch.dict(os.environ,{'VOLC_TTS_API_KEY':'private-test-value'}), patch.object(tts,'dpapi',return_value=b'cipher'), patch.object(tts.Client,'post',side_effect=AssertionError('no cloud')), contextlib.redirect_stdout(io.StringIO()) as output:
            status=tts.main(['--root',str(self.root),'--account','new','configure'])
        self.assertEqual(status,0)
        self.assertNotIn('private-test-value',output.getvalue())
        store=(self.root/'volcengine/new/credentials.dpapi.json').read_text()
        self.assertNotIn('private-test-value',store)
    def test_environment_key_overrides_dpapi(self):
        with patch.dict(os.environ,{'VOLC_TTS_API_KEY':'override'}), patch.object(tts,'dpapi',side_effect=AssertionError('must not decrypt')):
            self.assertEqual(tts.credentials(self.directory),'override')
    def test_real_mp3_decode_and_invalid_audio(self):
        if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
            self.skipTest('ffmpeg/ffprobe not installed')
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=440:duration=1',
                        '-c:a','libmp3lame',str(self.audio)],check=True,capture_output=True)
        self.assertGreater(tts.audio_duration(self.audio),.9)
        self.audio.write_bytes(b'{"error":"not audio"}')
        with self.assertRaises(tts.TtsError): tts.audio_duration(self.audio)


if __name__=='__main__':
    unittest.main()
