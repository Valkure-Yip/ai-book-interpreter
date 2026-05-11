# User Journeys

## Persona 1：学者本人

> "我要读一本德语哲学新著，想先用 AI 译成中文 + 思维导图，自己边读边校。"

**典型流程**：
1. 拿到 EPUB → `abi survey book.epub -o ./preview` 先花几分钟产出概要
2. 浏览 `preview/overview.md` 和思维导图，决定要不要花钱翻全书
3. `abi translate book.epub -o ./full --mode bilingual,annotated --quality high`
4. 用 IDE/Obsidian 打开 `bilingual.md`，逐章读，遇到问题时检查 `report.md` 的 flagged 列表
5. 想改某个术语：`abi glossary edit <book-id>`，改后系统**自动重跑受影响段落**（按 glossary diff）

**关键诉求**：术语稳定、双语对照、可控成本（hard cap）。

## Persona 2：翻译公司预处理

> "我们做学术书的中译版，AI 出初稿，人工精修。"

**典型流程**：
1. 公司内有一份"全局术语库"（多本书共享） → `abi translate ... --glossary-import ./shared-terms.json`
2. 用 `--mode bilingual` 出双语稿
3. 译者在 InDesign/Word 里精修
4. 反向：把人工修正后的术语回写到全局库

**关键诉求**：
- 一致的术语库管理（v0.2 路线）
- 高质量初稿（`--quality high`）
- 可批量处理（v0.3 路线：`abi batch ./books-dir/`）

## Persona 3：读书爱好者

> "我想读一本英文畅销书的中文版，但还没出版。"

**典型流程**：
1. `abi translate book.epub` 默认参数
2. 直接看 `translated.md`
3. 不关心成本细节，但希望默认配置就合理

**关键诉求**：开箱即用、单命令、好的默认值。

## Persona 4：开发者 / 智能体二次开发

> "我要把 abi 作为 SDK 集成到自己的工作流里。"

**典型流程**：
```python
from abi import translate, RunConfig

result = translate(
    "book.epub",
    config=RunConfig(target="zh", mode=["translated"]),
)
print(result.report.flagged)
for unit in result.translations:
    ...
```

**关键诉求**：稳定的 Python API、清晰的数据模型（pydantic 直接可用）、可观察事件流可订阅。

## 非目标（明确不做）

- **不做实时交互翻译**（这是流水线，不是聊天）
- **不做风格化创作**（学术保真优先；不为"读着顺"牺牲准确）
- **不做版权破解**（拒绝处理明显有 DRM 的输入）
