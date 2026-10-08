#!/usr/bin/env python3
"""DubbingX async TTS, bounded polling and account-scoped successful voice history.

Only Python standard library; MP3 validation additionally uses ffprobe.
Credentials: DUBBINGX_API_KEY, DUBBINGX_API_SECRET (webhook verification only).
"""
import argparse
import base64
import ctypes
import hashlib
import hmac
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

BASE_URL = "https://tts-api.dubbingx.com"
TERMINAL = {"Completed", "Failed", "Canceled"}
MAX_AUDIO_BYTES = 128 * 1024 * 1024


class TtsError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def load_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise TtsError(f"重复JSON字段: {key}")
            result[key] = value
        return result
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"), object_pairs_hook=unique,
                          parse_constant=lambda value: (_ for _ in ()).throw(TtsError(f"非法数值: {value}")))
    except (ValueError, OSError) as exc:
        raise TtsError(f"不能读取JSON文件: {path}") from exc


def write_json(path, value, exclusive=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if exclusive:
        try:
            with path.open("xb") as output:
                output.write(encoded)
        except FileExistsError as exc:
            raise TtsError(f"文件已存在，不能覆盖: {path}") from exc
        return
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".tts-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def identifier(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise TtsError(f"{label}必须为1–128位字母、数字、下划线或连字符")
    if value.upper().startswith("YOUR_"):
        raise TtsError(f"请填写真实{label}，不能使用模板占位符")
    return value


def number(value, label, low=None, high=None):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise TtsError(f"{label}必须为有限数值")
    if (low is not None and value < low) or (high is not None and value > high):
        raise TtsError(f"{label}超出允许范围")
    return value


def validate_request(payload):
    allowed = {"voiceId", "text", "emotion", "emotionCustom", "language", "audioPitch",
               "audioSpeed", "audioVolume", "fileFormat"}
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise TtsError("请求必须为对象，且只包含文档规定的V1字段")
    identifier(payload.get("voiceId"), "音色ID")
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise TtsError("text不能为空")
    if payload.get("language") not in {"zh", "jp", "en", "yue", "sc", "ko"}:
        raise TtsError("language必须为zh/jp/en/yue/sc/ko")
    if payload.get("fileFormat", "wav") not in {"wav", "mp3"}:
        raise TtsError("fileFormat必须为wav或mp3")
    for field in ("emotion", "emotionCustom"):
        if field in payload and not isinstance(payload[field], str):
            raise TtsError(f"{field}必须为字符串")
    if payload.get("emotion") and payload.get("emotionCustom"):
        raise TtsError("不要同时设置emotion和emotionCustom")
    for field in ("audioPitch", "audioSpeed"):
        if field in payload:
            number(payload[field], field, low=0.000001)
    if "audioVolume" in payload:
        number(payload["audioVolume"], "audioVolume", -12, 12)
    if re.search(r"</?(?:break|phoneme|speak)\b", text):
        try:
            root = ET.fromstring("<root>" + text + "</root>")
        except ET.ParseError as exc:
            raise TtsError("停顿/音素标签不是有效XML；不要复制文档示例中多余的引号") from exc
        for element in root.iter():
            if element.tag not in {"root", "break", "phoneme"}:
                raise TtsError("V1文本只使用break/phoneme标签，不使用V2 speak封装")
            if element.tag == "break":
                try:
                    number(float(element.attrib["time"]), "break time", 0, 20)
                except (ValueError, KeyError) as exc:
                    raise TtsError("break time必须为0–20秒") from exc
            if element.tag == "phoneme" and payload["language"] != "zh":
                raise TtsError("phoneme只支持中文")
    return payload


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise TtsError("拒绝API重定向，避免Authorization传到其他地址")


class Client:
    def __init__(self, key, timeout=30):
        if not key or "\n" in key or "\r" in key:
            raise TtsError("请在本机设置DUBBINGX_API_KEY环境变量")
        self.key = key
        self.timeout = timeout
        self.opener = urllib.request.build_opener(NoRedirect())

    def post(self, path, payload=None):
        request = urllib.request.Request(BASE_URL + path,
                  data=json.dumps({} if payload is None else payload, ensure_ascii=False).encode("utf-8"),
                  headers={"Authorization": "Bearer " + self.key, "Content-Type": "application/json"},
                  method="POST")
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                result = json.loads(response.read(4 * 1024 * 1024))
        except urllib.error.HTTPError as exc:
            raise TtsError(f"API HTTP {exc.code}；提交请求不可盲目重试") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise TtsError("API连接超时、连接失败或返回非JSON；提交状态可能未知") from exc
        if not isinstance(result, dict) or result.get("success") is not True or result.get("code") != 200:
            # Do not print remote bodies: they can reflect credentials, text or signed URLs.
            message = safe_output(str(result.get("msg", "")) if isinstance(result, dict) else "", self.key)
            raise TtsError("API业务失败: " + message[:200] + "；未自动重试")
        if "data" not in result:
            raise TtsError("API成功响应缺少data")
        return result["data"]


def voice_pages(client, mine=True, all_pages=False, **filters):
    result, page = [], 1
    while True:
        payload = {"pageIndex": page, "pageSize": 100, **filters}
        if mine:
            payload["isMyModel"] = True
        data = client.post("/v2/getTTSTimbreList", payload)
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise TtsError("音色列表响应结构不正确")
        result.extend(data["list"])
        if not all_pages or not data["list"] or len(result) >= int(data.get("total", len(result))):
            break
        page += 1
        if page > 100:
            raise TtsError("音色列表超过100页，请使用筛选；未返回截断的完整列表")
    return result


def preflight(client, payload, source):
    voices = voice_pages(client, mine=source == "mine", all_pages=True)
    selected = next((v for v in voices if str(v.get("id")) == payload["voiceId"]), None)
    if selected is None or selected.get("status") is not True:
        raise TtsError("所选ID未在指定音色库中找到或不可用；不得自动换音色")
    version, grade = str(selected.get("version", "")).upper(), selected.get("grade")
    if payload.get("emotionCustom") and version != "V4":
        raise TtsError("emotionCustom仅支持V4音色")
    if version == "V4" and payload["language"] not in {"zh", "en"}:
        raise TtsError("V4仅支持zh/en")
    emotions = client.post("/v1/getEmotionList/" + payload["voiceId"])
    if not isinstance(emotions, list):
        raise TtsError("情绪列表响应结构不正确")
    pairs = [(e.get("type", {}).get("zh"), a.get("zh")) for e in emotions for a in e.get("aura", [])]
    emotion = payload.get("emotion", "")
    if payload.get("emotionCustom"):
        return selected
    if grade == "custom" and not emotion:
        raise TtsError("多情绪音色必须选择明确emotion，不能传空")
    if emotion:
        if grade == "premium":
            match = re.fullmatch(r"(.+)-([1-5])", emotion)
            choices = {f"{kind}-{style}" for kind, style in pairs}
            valid = match is not None and match[1] in choices
        elif grade == "custom":
            valid = emotion in {f"{kind}-{style}" for kind, style in pairs} | {style for _, style in pairs}
        elif grade == "ordinary":
            valid = emotion == "单情绪"
        else:
            raise TtsError("音色grade未知，请核对平台，不猜测情绪格式")
        if not valid:
            raise TtsError("emotion与该音色的实时情绪列表不匹配")
    return selected


def account_dir(root, account, key=None):
    identifier(account, "账号别名")
    directory = Path(root) / "dubbingx" / account
    if key is not None:
        binding = directory / "account.json"
        fingerprint = hashlib.sha256(key.encode()).hexdigest()
        if not binding.exists():
            write_json(binding, {"schema_version": 1, "key_fingerprint": fingerprint}, exclusive=True)
        if load_json(binding).get("key_fingerprint") != fingerprint:
            raise TtsError("此账号别名已绑定另一API Key；更换别名以免混用私人音色历史")
    return directory


def dpapi(data, decrypt=False):
    """Windows CurrentUser protection; no fallback plaintext credential store."""
    if os.name != "nt":
        raise TtsError("本地加密凭据仅支持Windows；其他系统用环境变量，不回退明文")
    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("bytes", ctypes.POINTER(ctypes.c_ubyte))]
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source, output = Blob(len(data), buffer), Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(Blob)]
    function.restype = ctypes.c_int
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise TtsError("Windows凭据加密/解密失败；须由保存时的本机用户读取")
    kernel.LocalFree.argtypes, kernel.LocalFree.restype = [ctypes.c_void_p], ctypes.c_void_p
    try:
        return ctypes.string_at(output.bytes, output.size)
    finally:
        kernel.LocalFree(output.bytes)


