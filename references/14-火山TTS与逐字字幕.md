# 火山 TTS、逐字时间轴与音色历史

本页只适用于用户已选火山的旁白。DubbingX可选调用见[16号](16-DubbingX异步配音与音色历史.md)，该平台音频不附带本页时间轴。平台未定先结合已有音色库和字幕需要给具体选择；不静默切换平台，不互用凭据/音色ID/任务或另一条音轨的时间戳。

需要新旁白、选择自定义音色、复用历史 ID，或将旁白接入字幕/动画节拍时读本页。脚本为`scripts/volcengine_tts.py`；当前实现火山豆包 2.0 **异步长文本接口**，音频与时间戳来自同一任务。并未安装外部仓库的动画渲染器。

## 需求过程必问声音

结合素材给具体选择，例如：“这段像是你本人在讲经历，是不是沿用你的复刻音色更合适？还是选更客观的官方旁白？我可以先列出你导入和成功用过的 ID。”必须问是否用用户自定义音色及具体 ID；已明确选择不重复问，不能只问男声/女声。先列本账号`voices`与`history`，名称、来源、speaker、资源 ID、可用试听和用户评价分开呈现。历史生成成功不等于用户喜欢。

未选择不合成；针对具体候选的明确委托可以代选并记录依据。已有口播、无旁白、纯风格研究不新配。全文及字幕原文、数字/缩写读法、音色、语速和此次生成范围放行后才能提交；短试听不自动授权整段。技术上脚本要求`--approval`，内容必须引用已经存在的真实确认，不能自造同意。

## 本机凭据与音色库

当前鉴权使用`VOLC_TTS_API_KEY`，从火山控制台 API Key 管理取得。不要把密钥放聊天、请求模板、代码、ZIP或日志。通过本机环境变量设置；Windows可执行`configure`保存为CurrentUser DPAPI密文。其他平台仅环境变量。默认config为`CODEX_HOME/config/huashu-art-motion/volcengine/<账号别名>/`；Key指纹绑定别名，换Key需用新别名。`configure`只本地保存，不代表云端权限验证或收费授权。

```powershell
python scripts/volcengine_tts.py --account my-voice configure
python scripts/volcengine_tts.py --account my-voice import-voices --file '项目/火山音色.json'
python scripts/volcengine_tts.py --account my-voice voices
python scripts/volcengine_tts.py --account my-voice history
```

官方音色资源`seed-tts-2.0`，已在火山控制台完成复刻的自定义音色资源`seed-icl-2.0`。本脚本**不创建/训练音色**。当前提交/查询文档未提供账号音色枚举接口，不能声称自动下载云端音色库；用户从控制台音色库提供 ID 或导出清单，再导入本地。也不把其他平台 ID 当作火山 ID。导入是候选元数据，能否使用仍需实际账号权限和已授权短试听核验。导入文件为数组，例如以下结构，占位符须换真实值：

```json
[{"speaker":"YOUR_SPEAKER_ID","name":"我的叙述音色","source":"custom","resource_id":"seed-icl-2.0"}]
```

可选`preview_url`填实际HTTPS试听地址；未提供就标“尚无试听”。本账号成功任务中保存音色元数据、参数摘要指纹及音频/时间轴/SRT证据；仅有效MP3与完整真实时间轴全部保存成功后记录历史。不自动标记为用户认可。

## 提交与恢复

复制`assets/volcengine-request.template.json`到项目。`segments`每项是一个已确认字幕/镜头节拍；`text`保留展示原文，`spoken_text`可填写**单独确认的读法**，例如数字3→三。真正提交文本是逐项朗读文本直接拼接，因此标点/停顿须显式写在片段内。按完整词组分段；不自动断开英文词或平台返回的多字词组。模板内容仅演示，不能直接当用户文案。

本适配器输出MP3，采样率24000、语速/音量0只作初始建议。火山语速是整数[-50,100]，0=原速、100=2倍速、-50=0.5倍速，不是倍数值。`enable_timestamp`必须true。本实现仅接入已验证结构的字段，不接受未实现的emotion/SSML/发音词典等选项；需要时另行核实官方协议并确认影响。

