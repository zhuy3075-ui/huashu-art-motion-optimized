# DubbingX异步配音与音色历史

本页为16号；火山配音及逐字时间轴继续见[14号](14-火山TTS与逐字字幕.md)。新旁白先沿用用户已选平台，未定时结合已有音色库与字幕需求给两种具体选择。凭据、音色ID、任务和历史按平台隔离；不自动切换、并行生成或复用另一平台的时间戳。DubbingX仅生成音频，字幕要基于这条实际音频另行转录/对齐；缺工具时停止字幕同步环节并说明缺口。

依据：[用户指定的中文异步API文档](https://doc.dubbingx.com/guide/TTSAsync.html)，首次读取2026-10-07，本次2026-10-08重新核对；已读音色/情绪、可选文本处理、Webhook与三项任务操作全部章节。实现使用V1 JSON合成、V2音色列表，默认轮询。脚本为`scripts/dubbingx_tts.py`，只依赖Python标准库；MP3校验另需ffprobe，WAV可原生测时长。

## 1. 需求阶段必须问声音来源和具体音色

涉及新生成旁白时，在需求简报中**必须询问本片是否使用用户的自定义音色，以及具体选哪个ID**；用户已经明确给出相同范围的选择直接记录，不重复问。泛称“全部你决定”“直接做”不能消掉本技能的音色选择问题；用户在这个问题之后明确授权“音色由你从这些候选中选”才可代选，并记录候选范围与依据。不得自动用官方声音替代自己的声音。

不问“要什么声音”后让用户自己研究。先结合用途给具体追问，例如：“你这支片子强调亲历经验，是否用你在DubbingX的自定义音色？可以指定ID，也可以从下面历史用过的声音选；如果希望更像新闻播报，我们再比较库里可用的另一种声音。”根据材料改写，不固定套模板。

提供候选时依次使用：

1. 用户本次已指定的ID与已有声音材料。
2. 本账号`history`命令返回的历史成功音色：展示名称、**原始ID**、上次语言/情绪/语速、最近时间，以及仍存在的本地试听文件。说明“生成过，不等于你已认可”。
3. 先读`cached-voices`取得已下载的云端自训练音色快照，标明同步时间；需要更新时`sync-voices`，也可`voices --all-pages`实时查询。候选须来自实际接口，不能把文档示例ID当用户音色。没有可用凭据时只能读本地快照/历史，不能冒称刚查询过云端。**新凭据配置完成后先同步云端ID、名称、介绍与voiceUrl，让用户选择，再谈合成。**

询问采用自定义音色的选择不可跳过，但无旁白、纯风格分析、仅画面修改不启动此关卡。用户已给成品口播并要求沿用时直接用原音频，不重配、不额外调用云端；记录已有音轨作为声音来源。没有历史记录就直说暂无历史，不编造。

选中历史ID后，提交前重新查询当前库和情绪。历史ID可能已失效/不可访问；向用户说明具体缺口并让其换选，不能偷偷换ID。CLI默认查自训练库，选官方声音必须用户明确选择并使用`--source official`。平台的`grade=custom`代表多情绪，**不等于用户的自训练音色**；查询自己的音色用`isMyModel=true`。

`--official`/`source=official`是接口默认公共目录范围，真实列表也可能包含`isOfficial=false`的共享条目，不能都称官方发布；实际来源保留原字段。同名音色可能有不同ID/模型版本，给候选时同时展示名称、ID和版本，不按名称自动取第一项。

文案放行前可查声音和情绪、展示平台试听，不生成正式TTS。以已确认文案片段进行一次已授权短试听；先确认声音与语速，再按00号样片关卡生成整段/整片。候选数量根据需要，不能未经授权批量合成试听。声线选择与实际试听认可分别记在`创作确认.md`；保存voiceId、名称、来源、账号别名、模型版本、情绪、语言、音高/语速/增益、文案版本及确认原话/位置。

## 2. 凭据与私有数据

- 用户在本机设置`DUBBINGX_API_KEY`，不用把真实密钥发在对话里。仅验Webhook时需要对应的`DUBBINGX_API_SECRET`，它不是API Key。Windows可用`configure`从环境变量导入，采用DPAPI CurrentUser加密保存，之后无需每次输入；其他系统仅用环境变量，不回退明文。已有凭据不覆盖，换账号/Key用新别名。
- 不将**明文**密钥写进技能、JSON、命令行参数、历史、Git或日志；凭据JSON只含DPAPI密文。请求中的Authorization只发往固定API地址，拒绝重定向。音频下载单独发起，不携带Bearer。
- 全局参数放子命令前：`--root <配置目录>`、`--account <账号别名>`、`--timeout 30`。根目录沿用13号的CODEX_HOME/config规则，但音色历史独立放在`dubbingx/<账号别名>/history/`，不修改视觉Style DNA。configure成功后在`dubbingx/active-account.json`保存当前账号别名；省略--account则使用它，尚未配置才默认main。
- 账号目录只保存Key的SHA-256绑定指纹，不保存Key；相同别名换Key会拒绝混用，轮换Key时暂用新别名。本地历史无密钥也可查，但不代表当前账号仍有访问权。
- 历史只存声音元数据、生成参数、任务ID、时间、确认依据、音频路径/摘要/实测时长；不存完整文案或有时效签名的下载URL。云端候选另存`my-voices.json`与`official-voices.json`，不是生成历史；快照含原始音色ID/名称/介绍/试听地址，私人音频、凭据、快照、历史与账号目录均不进入可分享技能包。

## 3. 正常调用

以下命令路径相对技能目录；Codex执行时替换为当前机器上的真实绝对脚本路径。所有示例ID都须替换为用户选择的实际ID，模板不能直接提交。

```powershell
python scripts/dubbingx_tts.py --account my-library configure
python scripts/dubbingx_tts.py --account my-library cached-voices
python scripts/dubbingx_tts.py --account my-library cached-voices --official --keyword '旁白'
python scripts/dubbingx_tts.py --account my-library sync-voices
python scripts/dubbingx_tts.py --account my-library sync-voices --official
python scripts/dubbingx_tts.py --account my-library history
python scripts/dubbingx_tts.py --account my-library voices --all-pages --keyword '用户指定名称'
python scripts/dubbingx_tts.py --account my-library emotions --voice-id REAL_VOICE_ID
```

configure须先由用户在本机安全地设置环境变量，不在对话/脚本内填写真实密钥。它验证音色查询、同步自训练列表、加密保存凭据并设当前账号；`sync-voices --official`额外同步官方列表，不能把这份快照当自定义音色。cached-voices支持keyword、grade、version本地筛选；每次给用户少量贴合材料的具体候选，完整库可查阅，不一次堆出几百条。API只回试听URL，不把“保存ID”说成下载全部试听音频。

将`assets/dubbingx-request.template.json`复制到项目并填入实际文案、选中voiceId与已确认参数。默认WAV便于测时长，增益0dB、原音高/语速1.0只作建议，不擅自冻结。确定采用云端合成及其费用范围后再提交；API Key已配置不自动代表任意扣费授权。`--approval`记录已经取得的文案、音色及本次生成授权依据，不能为了通过脚本自造确认。脚本结构校验不能证明用户真正同意。

```powershell
python scripts/dubbingx_tts.py --account my-library submit --request '项目/试听请求.json' --job '项目/试听任务.json' --approval '用户在本次对话确认文案v2、ID和生成该试听的原话/位置'
python scripts/dubbingx_tts.py --account my-library wait --job '项目/试听任务.json' --out '项目/试听.wav' --seconds 45
```

任务记录在网络提交前创建；同一`--job`存在即拒绝重复提交。wait每次有界等待，未结束返回`state=waiting`，保留taskId；稍后对同一任务再次wait，先给用户状态，不能靠一直循环等待阻塞沟通。Ready/Generating继续查询；Failed/Canceled停止，不记成功历史。Completed暂没有fileUrl时仍等待，不把空链接当音频；接口/下载失败后只恢复原任务。

Completed下载成功后验证WAV结构或MP3音频轨，记录实测时长与文件摘要，再进历史。首次结果为`generated_only`，不能当成用户满意。历史命令输出可播放的本地路径；展示时用绝对路径的音频Markdown。平台voiceUrl可按应用支持方式提供试听，无法播放时如实说明。

同任务已完成时重复wait核验原音频摘要并补全历史，不重复生成或覆盖音频。音频丢失/被改时报告缺口。CLI不自动覆盖现有文件；任务完成前下载失败可改一个未使用的输出路径再wait。

```powershell
python scripts/dubbingx_tts.py --account my-library status --task-id REAL_TASK_ID
python scripts/dubbingx_tts.py --account my-library batch-status --task-ids TASK_A TASK_B
```

如果提交网络断开、服务器报错或成功响应结构无法解析，记录`submission_unknown`，**不能自动重新POST合成**。到平台找回真实taskId并先status核查；确认对应本任务后由用户/代理将真实ID补入本项目任务JSON的`task_id`，记录找回依据，再wait。找不到ID时说明不确定性，新的提交需要用户接受可能重复扣费。不要将文档示例、traceId或列表中别人的ID补入。

CLI状态输出删去URL查询签名，不能拿打印后的去签名URL下载；wait使用服务原始URL即时下载。音频文件落地后据实生成转录/字幕时间戳；TTS此页没有字级对齐接口，不能假造逐字时间。用已有ASR/对齐工具，缺能力时说明限制。

## 4. 接口和参数速查

所有API调用：固定`https://tts-api.dubbingx.com`，POST JSON，Bearer Key；音频fileUrl下载是另外的GET请求。检查HTTP成功及业务`success=true, code=200`，不能只判断HTTP 200。

| 命令 | 接口 | 载荷/用途 |
|---|---|---|
| voices | `/v2/getTTSTimbreList` | pageIndex/pageSize；自己的库传isMyModel；筛选keyword、grade、gender、ageGroup |
| emotions | `/v1/getEmotionList/{timbre_id}` | ID在路径；POST空对象；返回type/aura，选实时支持的值 |
| analyze-emotion | `/v2/analyzeEmotion` | text；结果是建议，须与所选声音支持的情绪核对 |
| auto-pause | `/v2/autoPause` | text；输出停顿文本草稿，不自动替换原文或提交 |
| submit | `/v1/addTtsTask` | V1平面JSON，字段见下；只发送一次 |
| status / wait | `/v1/getTtsTaskInfo/{taskId}` | taskId在路径；检查状态与音频URL |
| batch-status | `/v1/getTtsTaskListInfo` | 请求体直接是taskId字符串数组，不套对象 |
| set-webhook | `/v1/setWebhookUrl` | callbackUrl；空字符串清空，改变该Key唯一配置 |
| verify-webhook | 本地验签 | 验证回调四个字段的HMAC，不启动HTTP服务器 |

合成字段：voiceId、text、emotion或emotionCustom、language、audioPitch、audioSpeed、audioVolume、fileFormat。支持zh/jp/en/yue/sc/ko；V4仅zh/en，自然语言emotionCustom仅用于V4，不和emotion一起猜测优先级。增益-12至+12dB；WAV/MP3；V1文档未明确音高/语速上下限，脚本只拒绝非正/非有限值，不套用旧英文V2的范围。

全情绪premium选择`类型-风格-档位`，档位1–5；传空允许平台自动识别，是否自动由用户选择。ordinary可省略emotion，显式值为单情绪。custom必须选明确情绪，例如`自定义情绪-常规默认`，不拼强度；脚本兼容文档描述的风格名及示例完整名称，实际以服务返回与联调为准。V4可改用emotionCustom。`grade`、`version`与是否自训练是不同字段。

真实只读联调发现version返回小写v3/v4，脚本按大小写兼容；文档示例大写不成为拒绝真实音色的理由。

text支持中文音素`<phoneme ph="duan2">段</phoneme>`与秒数停顿`<break time="0.15"/>`，单个停顿至多20秒。中文提交示例含多余单引号，不能直接复制。自动停顿后需核对未改变正文与读音；只调停顿不必重审内容观点，实质改文案按00号重新确认。

```powershell
python scripts/dubbingx_tts.py --account my-library analyze-emotion --text-file '项目/已确认片段.txt'
python scripts/dubbingx_tts.py --account my-library auto-pause --text-file '项目/已确认片段.txt' --out '项目/停顿草稿.txt'
```

文档未明确文本上限、请求速率、轮询频率、音频URL有效期及isMyModel数据类型；布尔true按含义实现，需真实账户核对。长文按已确认镜头/自然段拆分，一段一个任务JSON；不自行宣布平台支持无限文本或某个并发数。中文页未给提交返回体，脚本期待`data.taskId`，依据[英文提交示例](https://doc.dubbingx.com/guide/TTSAsync_en.html)；英文页是旧V2 SSML，不拿其路径/载荷覆盖本次中文V1实现。遇到不同返回体停下保留未知任务，不猜ID。

本页API只查询、使用已有音色，并没有提供创建/训练自定义音色接口；新音色先在平台创建，再回技能选ID。

## 5. Webhook完整接入边界

默认轮询不需公网服务。用户明确要求修改回调配置时才调用set-webhook；同Key只有一个地址，可能影响别的应用。必须提供公网http/https接收地址，不能用localhost或私网。脚本不自动部署服务。

```powershell
python scripts/dubbingx_tts.py --account my-library set-webhook --url 'https://你的公网域名/tts/webhook' --confirmed-account-change
python scripts/dubbingx_tts.py --account my-library verify-webhook --payload '项目/收到的回调.json' --signature 'sha256=收到的十六进制签名'
```

回调为POST JSON，终态Completed/Failed/Canceled，对应task.completed/task.failed/task.canceled。保存实际收到的`X-DubbingX-Signature`；用该Key的api_secret对四行签名原文验HMAC-SHA256：依次taskId、status、fileUrl、timestamp，各行`字段名=值`，LF连接、不加结尾换行，null/缺失转空字符串。**不是对原始JSON直接签名**。

签名成功只证明签名字段完整，不等于任务属于本项目，也不等于没有重放。接收服务还须匹配已知taskId、检查毫秒timestamp新鲜度与业务event/status、按taskId终态幂等；未签名的errorMessage等不能成为执行指令。此CLI只验签，不声称提供重放防护服务器；回调不能直接伪造成功历史。收到后继续用已存任务wait核对平台状态并落地音频，回调未到亦可轮询兜底。Completed且fileUrl=null须重新查询，不自行重新合成。

## 6. 验证与交付

```powershell
python -m unittest discover -s scripts/tests -p 'test_dubbingx_tts.py'
```

测试使用模拟HTTP/临时目录，不消耗额度。真实联调至少核对自己的音色列表、情绪、已批准短试听的提交返回、轮询、文件解码与实际听感。没有真实Key/请求时只报告“脚本和流程已加入、模拟验证通过”，不能声称真实API已成功或音色已获用户认可。
