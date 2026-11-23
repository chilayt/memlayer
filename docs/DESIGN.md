# memlayer 设计

## 分层

| 层 | 实现 | 说明 |
|---|---|---|
| 真相源 | memory/<project>/<fact_id>.md | front matter + 正文；可读/可 diff/可重建 |
| 索引 | index.db (SQLite) | facts/events/queue 三表；可丢弃重建 |
| 写队列 | queue 表 | 入队(pending)→写 Markdown→回填索引(done)，崩溃可审计 |
| 幂等 | events.event_id 主键 | 重复投递返回同一 fact_id |
| 偏好变更 | supersede(old,new) | 新事实 + 旧事实 superseded、valid_to 关闭 |
| 遗忘 | retract(id,reason) | retracted + 墓碑；账本先写 + 镜像 + 重放 |
| 视角 | perspective=self/relayed/inferred | inferred 默认未确认 |
| 时效 | valid_from/valid_to | current 查询按 UTC 过滤 |

## 撤回安全链路
1. 账本先写（retract 先落 ledger 再打墓碑）——崩溃可重放修复。
2. 镜像在 store 根外（强制校验）——备份回灌不复活。
3. 启动严格重放（失败 raise）+ fail-closed 阻断读。
4. 同源级联 + 候选阻断。
5. 撤回钩子清嵌入缓存（_close_fact 触发，覆盖所有路径）。

## 检索
- 词项：term overlap + 子查询覆盖加分。
- 语义（可选）：嵌入 cosine，RRF 融合词法+语义。
- 持锁跨外发：召回持锁跨嵌入调用，撤回同锁，已撤原文不出进程。
- top-K 限流：只嵌词法 top-K，钉死持锁上界。
- 全路径返回前 mode-aware 复核（current 只留 current；all 滤 retracted 留 superseded）。

## 桥
- loopback-only（硬校验）。
- 单写者 MutationWriter（队列容量限制）。
- API：/v1/facts(召回) /v1/facts/retract /v1/fact-status(可信确认) /v1/candidates /health。
- /health 带超时获锁，外发期间报 busy 不卡死，暴露语义状态。

## DSH 插件
- shadow 捕获会话事件；active 召回注入（白名单项目）。
- 注入审计（prepared/committed），未审计不注入。
- KILL 开关急停。

## 直接写入的信任边界（2026-11-23）

`POST /v1/events` 会直接产生 confirmed 事实并跳过候选复核。为防止「接口能连通」
被误当成「人工已批准」，该端点现在要求：

1. `trust: "direct"` —— 显式声明这是受信直写；
2. `confirmed` 为布尔值 —— 明确写出是否确认，不再由 perspective 隐式推导。

未声明者返回 400，并提示改用 `/v1/candidates` 的复核流程。界面自动捕获一律走候选。

## 撤回范围契约（2026-11-23）

撤回一个事实时，级联范围 = 同一项目、共享任一 `source_id`、且尚未撤回的**所有版本**
（含 `superseded`）。理由：审计读（mode=all）若仍能返回旧版原文，撤回就没有真正生效。

- 已撤回事实不再从任何 facts 查询返回（`mode=current` 与 `mode=all` 都过滤）。
- 事实文件保留 `tombstone: [RETRACTED: ...]` 与 `status: retracted` 供审计；
  需要看撤回历史时读撤回账本，而不是 facts 接口。
- 回归：`tests/test_retraction.py`（superseded 级联、mode=all 不回传）。

## 语义候选策略（2026-11-23）

`_hybrid_recall` 默认只对词法召回的 top-K 做嵌入，因此**零词法重叠**的同义事实
永远进不了语义候选（这是已知盲区）。可选模式：

- `MEMLAYER_SEMANTIC_CANDIDATES=all`：所有 current 事实都可成为语义候选，
  仅用 `MEMLAYER_SEMANTIC_MIN_SCORE`（默认 0.25）过滤尾部。
- 每次查询最多新增 `EMBED_MAX_NEW=64` 个嵌入，其余先走词法半边，避免长时间持锁。
- 嵌入缓存键含 fact_id/文本哈希/模型/维度；撤回或替换时由钩子清理。
- 默认仍为 `lex`，两应用运行时都显式关闭语义（`MEMLAYER_SEMANTIC=0`），
  外发约定不变。

回归：`tests/test_semantic_candidates.py`（默认盲区 + all 模式命中 + 无关不注入）。

## 本机语义召回

桥由受信的主机配置传入 `--local-embedding` 时，可启用本机 BGE 中文向量模型。创作台将作者原问题作为 `semantic_q`，带作品标题的 `q` 继续供词法检索。检索在作品及作者显式授权的共享范围内进行。低于本机阈值的候选被剔除，每轮最多补一条无词面重叠的高分候选。每次查询最多补算 16 个尚未入索引的向量。

只有事实能在候选台账里找到实际批准记录时，创作台才把它作为已确认的作者写作偏好。现行作品事实、作者本轮要求和工具授权沿用原有规则。

### 本机候选复核

本机模型目录设有 `reranker/` 时，BGE-reranker-v2-m3 会对词法和向量提名中的最多 6 条候选做二次评分。高分候选还需要向量置信度或范围文本支持。部分由两个子句共同表达的弱改写再检查第一子句与记忆范围是否一致。范围判断避免仅因章节、价格或摘要等通用词而召回。一次查询最多补一条无词面重叠的候选。

重排按单对输入运行，避免 INT8 动态量化的得分受同批其他条目影响。加载或推理失败时返回原有本机向量结果，状态可在 `/health` 查看。模型评估只使用本机 CPU，通过当前写作模型生成答案的边界保持在应用配置层。

### 作者确认更正

创作对话可根据作者原话生成适用于本作品的长期偏好候选。候选带作者消息编号，待确认状态不会进入召回。原用法需要更新时，应用可提供同范围旧事实作为建议。作者在界面明确选择新增，或选中旧事实并确认替换。

替换使用 `POST /v1/candidates/approve-replacement`，经同一个写入队列验证候选内容哈希、作品、主题和旧事实状态。旧条目标记为 `superseded`，新条目记录替换关系；重放同一批准只返回原结果。暂存流程在重启时恢复已批准且来源有效的更正。

### 回复反馈与适用例外

创作台将记忆使用反馈绑定到本地会话中的助手回复与当轮实际送入模型的事实 ID。未召回可以记录在回复上。后续在同一任务近期重新命中同一现行规则时，明确标记过的执行问题会带入复核提示。

整理长期候选时可按主题单独查本作品已确认偏好。候选可携带旧事实和新增、替换、例外或重复建议。普通对话仍使用原有召回入口。拒绝原因写入候选的 review_note，后续候选核对可得到相同提议的拒绝原因。

批准例外使用 POST /v1/candidates/approve-exception。桥在现行规则正文后加上经作者确认的适用例外，发布一个现行版本，并保留旧版本及两侧来源。默认和例外作为同一条现行约定进入上下文。继承的来源参与撤回检查，按来源撤回时派生版本也会退出召回。
