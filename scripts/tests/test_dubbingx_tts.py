import contextlib
import hashlib
import hmac
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import wave

SCRIPT = Path(__file__).resolve().parents[1] / "dubbingx_tts.py"
spec = importlib.util.spec_from_file_location("dubbingx_tts", SCRIPT)
tts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tts)


class StubClient:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []
        self.key, self.timeout = "unit-test-not-a-real-key", 3

    def post(self, path, payload=None):
        self.calls.append((path, payload))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


VOICE = {"id": "123", "name": "测试自定义声", "description": "稳重", "grade": "custom",
         "version": "V3", "isOfficial": False, "status": True}
EMOTIONS = [{"type": {"zh": "自定义情绪"}, "aura": [{"zh": "常规默认"}]}]
PAYLOAD = {"voiceId": "123", "text": "已经确认的文案。", "language": "zh",
           "emotion": "自定义情绪-常规默认", "fileFormat": "wav"}


def client_for_submit(result=None, voice=None):
    return StubClient({"total": 1, "list": [voice or VOICE]}, EMOTIONS,
                      {"taskId": "456"} if result is None else result)


def wav_bytes():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b"\0\0" * 8000)
    return buffer.getvalue()


class TtsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="配音 测试 ")
        self.root = Path(self.temp.name)
        self.job_path = self.root / "任务.json"
        self.directory = tts.account_dir(self.root, "test")

    def tearDown(self):
        self.temp.cleanup()

    def submit(self, client=None, payload=None):
        return tts.submit(client or client_for_submit(), payload or dict(PAYLOAD), self.job_path,
                          "test", "用户确认文案v1和音色123，仅生成此试听", "mine")

    def test_mine_pagination_and_filters(self):
        client = StubClient({"total": 2, "list": [VOICE]}, {"total": 2, "list": [{**VOICE, "id": "124"}]})
        result = tts.voice_pages(client, all_pages=True, keyword="测试", gender=0)
        self.assertEqual(["123", "124"], [voice["id"] for voice in result])
        self.assertEqual(2, client.calls[1][1]["pageIndex"])
        self.assertIs(client.calls[0][1]["isMyModel"], True)
        self.assertEqual(0, client.calls[0][1]["gender"])

    def test_official_does_not_claim_mine(self):
        client = StubClient({"total": 1, "list": [VOICE]})
        tts.voice_pages(client, mine=False)
        self.assertNotIn("isMyModel", client.calls[0][1])

    def test_submit_exact_json_and_does_not_store_text_or_key(self):
        client = client_for_submit()
        job = self.submit(client)
        self.assertEqual(("/v1/addTtsTask", PAYLOAD), client.calls[-1])
        self.assertEqual("456", job["task_id"])
        content = self.job_path.read_text(encoding="utf-8")
        self.assertNotIn(PAYLOAD["text"], content)
        self.assertNotIn(client.key, content)
        self.assertEqual("submitted", job["state"])
        self.assertEqual([], tts.history(self.directory))

    def test_missing_approval_no_api_calls(self):
        client = client_for_submit()
        with self.assertRaises(tts.TtsError):
            tts.submit(client, PAYLOAD, self.job_path, "test", " ", "mine")
        self.assertEqual([], client.calls)

    def test_missing_voice_does_not_fallback(self):
        client = StubClient({"total": 0, "list": []})
        with self.assertRaises(tts.TtsError):
            self.submit(client)
        self.assertFalse(self.job_path.exists())
        self.assertEqual(1, len(client.calls))

    def test_disabled_voice_rejected(self):
        with self.assertRaises(tts.TtsError):
            self.submit(client_for_submit(voice={**VOICE, "status": False}))
        self.assertFalse(self.job_path.exists())

    def test_custom_emotion_empty_rejected_before_submit(self):
        client = client_for_submit()
        with self.assertRaises(tts.TtsError):
            self.submit(client, {**PAYLOAD, "emotion": ""})
        self.assertEqual(2, len(client.calls))

    def test_unknown_emotion_rejected(self):
        with self.assertRaises(tts.TtsError):
            self.submit(payload={**PAYLOAD, "emotion": "恐惧-尖叫-5"})

    def test_premium_and_ordinary_formats(self):
        emotions = [{"type": {"zh": "常规"}, "aura": [{"zh": "日常说话"}]}]
        for emotion, valid in [("常规-日常说话-3", True), ("常规-日常说话-6", False), ("", True)]:
            client = StubClient({"total": 1, "list": [{**VOICE, "grade": "premium"}]}, emotions)
            if valid:
                tts.preflight(client, {**PAYLOAD, "emotion": emotion}, "mine")
            else:
                with self.assertRaises(tts.TtsError):
                    tts.preflight(client, {**PAYLOAD, "emotion": emotion}, "mine")
        client = StubClient({"total": 1, "list": [{**VOICE, "grade": "ordinary"}]}, EMOTIONS)
        tts.preflight(client, {**PAYLOAD, "emotion": "单情绪"}, "mine")

    def test_v4_natural_emotion_and_language(self):
        payload = {**PAYLOAD, "emotionCustom": "开心", "emotion": ""}
        client = client_for_submit(voice={**VOICE, "version": "v4"})
        self.submit(client, payload)
        for voice, language in [(VOICE, "zh"), ({**VOICE, "version": "V4"}, "jp")]:
            with self.assertRaises(tts.TtsError):
                tts.preflight(client_for_submit(voice=voice), {**payload, "language": language}, "mine")

    def test_invalid_requests_and_template(self):
        variants = [{**PAYLOAD, "voiceId": "../123"}, {**PAYLOAD, "voiceId": "YOUR_VOICE_ID"},
                    {**PAYLOAD, "audioSpeed": float("nan")}, {**PAYLOAD, "audioSpeed": False},
                    {**PAYLOAD, "audioVolume": 13}, {**PAYLOAD, "emotionCustom": "开心"},
                    {**PAYLOAD, "voice_id": "123"}, {**PAYLOAD, "text": ""},
                    {**PAYLOAD, "text": '<break time="0.8"\'/>'},
                    {**PAYLOAD, "text": '<break time="21"/>'},
                    {**PAYLOAD, "text": '<phoneme ph="duan2">段</phoneme>', "language": "en"}]
        for payload in variants:
            with self.subTest(payload=payload), self.assertRaises(tts.TtsError):
                tts.validate_request(payload)
        tts.validate_request({**PAYLOAD, "text": '你好<break time="0.8"/>！'})

    def test_unknown_submission_and_no_duplicate_post(self):
        client = client_for_submit(tts.TtsError("network timeout"))
        with self.assertRaises(tts.TtsError):
            self.submit(client)
        self.assertEqual("submission_unknown", tts.load_json(self.job_path)["state"])
        with self.assertRaises(tts.TtsError):
            self.submit(client)
        self.assertEqual(1, sum(path == "/v1/addTtsTask" for path, _ in client.calls))
        self.assertEqual([], tts.history(self.directory))

    def test_malformed_submit_response_not_guessed(self):
        for response in ["456", {"traceId": "456"}, {"taskId": 456}]:
            path = self.root / (str(len(list(self.root.glob("*.json")))) + ".json")
            with self.assertRaises(tts.TtsError):
                tts.submit(client_for_submit(response), PAYLOAD, path, "test", "真实授权引用", "mine")
            self.assertEqual("submission_unknown", tts.load_json(path)["state"])

    def test_poll_timeout_and_resume_same_id(self):
        self.submit()
        client = StubClient({"id": "456", "status": "Generating"})
        result = tts.wait_job(client, self.job_path, self.directory, None, seconds=0)
        self.assertEqual("waiting", result["state"])
        self.assertEqual([("/v1/getTtsTaskInfo/456", None)], client.calls)
        self.assertEqual([], tts.history(self.directory))

    def test_empty_completed_url_waits(self):
        self.submit()
        client = StubClient({"id": "456", "status": "Completed", "fileUrl": None})
        self.assertEqual("waiting", tts.wait_job(client, self.job_path, self.directory, None, seconds=0)["state"])
        self.assertEqual([], tts.history(self.directory))

    def test_failed_canceled_unknown_and_wrong_id_no_history(self):
        self.submit()
        for data in [{"id": "456", "status": "Failed"}, {"id": "456", "status": "Canceled"},
                     {"id": "456", "status": "Surprise"}, {"id": "999", "status": "Completed"}]:
            with self.subTest(data=data), self.assertRaises(tts.TtsError):
                tts.wait_job(StubClient(data), self.job_path, self.directory, None, seconds=0)
        self.assertEqual([], tts.history(self.directory))

    def test_success_history_duration_and_idempotent_recovery(self):
        self.submit()
        output = self.root / "试听.wav"
        output.write_bytes(wav_bytes())
        audio = {"path": str(output.resolve()), "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                 "duration_seconds": 1.0}
        client = StubClient({"id": "456", "status": "Completed", "fileUrl": "https://cdn.example/a.wav?signature=secret"})
        with patch.object(tts, "download_audio", return_value=audio):
            tts.wait_job(client, self.job_path, self.directory, output)
        history = tts.history(self.directory)
        self.assertEqual("123", history[0]["voice_id"])
        self.assertEqual(1, history[0]["successful_generations"])
        self.assertIn("不等于", history[0]["preference"])
        self.assertNotIn("signature", self.job_path.read_text(encoding="utf-8"))
        with patch.object(tts, "record_success", side_effect=tts.TtsError("模拟历史写盘失败")):
            with self.assertRaises(tts.TtsError):
                tts.wait_job(StubClient(), self.job_path, self.directory, output)
        tts.wait_job(StubClient(), self.job_path, self.directory, output)
        self.assertEqual(1, tts.history(self.directory)[0]["successful_generations"])
        output.write_bytes(b"changed")
        with self.assertRaises(tts.TtsError):
            tts.wait_job(StubClient(), self.job_path, self.directory, output)

    def test_invalid_download_does_not_enter_history(self):
        self.submit()
        with patch.object(tts, "download_audio", side_effect=tts.TtsError("返回HTML")):
            with self.assertRaises(tts.TtsError):
                tts.wait_job(StubClient({"id": "456", "status": "Completed", "fileUrl": "https://example.com"}),
                             self.job_path, self.directory, self.root / "bad.wav")
        self.assertEqual([], tts.history(self.directory))

    def test_account_binding_and_task_key_isolation(self):
        tts.account_dir(self.root, "test", "key-one")
        with self.assertRaises(tts.TtsError):
            tts.account_dir(self.root, "test", "key-two")
        self.submit()
        client = StubClient()
        client.key = "different"
        with self.assertRaises(tts.TtsError):
            tts.wait_job(client, self.job_path, self.directory, None)
        self.assertEqual([], client.calls)

    def test_http_post_bearer_json_and_business_errors(self):
        client = tts.Client("fake-key", 3)
        captured = []
        def opened(request, timeout):
            captured.append(request)
            return io.BytesIO(json.dumps({"code": 200, "success": True, "data": ["ok"]}).encode())
        with patch.object(client.opener, "open", side_effect=opened):
            self.assertEqual(["ok"], client.post("/v1/getTtsTaskListInfo", ["456"]))
        self.assertEqual("POST", captured[0].get_method())
        self.assertEqual("Bearer fake-key", captured[0].get_header("Authorization"))
        self.assertEqual(["456"], json.loads(captured[0].data))
        for body in [{"code": 400, "success": False, "msg": "fake-key"}, {"code": 200, "success": True}]:
            with patch.object(client.opener, "open", return_value=io.BytesIO(json.dumps(body).encode())):
                with self.assertRaises(tts.TtsError) as error:
                    client.post("/v1/addTtsTask", PAYLOAD)
                self.assertNotIn("fake-key", str(error.exception))

    def test_bearer_redirect_rejected(self):
        with self.assertRaises(tts.TtsError):
            tts.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example")

    def test_audio_download_no_auth_and_validate_before_install(self):
        output = self.root / "音频.wav"
        opener = unittest.mock.Mock()
        opener.open.return_value = io.BytesIO(wav_bytes())
        with patch.object(tts, "public_https"), patch.object(tts.urllib.request, "build_opener", return_value=opener):
            audio = tts.download_audio("https://example.com/a.wav?sig=private", output, "wav", 3)
        self.assertEqual(1.0, audio["duration_seconds"])
        self.assertEqual("https://example.com/a.wav?sig=private", opener.open.call_args.args[0])
        with self.assertRaises(tts.TtsError):
            tts.download_audio("https://example.com", output, "wav", 3)
        self.assertEqual(wav_bytes(), output.read_bytes())
        opener.open.return_value = io.BytesIO(b"<html>not audio</html>")
        bad = self.root / "bad.wav"
        with patch.object(tts, "public_https"), patch.object(tts.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(tts.TtsError):
                tts.download_audio("https://example.com", bad, "wav", 3)
        self.assertFalse(bad.exists())

    def test_private_download_addresses_rejected(self):
        for url in ["http://example.com", "file:///a", "https://user:pass@example.com"]:
            with self.assertRaises(tts.TtsError):
                tts.public_https(url)
        with patch.object(tts.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
            with self.assertRaises(tts.TtsError):
                tts.public_https("https://example.com")

    def test_truncated_wav_rejected_by_complete_download_pipeline(self):
        self.submit()
        opener = unittest.mock.Mock()
        opener.open.return_value = io.BytesIO(wav_bytes()[:44])
        output = self.root / "truncated.wav"
        client = StubClient({"id": "456", "status": "Completed", "fileUrl": "https://example.com/a.wav"})
        with patch.object(tts, "public_https"), patch.object(tts.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(tts.TtsError):
                tts.wait_job(client, self.job_path, self.directory, output)
        self.assertFalse(output.exists())
        self.assertEqual([], tts.history(self.directory))

    def test_cloud_cache_and_generated_history_remain_distinct(self):
        client = StubClient({"total": 1, "list": [VOICE]})
        snapshot = tts.cache_voices(client, self.directory)
        self.assertEqual("123", snapshot["voices"][0]["id"])
        self.assertEqual("mine", snapshot["source"])
        self.assertEqual([], tts.history(self.directory))
        self.assertEqual(snapshot, tts.load_json(self.directory / "my-voices.json"))

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_dpapi_roundtrip_and_private_store_env_override(self):
        original = json.dumps({"api_key": "fake-test-key", "api_secret": "fake-test-secret"}).encode()
        cipher = tts.dpapi(original)
        self.assertNotIn(b"fake-test-key", cipher)
        self.assertEqual(original, tts.dpapi(cipher, decrypt=True))
        import base64
        tts.write_json(self.directory / "credentials.dpapi.json", {"protection": "windows-dpapi-current-user", "ciphertext": base64.b64encode(cipher).decode()})
        with patch.dict(os.environ, {"DUBBINGX_API_KEY": "", "DUBBINGX_API_SECRET": ""}):
            self.assertEqual(("fake-test-key", "fake-test-secret"), tts.credentials(self.directory))
        with patch.dict(os.environ, {"DUBBINGX_API_KEY": "override-key", "DUBBINGX_API_SECRET": ""}):
            self.assertEqual(("override-key", ""), tts.credentials(self.directory))

    def test_configure_first_sync_then_sealed_store(self):
        client = StubClient({"total": 1, "list": [VOICE]})
        with patch.dict(os.environ, {"DUBBINGX_API_KEY": client.key, "DUBBINGX_API_SECRET": "fake-secret"}), patch.object(tts, "Client", return_value=client), patch.object(tts, "dpapi", return_value=b"cipher"), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(0, tts.main(["--root", str(self.root), "--account", "test", "configure"]))
        self.assertNotIn(client.key, output.getvalue())
        self.assertNotIn("fake-secret", output.getvalue())
        self.assertTrue((self.directory / "my-voices.json").exists())
        self.assertTrue((self.directory / "credentials.dpapi.json").exists())
        self.assertEqual([], tts.history(self.directory))

    def test_cached_voices_offline(self):
        tts.cache_voices(StubClient({"total": 1, "list": [VOICE]}), self.directory)
        with patch.dict(os.environ, {"CODEX_HOME": str(self.root)}, clear=True), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(0, tts.main(["--root", str(self.root), "--account", "test", "cached-voices"]))
        self.assertEqual("123", json.loads(output.getvalue())["voices"][0]["id"])

    def test_default_active_account_and_cached_filter(self):
        tts.cache_voices(StubClient({"total": 1, "list": [{**VOICE, "version": "v4"}]}), self.directory, official=True)
        tts.write_json(self.root / "dubbingx/active-account.json", {"account": "test"})
        with patch.dict(os.environ, {"CODEX_HOME": str(self.root)}, clear=True), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(0, tts.main(["--root", str(self.root), "cached-voices", "--official", "--keyword", "123", "--version", "V4"]))
        self.assertEqual("123", json.loads(output.getvalue())["voices"][0]["id"])

    def test_cached_voices_rejects_wrong_env_key_for_bound_alias(self):
        tts.account_dir(self.root, "test", "key-A")
        tts.cache_voices(StubClient({"total": 1, "list": [VOICE]}), self.directory)
        with patch.dict(os.environ, {"DUBBINGX_API_KEY": "key-B"}), contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(1, tts.main(["--root", str(self.root), "--account", "test", "cached-voices"]))
        self.assertEqual("", output.getvalue())

    def test_webhook_private_address_rejected_before_api(self):
        client = StubClient()
        with patch.dict(os.environ, {"DUBBINGX_API_KEY": client.key}), patch.object(tts, "Client", return_value=client), patch.object(tts.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("192.168.1.2", 443))]), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(1, tts.main(["--root", str(self.root), "set-webhook", "--url", "https://192.168.1.2/callback", "--confirmed-account-change"]))
        self.assertEqual([], client.calls)

    def test_webhook_independent_vector_null_and_fixed_order(self):
        payload = {"timestamp": 123, "fileUrl": None, "status": "Failed", "taskId": "456", "event": "task.failed"}
        literal = "taskId=456\nstatus=Failed\nfileUrl=\ntimestamp=123"
        signature = "sha256=" + hmac.new(b"fake-secret", literal.encode(), hashlib.sha256).hexdigest()
        self.assertTrue(tts.verify_webhook(payload, "fake-secret", signature))
        self.assertFalse(tts.verify_webhook({**payload, "status": "Completed"}, "fake-secret", signature))
        self.assertFalse(tts.verify_webhook(payload, "wrong-secret", signature))
        self.assertFalse(tts.verify_webhook(payload, "fake-secret", ""))

    def test_cli_history_without_credentials_empty_no_fabricated_ids(self):
        with patch.dict(os.environ, {"CODEX_HOME": str(self.root)}, clear=True), contextlib.redirect_stdout(io.StringIO()) as output:
            code = tts.main(["--root", str(self.root), "history"])
        self.assertEqual(0, code)
        self.assertEqual([], json.loads(output.getvalue()))

    def test_cli_all_api_command_routes_and_webhook_gate(self):
        text = self.root / "稿.txt"
        text.write_text("测试", encoding="utf-8")
        cases = [(["emotions", "--voice-id", "123"], "/v1/getEmotionList/123", {}),
                 (["status", "--task-id", "456"], "/v1/getTtsTaskInfo/456", {}),
                 (["batch-status", "--task-ids", "456", "789"], "/v1/getTtsTaskListInfo", ["456", "789"]),
                 (["analyze-emotion", "--text-file", str(text)], "/v2/analyzeEmotion", {"text": "测试"}),
                 (["auto-pause", "--text-file", str(text), "--out", str(self.root / "停顿.txt")], "/v2/autoPause", {"text": "测试"}),
                 (["set-webhook", "--url", "https://example.com/callback", "--confirmed-account-change"], "/v1/setWebhookUrl", {"callbackUrl": "https://example.com/callback"})]
        for argv, path, payload in cases:
            client = StubClient("建议或结果")
            with patch.dict(os.environ, {"DUBBINGX_API_KEY": client.key}), patch.object(tts, "Client", return_value=client), patch.object(tts.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, tts.main(["--root", str(self.root), *argv]))
            self.assertEqual((path, payload), (client.calls[0][0], client.calls[0][1] or {}))
        client = StubClient()
        with patch.dict(os.environ, {"DUBBINGX_API_KEY": client.key}), patch.object(tts, "Client", return_value=client), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(1, tts.main(["--root", str(self.root), "set-webhook", "--url", ""]))
        self.assertEqual([], client.calls)

    def test_cli_missing_key_and_secret_not_printed(self):
        with patch.dict(os.environ, {"CODEX_HOME": str(self.root)}, clear=True), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(1, tts.main(["--root", str(self.root), "voices"]))
        output = tts.safe_output({"msg": "a fake-key fake-secret", "fileUrl": "https://example.com/a?sig=secret",
                                  "key_fingerprint": "hash"}, "fake-key", "fake-secret")
        self.assertNotIn("key_fingerprint", output)
        self.assertEqual("https://example.com/a", output["fileUrl"])
        self.assertNotIn("fake-key", output["msg"])


if __name__ == "__main__":
    unittest.main()
