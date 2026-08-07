# User Journeys

## 1. 公版书一键翻译

用户有一份公版 TXT/EPUB，希望得到可阅读的中文 EPUB：

```bash
abi make-book ./book.epub --source-target en-zh-Hans --title book
```

系统创建书籍工程和唯一 durable run，动态组织研究、试译、逐章翻译、QA、EPUB、独立评审、随机抽检、
release 与复盘。命令一直运行到 `COMPLETED`，或在预算、HITL、integrity incident 等安全停止点退出。

用户主要关心：一条命令、术语和文体一致、最终 EPUB 能通过阅读器和 EPUBCheck。

## 2. 长任务中断后恢复

进程被终止或网络中断后：

```bash
abi inspect books/zh-Hans/0001_book
abi resume books/zh-Hans/0001_book
```

`inspect` 展示 RunLedger 中的 run、当前 plan、Action/attempt、receipt、gate、incident、预算与下一条安全
命令。`resume` 先执行 reconciliation：已持久化的 receipt/intents 可继续提交；无法证明 exact outcome 的
`RUNNING` attempt 会 fail closed，而不是盲目重跑。

用户主要关心：已完成章节不丢、不重复覆盖 canonical 工件、失败原因和恢复动作可解释。

## 3. 质量缺陷自动收敛

章节 gate、独立评审或随机抽检发现真实缺陷时，validator 输出 typed reason。Registry 将可修复的语义
问题映射到受影响章节的 `chapter.review` 或其他 repair capability；controller 创建新的 plan、Action ID 和
staging。旧失败、review evidence 与 receipt 保留。

用户不需要手动指定固定阶段或从头重跑。达到 semantic repair 上限时系统阻断，避免在 TDD/repair loop
中无限消耗。

## 4. 人工批准一个受限写操作

Action tool 被 HITL policy 暂停时：

```bash
abi inspect PROJECT_ROOT
abi approve PROJECT_ROOT <id> --decision approve
```

`approve` 只接受该 public interrupt 声明的 ordered decision，追加 continuation receipt，并从同一
Action/attempt checkpoint 继续。人工批准不等于业务成功；输出仍要通过 manifest、validator 与 commit。

用户主要关心：批准对象明确、重复提交幂等、拒绝和反馈可审计。

## 5. 预算暂停或 integrity block

预算达到 hard cap 时，run 进入 durable pause。提高额度后，用 `inspect` 给出的命令显式恢复。

identity、checksum、canonical 或 receipt 冲突进入 `BLOCKED`。操作员调查后可以使用：

```bash
abi unblock PROJECT_ROOT \
  --reason "verified canonical resolution" \
  --evidence-ref ticket-42 \
  --source-action <action-id> \
  --resolved-canonical path/to/file:selected:<sha256>
```

unblock 不改写旧事实，而是授权新的 replacement Action。它不能绕过普通 semantic repair。

## 6. 私人自用或已授权内容

```bash
abi make-book ./licensed.epub --mode licensed
abi make-book ./private.epub --mode private_use
```

用户必须提供与模式匹配的版权/授权 evidence。内容会发送到配置的 LLM provider；如需完全离线，使用
本地 OpenAI-compatible endpoint。Langfuse 正文上传默认关闭，可通过 `LANGFUSE_FULL_PAYLOAD` 显式控制。

## 7. 开发者与评测

开发者使用公开 CLI 和 typed ledger reads，而不是直接查询 checkpoint 私有表：

```bash
abi eval trace PROJECT_ROOT
abi eval book PROJECT_ROOT --source-lang en --target-lang zh-Hans
```

L1 检查过程可信度，L2 检查中间工件和逐章译文，L3 检查最终 EPUB/发布物。SDK 级稳定 Python API 当前
不是产品承诺；集成应优先调用 CLI 或 `abi` 的公开 service boundary。

## 非目标

- 不做实时聊天式翻译；
- 不提供跳过门禁的 `--until`、状态写入或任意阶段跳转；
- 不破解 DRM，不替用户判断版权许可；
- 不把 Langfuse、events 或 checkpoint 当作业务真相；
- 不承诺 PDF、DOCX、批量多书和人工术语编辑工作流。