```powershell
python scripts/volcengine_tts.py --account my-voice submit --request '项目/试听请求.json' --job '项目/试听任务.json' --approval '用户确认全文v2、音色ID与本次短试听的原话/位置'
python scripts/volcengine_tts.py --account my-voice wait --job '项目/试听任务.json' --out '项目/试听.mp3' --seconds 45
```

`submit`先独占保存UUID为`unique_id/task_id`，再调用云端；请求中断后状态为`submission_unknown`，用同一任务`wait`找回，不自动再次提交。查询Running预算最多60秒，单次HTTP另有30秒超时；下载/解码另有上限，因此`--seconds`是轮询预算而非总运行硬期限。超时保留任务并再次`wait`，不会付费重合成。签名下载URL失效时重新查询原任务。结果保留为`任务.json.result.json`，含原始时间戳和临时签名URL，放项目私人目录，不入技能包。

`wait`输出同名MP3、JSON和SRT。JSON有`duration`、`wordList:[{w,s,e,confidence}]`、`sentences`、`captions`、`segments`及`segmentStarts`；单位均为秒。字幕片段不能含空白行，以兼容SRT解析器；有多段文字应拆成多个segments，单换行可用于同条字幕排版。MP3需要ffprobe测时长、ffmpeg完整解码；成功下载不等于有效声音。没有时间戳、时间倒序/越界、文字不完整、实际朗读与提交文字不一致则保留音频/原结果并停止，不能用均分字时长补救，不能自动重新付费。已完成任务验证所有产物哈希后可重复读取，历史不会重复写。

## 字幕与动画使用

SRT字幕是已确认`text`，起止时间直接取该片段对应真实词的首尾。字级`wordList`保留平台实际词组及置信度，不把英文词或多字词均分成伪逐字时间。关键词动作从词表查实际`s/e`；镜头节拍从`segmentStarts`取时间。接入已有`TM.cue`或字幕渲染器时转换其数据结构，保留真实值；不得重新按字符数估时。这是可供引擎消费的时间轴产物，现有示范工程的入口各异，需要在当前项目显式绑定该JSON/SRT；并非自动替换所有示范片的历史字幕。

匹配仅归一全半角、大小写，忽略空白与普通语句标点；保留加减号、百分号、货币和其他有含义符号，不能将`a+b`漏读成`ab`当作完整匹配。数字展开或缩写换读法必须核对；必要时确认`spoken_text`，保留原字幕。若原已生成任务返回不同读法，先审阅实际结果和用户确认，**不修改任务请求绕过指纹校验**。可在项目创建经确认的离线匹配请求，再用原音频与原结果导出，无需重新合成；该离线导出不自动写成功音色历史：

```powershell
python scripts/volcengine_tts.py timeline --request '项目/已确认匹配请求.json' --result '项目/试听任务.json.result.json' --audio '项目/试听.mp3' --out '项目/校正时间轴.json'
python -m unittest discover -s scripts/tests -p 'test_volcengine_tts.py'
```

离线请求的朗读文本须与真实返回文字匹配，字幕展示原文与读法须有确认记录。句/词时间单位是秒，不除以1000；SRT输出才转换成毫秒格式。时轴覆盖不完整应停在样片评审，声音听感仍需用户试听确认。

## 官方协议与边界

核对日期2026-10-08。协议来源：[任务提交](https://docs.volcengine.com/docs/DoubaoVoice/Tasksubmission?lang=zh)、[结果查询](https://docs.volcengine.com/docs/DoubaoVoice/Resultquery?lang=zh)。POST路径`/api/v3/tts/submit`、`/api/v3/tts/query`；`X-Api-Key`、`X-Api-Resource-Id`、随机`X-Api-Request-Id`；业务成功码20000000；查询体`task_id`，状态1/2/3表示运行/成功/失败。复刻音色提交`model=seed-tts-2.0-standard`。异步接口开启`audio_params.enable_timestamp=true`，不混用流式接口的`enable_subtitle`。

官方支持最长10万字符、音频保留7天、签名URL有效1小时；本适配器限MP3及128MiB下载，长片按已确认段落分任务。音频下载使用独立无鉴权HTTPS请求，重定向也校验公网地址。离线测试不代表账号音色可用、收费权限、听感或真实时间戳质量通过；真实短试听仍须火山凭据及用户选定speaker。