def credentials(directory):
    key = os.environ.get("DUBBINGX_API_KEY", "")
    secret = os.environ.get("DUBBINGX_API_SECRET", "")
    if key:
        return key, secret  # Do not pair an override Key with another stored Secret.
    path = directory / "credentials.dpapi.json"
    if not path.exists():
        return "", secret
    store = load_json(path)
    if store.get("protection") != "windows-dpapi-current-user":
        raise TtsError("不支持的凭据保存方式")
    try:
        values = json.loads(dpapi(base64.b64decode(store["ciphertext"], validate=True), decrypt=True))
        if not isinstance(values.get("api_key"), str) or not isinstance(values.get("api_secret", ""), str):
            raise TtsError("解密凭据结构无效")
        return values["api_key"], values.get("api_secret", "")
    except (ValueError, KeyError) as exc:
        raise TtsError("本地加密凭据无效") from exc


def cache_voices(client, directory, official=False):
    voices = voice_pages(client, mine=not official, all_pages=True)
    selected_fields = ("id", "name", "description", "grade", "version", "gender", "isOfficial", "status", "voiceUrl")
    snapshot = {"schema_version": 1, "fetched_at": now(), "source": "official" if official else "mine",
                "voices": [{k: v.get(k) for k in selected_fields} for v in voices],
                "note": "云端查询快照，不是历史成功生成；提交前重新核验"}
    write_json(directory / ("official-voices.json" if official else "my-voices.json"), snapshot)
    return snapshot


def submit(client, payload, job_path, account, approval, source):
    validate_request(payload)
    if not approval.strip():
        raise TtsError("提交必须记录文案、音色和本次调用的用户确认依据")
    if Path(job_path).exists():
        raise TtsError("任务记录已存在；先恢复查询，不能重复提交")
    voice = preflight(client, payload, source)
    job = {"schema_version": 1, "job_id": uuid.uuid4().hex, "account": account,
           "key_fingerprint": hashlib.sha256(client.key.encode()).hexdigest(),
           "created_at": now(), "state": "submitting", "approval": approval,
           "text_sha256": hashlib.sha256(payload["text"].encode()).hexdigest(),
           "settings": {k: v for k, v in payload.items() if k != "text"},
           "voice": {k: voice.get(k) for k in ("id", "name", "description", "grade", "version", "isOfficial")},
           "source": source}
    write_json(job_path, job, exclusive=True)
    try:
        result = client.post("/v1/addTtsTask", payload)
        if not isinstance(result, dict):
            raise TtsError("提交响应不是data.taskId结构，不能猜测任务ID")
        job["task_id"] = identifier(result.get("taskId"), "任务ID")
    except Exception:
        job["state"] = "submission_unknown"
        write_json(job_path, job)
        raise
    job["state"] = "submitted"
    write_json(job_path, job)
    return job


def public_https(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise TtsError("音频下载仅允许无用户凭据的HTTPS地址")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise TtsError("音频主机DNS解析失败") from exc
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise TtsError("拒绝内网/本机音频下载地址")


class AudioRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def audio_duration(path, file_format):
    try:
        if file_format == "wav":
            with wave.open(str(path), "rb") as audio:
                frames = audio.getnframes()
                frame_size = audio.getnchannels() * audio.getsampwidth()
                actual_bytes = 0
                while chunk := audio.readframes(65536):
                    actual_bytes += len(chunk)
                if actual_bytes != frames * frame_size:
                    raise TtsError("WAV样本被截断，不能只信任文件头时长")
                duration = frames / audio.getframerate()
        else:
            result = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0",
                     "-show_entries", "stream=codec_type:format=duration", "-of", "json", str(path)],
                     capture_output=True, text=True, timeout=30, check=True)
            info = json.loads(result.stdout)
            if not info.get("streams") or info["streams"][0].get("codec_type") != "audio":
                raise TtsError("返回文件没有可识别的音频轨")
            duration = float(info["format"]["duration"])
        number(duration, "音频时长", low=0.000001)
        return duration
    except (OSError, ValueError, KeyError, wave.Error, subprocess.SubprocessError) as exc:
        raise TtsError("音频校验失败；MP3需要ffprobe，不能把下载成功当作有效配音") from exc


def download_audio(url, output, file_format, timeout):
    output = Path(output)
    if output.exists():
        raise TtsError(f"音频文件已存在，不能覆盖: {output}")
    public_https(url)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=output.parent, suffix="." + file_format)
    try:
        # A separate opener and request: NEVER forward the API Bearer token to fileUrl.
        with urllib.request.build_opener(AudioRedirect()).open(url, timeout=timeout) as response, os.fdopen(fd, "wb") as file:
            fd = None
            size = 0
            while chunk := response.read(65536):
                size += len(chunk)
                if size > MAX_AUDIO_BYTES:
                    raise TtsError("音频超过128MiB限制；请按已确认段落拆分")
                file.write(chunk)
        duration = audio_duration(temp, file_format)
        digest = hashlib.sha256(Path(temp).read_bytes()).hexdigest()
        # Exclusive creation protects existing user audio, even under concurrent calls.
        os.link(temp, output)
        return {"path": str(output.resolve()), "sha256": digest, "duration_seconds": duration}
    except (urllib.error.URLError, OSError) as exc:
        raise TtsError("音频下载/保存失败；可恢复原任务查询，不要重复合成") from exc
    finally:
        if fd is not None:
            os.close(fd)
        if os.path.exists(temp):
            os.unlink(temp)


def record_success(directory, job):
    event = {"schema_version": 1, "job_id": job["job_id"], "task_id": job["task_id"],
             "voice_id": job["settings"]["voiceId"], "voice": job["voice"], "source": job["source"],
             "settings": job["settings"], "completed_at": job["completed_at"], "audio": job["audio"],
             "approval": job["approval"], "preference": "generated_only"}
    path = directory / "history" / (job["job_id"] + ".json")
    if path.exists():
        if load_json(path) != event:
            raise TtsError("同一任务的历史证据冲突")
    else:
        write_json(path, event, exclusive=True)


def wait_job(client, job_path, directory, output, seconds=45, interval=3):
    job = load_json(job_path)
    if job.get("key_fingerprint") != hashlib.sha256(client.key.encode()).hexdigest():
        raise TtsError("任务属于另一API Key，不能混用")
    identifier(job.get("task_id"), "任务ID；提交状态未知请先在平台找回真实ID")
    if job.get("state") == "completed":
        audio = Path(job["audio"]["path"])
        if not audio.exists() or hashlib.sha256(audio.read_bytes()).hexdigest() != job["audio"]["sha256"]:
            raise TtsError("已完成任务的本地音频缺失/变化，请核对原产物")
        record_success(directory, job)
        return job
    deadline = time.monotonic() + seconds
    while True:
        data = client.post("/v1/getTtsTaskInfo/" + job["task_id"])
        if not isinstance(data, dict) or str(data.get("id")) != job["task_id"]:
            raise TtsError("查询结果任务ID不匹配")
        status = data.get("status")
        if status not in {"Ready", "Generating"} | TERMINAL:
            raise TtsError("未知任务状态；未下载或自动重试提交")
        job["remote_status"] = status
        write_json(job_path, job)
        if status in {"Failed", "Canceled"}:
            job["state"] = status.lower()
            write_json(job_path, job)
            raise TtsError(f"任务{status}，未记入成功音色历史；平台查看失败详情")
        if status == "Completed" and data.get("fileUrl"):
            if not output:
                raise TtsError("完成任务需要--out指定本地音频文件")
            job["audio"] = download_audio(data["fileUrl"], output, job["settings"].get("fileFormat", "wav"), client.timeout)
            job["state"], job["completed_at"] = "completed", now()
            write_json(job_path, job)
            record_success(directory, job)
            return job
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            job["state"] = "waiting"
            write_json(job_path, job)
            return job
        time.sleep(min(interval, remaining))


def history(directory):
    voices = {}
    for path in sorted((directory / "history").glob("*.json")):
        event = load_json(path)
        voice_id = event["voice_id"]
        item = voices.setdefault(voice_id, {"voice_id": voice_id, "name": event["voice"].get("name"),
                "source": event["source"], "successful_generations": 0, "last_used": "",
                "availability": "需查询当前账户验证", "preference": "生成过，不等于用户已认可"})
        item["successful_generations"] += 1
        if event["completed_at"] >= item["last_used"]:
            item.update(last_used=event["completed_at"], settings=event["settings"], audio=event["audio"])
    return sorted(voices.values(), key=lambda item: item["last_used"], reverse=True)


def verify_webhook(payload, secret, signature):
    content = "\n".join(f"{key}={'' if payload.get(key) is None else payload[key]}"
                        for key in ("taskId", "status", "fileUrl", "timestamp"))
    expected = "sha256=" + hmac.new(secret.encode(), content.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def safe_output(value, key="", secret=""):
    if isinstance(value, dict):
        return {k: safe_output(v, key, secret) for k, v in value.items() if k != "key_fingerprint"}
    if isinstance(value, list):
        return [safe_output(v, key, secret) for v in value]
    if isinstance(value, str):
        for credential in (key, secret):
            if credential:
                value = value.replace(credential, "[REDACTED]")
        if value.startswith(("https://", "http://")):
            parts = urllib.parse.urlsplit(value)
            return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return value


def main(argv=None):
    codex_root = os.environ.get("CODEX_HOME")
    default_root = (Path(codex_root) if codex_root else Path.home() / ".codex") / "config" / "huashu-art-motion"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=default_root)
    parser.add_argument("--account", help="不同API Key使用不同别名；默认用本地已配置的活跃账号")
    parser.add_argument("--timeout", type=float, default=30)
    sub = parser.add_subparsers(dest="command", required=True)
    voices = sub.add_parser("voices", help="默认查询自己的自训练音色")
    voices.add_argument("--official", action="store_true")
    voices.add_argument("--all-pages", action="store_true")
    voices.add_argument("--keyword")
    voices.add_argument("--grade", choices=["premium", "ordinary", "custom"])
    voices.add_argument("--gender", type=int, choices=[0, 1])
    voices.add_argument("--age-group", choices=["孩童", "少年", "青年", "中年", "老年"])
    emotions = sub.add_parser("emotions")
    emotions.add_argument("--voice-id", required=True)
    for command in ("analyze-emotion", "auto-pause"):
        item = sub.add_parser(command)
        item.add_argument("--text-file", type=Path, required=True)
        if command == "auto-pause":
            item.add_argument("--out", type=Path, required=True)
    item = sub.add_parser("submit")
    item.add_argument("--request", type=Path, required=True)
    item.add_argument("--job", type=Path, required=True)
    item.add_argument("--source", choices=["mine", "official"], default="mine")
    item.add_argument("--approval", required=True, help="已取得的文案/音色/本次调用确认依据")
    item = sub.add_parser("status")
    item.add_argument("--task-id", required=True)
    item = sub.add_parser("batch-status")
    item.add_argument("--task-ids", nargs="+", required=True)
    item = sub.add_parser("wait")
    item.add_argument("--job", type=Path, required=True)
    item.add_argument("--out", type=Path)
    item.add_argument("--seconds", type=float, default=45)
    item.add_argument("--interval", type=float, default=3)
    sub.add_parser("history")
    sub.add_parser("configure", help="加密保存环境变量中的凭据，并立即同步自训练音色")
    for command in ("sync-voices", "cached-voices"):
        item = sub.add_parser(command)
        item.add_argument("--official", action="store_true")
        if command == "cached-voices":
            item.add_argument("--keyword")
            item.add_argument("--grade", choices=["premium", "ordinary", "custom"])
            item.add_argument("--version", choices=["v3", "v4", "V3", "V4"])
    item = sub.add_parser("set-webhook")
    item.add_argument("--url", required=True, help="空字符串清空；会改变该Key唯一回调地址")
    item.add_argument("--confirmed-account-change", action="store_true")
    item = sub.add_parser("verify-webhook")
    item.add_argument("--payload", type=Path, required=True)
    item.add_argument("--signature", required=True)
    args = parser.parse_args(argv)
    key, secret = "", ""
    try:
        number(args.timeout, "timeout", 1, 60)
        if args.account is None:
            active = args.root / "dubbingx" / "active-account.json"
            args.account = load_json(active).get("account") if active.exists() else "main"
        directory = account_dir(args.root, args.account)
        if args.command not in {"history", "cached-voices"} or os.environ.get("DUBBINGX_API_KEY"):
            key, secret = credentials(directory)
        if args.command == "history":
            if key:
                directory = account_dir(args.root, args.account, key)
            result = history(directory)
        elif args.command == "cached-voices":
            if key:
                directory = account_dir(args.root, args.account, key)
            path = directory / ("official-voices.json" if args.official else "my-voices.json")
            result = load_json(path) if path.exists() else {"voices": [], "note": "尚未同步云端音色；配置凭据后执行sync-voices"}
            if args.keyword or args.grade or args.version:
                result["voices"] = [v for v in result["voices"] if
                    (not args.keyword or args.keyword.casefold() in (str(v.get("name", "")) + " " + str(v.get("description", "")) + " " + str(v.get("id", ""))).casefold()) and
                    (not args.grade or v.get("grade") == args.grade) and
                    (not args.version or str(v.get("version", "")).lower() == args.version.lower())]
                result["note"] += "；当前为本地筛选结果"
        elif args.command == "verify-webhook":
            if not secret:
                raise TtsError("需设置DUBBINGX_API_SECRET；它与API Key不是同一个值")
            result = {"signature_valid": verify_webhook(load_json(args.payload), secret, args.signature)}
            if not result["signature_valid"]:
                raise TtsError("Webhook验签失败，不得据此记录成功或下载")
        else:
            client = Client(key, args.timeout)
            directory = account_dir(args.root, args.account, key)
            if args.command == "configure":
                if not os.environ.get("DUBBINGX_API_KEY"):
                    raise TtsError("configure从环境变量导入新凭据；已有加密凭据用sync-voices")
                path = directory / "credentials.dpapi.json"
                if path.exists():
                    raise TtsError("已有加密凭据，不能覆盖；换账号别名或用sync-voices")
                cipher = dpapi(json.dumps({"api_key": key, "api_secret": secret}).encode())
                result = cache_voices(client, directory)
                write_json(path, {"schema_version": 1, "protection": "windows-dpapi-current-user",
                           "ciphertext": base64.b64encode(cipher).decode()}, exclusive=True)
                write_json(args.root / "dubbingx" / "active-account.json", {"schema_version": 1, "account": args.account})
                result = {"credentials": "已加密保存", "voice_snapshot": result}
            elif args.command == "sync-voices":
                result = cache_voices(client, directory, args.official)
            elif args.command == "voices":
                filters = {k: v for k, v in {"keyword": args.keyword, "grade": args.grade,
                           "gender": args.gender, "ageGroup": args.age_group}.items() if v is not None}
                result = voice_pages(client, not args.official, args.all_pages, **filters)
            elif args.command == "emotions":
                result = client.post("/v1/getEmotionList/" + identifier(args.voice_id, "音色ID"))
            elif args.command in {"analyze-emotion", "auto-pause"}:
                text = args.text_file.read_text(encoding="utf-8-sig")
                if not text.strip():
                    raise TtsError("文本为空")
                result = client.post("/v2/analyzeEmotion" if args.command == "analyze-emotion" else "/v2/autoPause", {"text": text})
                if args.command == "auto-pause":
                    if not isinstance(result, str):
                        raise TtsError("停顿响应不是文本")
                    args.out.parent.mkdir(parents=True, exist_ok=True)
                    with args.out.open("x", encoding="utf-8") as output:
                        output.write(result)
                    result = {"path": str(args.out.resolve()), "status": "需审阅停顿，未自动提交"}
            elif args.command == "submit":
                result = submit(client, load_json(args.request), args.job, args.account, args.approval, args.source)
            elif args.command == "status":
                result = client.post("/v1/getTtsTaskInfo/" + identifier(args.task_id, "任务ID"))
            elif args.command == "batch-status":
                result = client.post("/v1/getTtsTaskListInfo", [identifier(v, "任务ID") for v in args.task_ids])
            elif args.command == "wait":
                number(args.seconds, "seconds", 0, 45)
                number(args.interval, "interval", 1, 30)
                job = load_json(args.job)
                if job.get("account") != args.account:
                    raise TtsError("任务账号别名不匹配")
                result = wait_job(client, args.job, directory, args.out, args.seconds, args.interval)
            elif args.command == "set-webhook":
                if not args.confirmed_account_change:
                    raise TtsError("先取得修改账户唯一Webhook地址的明确授权")
                if args.url:
                    parsed = urllib.parse.urlsplit(args.url)
                    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                        raise TtsError("Webhook须为公网http/https地址")
                    if parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
                        raise TtsError("Webhook不能使用本机地址")
                    try:
                        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
                    except OSError as exc:
                        raise TtsError("Webhook主机DNS解析失败") from exc
                    if parsed.username or parsed.password or not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
                        raise TtsError("Webhook必须使用无嵌入凭据的公网地址")
                result = client.post("/v1/setWebhookUrl", {"callbackUrl": args.url})
        print(json.dumps(safe_output(result, key, secret), ensure_ascii=False, indent=2))
        return 0
    except (TtsError, OSError, ValueError, KeyError, TypeError) as exc:
        message = safe_output(str(exc), key, secret)
        print(f"错误: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
