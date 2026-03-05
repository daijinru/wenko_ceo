# Memory 专项 Roadmap

> 基于对 `MemoryService`、`extract_and_save`、`_persist_preferences`、`WorkingMemory`、`ContextService`、`PerceptionBus` 及整体认知循环的源码审计，梳理 Memory 子系统的现状问题与演进路线。

---

## 现状总结

### 当前架构

系统实现了两层 Memory 机制：

```
Long-term Memory（持久化，跨会话）
  MemoryService → SQLite memories 表
    ├─ 自动提取：extract_and_save — 关键词匹配 LLM 响应
    ├─ 偏好持久化：_persist_preferences — PerceptionBus 统计 → 写入偏好
    └─ TUI 手动管理

Working Memory（瞬态，单次 CO 执行内）
  WorkingMemory Pydantic 模型 → co.context["working_memory"]
    ├─ ContextService.compress_to_working_memory() 生成
    └─ 在 build_prompt() 中替代冗长 findings
```

### 记忆写入的三条路径

| 路径 | 触发时机 | 决策者 | 存储 category |
|------|----------|--------|---------------|
| `extract_and_save` | 每步 LLM 响应后 | 13 个指示短语的文本匹配 | 统一 `lesson` |
| `_persist_preferences` | 任务完成/暂停/中止时 | PerceptionBus 统计 + 阈值规则 | `preference` |
| TUI 手动 | 用户主动操作 | 人工 | 任意 |

### 记忆检索

`retrieve(query, limit=5)` 对最近 100 条记忆做多因子关键词评分：
- 全文匹配 +3.0，分词匹配 +1.5/+0.5，标签匹配 +2.0/+1.0
- 支持 jieba 中文分词，不可用时退化为空格切分

### 记忆消费

认知循环中每步检索 top 3 记忆注入 prompt 的 `## Relevant Memories from Past Events` 区域；Planning 阶段同样检索 top 3 辅助规划。

---

## 问题清单

按严重程度排序：

### P0 — 噪音污染（影响全局检索质量）

**问题 1：`extract_and_save` 误触发率极高**

`"always"`、`"never"`、`"重要"` 等 indicator 在 LLM 正常技术推理中极其常见：

```
"This function always returns a list"       → 命中 "always"，存入
"You should never mutate state directly"    → 命中 "never"，存入
"重要的是文件路径要正确"                      → 命中 "重要"，存入
```

这些不是值得跨会话持久化的知识，而是当步推理的噪音。结果是 `memories` 表被低价值内容淹没，反过来污染 `retrieve_as_text` 注入 prompt 的质量。

**问题 2：内容截断粗暴**

`llm_response[:200]` 硬截断。LLM 响应通常很长，有价值的洞察可能在中部或尾部。加上 `From '{step_title}': ` 前缀，实际可用字符更少。

### P0 — 架构违规（影响可维护性）

**问题 3：`extract_and_save` 不应存在于 MemoryPlugin 协议中**

`MemoryPlugin` 的定位是 "Pure data — perception conclusions stored by kernel"，但 `extract_and_save` 内含决策逻辑（判断什么该记住）。这违反了内核/插件的划分原则——"判断该不该做"属于内核或编排层，"存取数据"才属于插件。

当前 `MemoryPlugin` Protocol 定义：

```python
class MemoryPlugin(Protocol):
    def save(self, category, content, tags, source_co_id) -> Any: ...
    def retrieve_as_text(self, query, limit=5) -> List[str]: ...
    def extract_and_save(self, co_id, llm_response, step_title) -> Any: ...  # ← 不属于此处
```

### P1 — 语义丢失（削弱四分类设计意图）

**问题 4：自动提取统一写为 `lesson`**

模型定义了四种 category（`preference` / `decision_pattern` / `domain_knowledge` / `lesson`），但 `extract_and_save` 将所有命中统一归为 `lesson`。这导致：

- 检索时无法按 category 过滤或加权
- TUI 中自动提取的记忆视觉上无法区分
- 语义重叠：LLM 说 "user prefers X" 被存为 `lesson` 而非 `preference`

### P1 — 高质量信息丢失

**问题 5：WorkingMemory 不流入长期记忆**

`WorkingMemory` 的 `key_findings` 和 `failed_approaches` 是经过 LLM 提炼的结构化信息，质量远高于 `extract_and_save` 的粗暴截断。但任务完成后这些信息随 CO 一起沉没，不会写入持久记忆。

### P2 — 检索天花板

**问题 6：只扫描最近 100 条**

```python
all_memories = self.session.query(Memory).order_by(Memory.created_at.desc()).limit(100).all()
```

随着使用时间增长，早期高价值记忆永远检索不到。这与"跨会话持久化"的设计初衷矛盾。

**问题 7：无时间衰减**

旧记忆和新记忆在评分时权重相同，近期记忆通常更相关但未获得加权。

### P2 — 偏好管理缺陷

**问题 8：`_persist_preferences` 去重太弱**

```python
existing = memory.retrieve_as_text(f"preference {tool}", limit=1)
if existing and tool in existing[0]:
    continue
```

用关键词检索做去重，而非精确标签查询。且只跳过、不更新——如果用户行为从 reject 转变为 approve，旧的错误偏好仍然存在。

**问题 9：偏好无过期机制**

一旦写入永远有效，但用户行为会随时间变化。

---

## 演进路线

### Phase 1：止血 — 降噪与架构归位 ✅

> 目标：让自动提取不再产生垃圾，让 MemoryPlugin 回归纯数据定位。

#### 1.1 提取逻辑从 MemoryPlugin 分离

将 `extract_and_save` 的**判断逻辑**上移至 ExecutionService（编排层），MemoryPlugin 只保留纯 CRUD。

变更前：
```
ExecutionService → memory.extract_and_save(co_id, response, title)
                   MemoryService 内部做 indicator 匹配 + save
```

变更后：
```
ExecutionService → MemoryExtractor.evaluate(response, title)
                   返回 {worth: bool, category, content, tags}
               → 若 worth: memory.save(category, content, tags, co_id)
```

`MemoryExtractor` 作为 ExecutionService 的内部组件或独立 utility，不属于 Plugin 层。同步更新 `MemoryPlugin` Protocol，移除 `extract_and_save` 方法。

#### 1.2 关键词预过滤 + 上下文窗口

改进当前的纯关键词匹配：

- **收紧 indicator 列表**：移除高频误触发词（`"always"`, `"never"`, `"重要"`），保留低误触发的复合短语（`"user prefers"`, `"lesson learned"`, `"remember that"`, `"用户偏好"`, `"经验教训"`）
- **上下文窗口提取**：命中 indicator 后，不截断整个 response 的前 200 字符，而是提取包含 indicator 的**那个段落**（前后各扩展 1-2 句）
- **频率限制**：同一 CO 内对同一 indicator 最多触发 N 次，防止高频推理步骤批量写入

#### 1.3 Indicator → Category 映射

建立 indicator 到 category 的显式映射，恢复四分类的设计意图：

```python
INDICATOR_MAP = {
    "preference": ["user prefers", "用户偏好", "用户倾向", "偏好"],
    "decision_pattern": ["pattern", "规律", "总是这样", "decision pattern"],
    "domain_knowledge": ["important to note", "domain knowledge", "领域知识"],
    "lesson": ["lesson learned", "remember that", "经验教训", "教训"],
}
```

---

### Phase 2：增值 — WorkingMemory 联动与偏好治理 ✅

> 目标：让高质量信息不再丢失，让偏好记忆可靠演化。

#### 2.1 WorkingMemory → 长期记忆桥接 ✅

在任务完成（`task_complete=True`）时，将 WorkingMemory 中的结构化信息写入长期记忆：

| WorkingMemory 字段 | 写入 category | 条件 |
|---------------------|---------------|------|
| `failed_approaches` | `lesson` | 每条作为独立记忆 |
| `key_findings` | `domain_knowledge` | 过滤掉纯过程性描述 |
| `open_questions` | 不写入 | 未解决的问题不适合固化 |

这些内容已经过 LLM 压缩和结构化，质量远高于 `extract_and_save` 的原始截断。后者在 Phase 3 引入 LLM 判断后可进一步精炼。

#### 2.2 偏好精确去重与更新 ✅

将 `_persist_preferences` 的去重逻辑从关键词检索改为精确标签查询 + 更新：

```python
# 变更前
existing = memory.retrieve_as_text(f"preference {tool}", limit=1)
if existing and tool in existing[0]:
    continue

# 变更后
existing = memory.query_by_tags(["implicit_preference", tool])
if existing:
    memory.update(existing[0].id, content=new_content)  # 更新而非跳过
else:
    memory.save(...)
```

需要在 MemoryService 中新增 `query_by_tags` 方法，直接用 JSON 字段查询。

> **前置修正**：当前 `Memory.relevance_tags` 在 ORM 中声明为 `Mapped[Dict[str, Any]]`，但实际使用方式始终是 `list[str]`。实现 `query_by_tags` 前需先将类型声明修正为 `Mapped[List[str]]`，并确认 SQLite JSON 查询使用 `json_each()` 做标签匹配。

#### 2.3 Memory 模型扩展 ✅

为 `Memory` ORM 模型新增字段：

```python
updated_at: Mapped[datetime]     # 记忆最后更新时间
access_count: Mapped[int] = 0    # 被检索命中的次数（用于后续衰减/清理）
```

`updated_at` 在 `update()` 时自动刷新，`access_count` 在 `retrieve()` 命中时递增。为后续的时间衰减和记忆清理提供数据基础。

---

### Phase 3：精炼 — LLM 驱动的记忆判断 ✅

> 目标：用 LLM 替代关键词匹配，让 "什么该被记住" 的判断达到人类水平。

#### 3.1 LLM 记忆评估器 ✅

已实现：`MemoryExtractor` 升级为关键词预筛 + LLM 精判的两阶段流程。

```
关键词预过滤（Phase 1 保留，作为成本控制的粗筛）
    ↓ 命中
LLM 精判（evaluate_with_llm）
    输入: 段落内容 + step_title + co.title
    输出: {
        worth: bool,
        category: "preference" | "lesson" | ...,
        content: "精炼后的记忆内容",
        tags: ["tag1", "tag2"]
    }
    ↓ worth=true → memory.save(LLM 精炼结果)
    ↓ worth=false → 不保存（撤销频率计数）
    ↓ LLM 失败 → fallback 到规则引擎结果
```

实现细节：
- `LLMService.judge()` 使用 secondary 模型（max_tokens=512, temperature=0.2）
- `LLMService.parse_judge()` 解析 `` ```judge``` `` fenced JSON block
- `LLMPlugin` Protocol 新增 `judge()` / `parse_judge()` 签名
- `MemoryExtractor` 接受可选 `llm: LLMPlugin` 依赖，无 LLM 时退化为纯规则
- `MEMORY_JUDGE_PROMPT` 中文系统提示，指导 LLM 区分四种 category 与不值得记住的噪音
- **降级策略**：LLM 调用超时/失败/解析失败时，fallback 到 Phase 1 的规则引擎结果
- 关键词未命中时不调用 LLM，零额外开销

#### 3.2 记忆去重与合并 ✅

已实现：`MemoryExtractor.deduplicate()` 在保存前检索 top-3 相似记忆，用 LLM 判断去重策略。

```
evaluate_with_llm() 返回 extraction
    ↓
deduplicate(extraction, memory)
    ├─ retrieve_as_text(content, limit=3) 检索相似记忆
    ├─ 无结果 → 返回 None（正常新增）
    └─ 有结果 → LLM merge_judge 判断
        ├─ "skip"  → 完全重复，丢弃
        ├─ "update" → 主题相同信息互补，合并到已有记忆
        └─ "new"   → 主题不同，正常新增
```

实现细节：
- `LLMService.merge_judge()` 使用 secondary 模型（max_tokens=512, temperature=0.2）
- `LLMService.parse_merge_judge()` 解析 `` ```merge``` `` fenced JSON block
- `MEMORY_MERGE_PROMPT` 中文系统提示，指导 LLM 区分 skip/update/new
- 合并时 LLM 生成融合新旧信息的 content，通过 `memory.update()` 写回
- **降级策略**：LLM 失败时返回 None（正常新增，不丢数据）
- ExecutionService Step 10 流程：evaluate_with_llm → deduplicate → save/update/skip

---

### Phase 4：检索升级

> 目标：突破关键词匹配的天花板，实现语义级检索。

#### 4.1 去除 100 条硬上限 ✅

- ~~短期：将 `limit(100)` 改为配置项 `config.memory.scan_limit`，默认值提升至 500~~
- 已实现：新增 `MemoryConfig.scan_limit` 配置项，`retrieve()` 读取配置而非硬编码
- 在 `content` 列创建索引加速扫描

#### 4.2 时间衰减评分 ✅

在现有评分基础上引入时间因子（已实现）：

```python
days_ago = (now - mem.created_at).days
time_factor = decay_base ** (days_ago / half_life_days)
final_score = raw_score * time_factor
```

`decay_base` 和 `half_life_days` 可配置（默认 `0.5` 和 `90`，即 90 天半衰期）。

结合 Phase 2 新增的 `access_count`，被频繁命中的记忆可获得额外加权，抵消时间衰减。

#### 4.3 SQLite FTS5 全文搜索

为 `memories` 表创建 FTS5 虚拟表，支持中文分词的全文索引：

```sql
CREATE VIRTUAL TABLE memories_fts USING fts5(
    content, relevance_tags,
    tokenize='unicode61'
);
```

> **中文分词注意**：`unicode61` tokenizer 按 Unicode 边界切分，对中文仅能做到单字粒度，无法进行语义分词。实现时需在应用层保持 jieba 预分词后写入 FTS5，或研究自定义 tokenizer 方案。

`retrieve()` 改为先走 FTS5 粗召回，再用当前评分逻辑精排。

#### 4.4 向量检索（长期）

引入 embedding 向量，使用 `sqlite-vec` 扩展或外接向量数据库（如 ChromaDB）：

- Memory 模型新增 `embedding: Mapped[bytes]` 字段
- 写入时生成 embedding（使用经济 embedding 模型）
- 检索时用 cosine similarity 召回，再用关键词评分 + 时间衰减做精排

这是最终方案，但依赖外部 embedding 服务，优先级最低。

---

## 里程碑总览

```
Phase 1  止血                        Phase 2  增值
┌──────────────────────────┐        ┌──────────────────────────┐
│ 1.1 提取逻辑从 Plugin 分离 │        │ 2.1 WorkingMemory 桥接    │
│ 1.2 收紧 indicator + 上下文│        │ 2.2 偏好精确去重与更新     │
│ 1.3 Indicator→Category 映射│        │ 2.3 Memory 模型扩展       │
└──────────────────────────┘        └──────────────────────────┘
            │                                    │
            ▼                                    ▼
Phase 3  精炼 ✅                      Phase 4  检索升级
┌──────────────────────────┐        ┌──────────────────────────┐
│ 3.1 LLM 记忆评估器 ✅      │        │ 4.1 去除 100 条硬上限 ✅   │
│ 3.2 记忆去重与合并 ✅      │        │ 4.2 时间衰减评分 ✅        │
│                          │        │ 4.3 FTS5 全文搜索          │
│                          │        │ 4.4 向量检索（长期）        │
└──────────────────────────┘        └──────────────────────────┘
```

Phase 1-2 可并行推进，无相互依赖。Phase 3 依赖 Phase 1 的架构分离（`MemoryExtractor` 作为升级对象）。Phase 4 独立于 Phase 1-3，可按需穿插。

---

## 与主 ROADMAP 的关系

| 本文档 | 主 ROADMAP 对应项 |
|--------|-------------------|
| Phase 1.1 架构归位 | Milestone 1 — MemoryPlugin 协议纯化 |
| Phase 3.1 LLM 评估器 | Milestone 2 — 多模型路由（secondary 模型） |
| Phase 4.3 FTS5 | Milestone 2 — 记忆检索升级 |
| Phase 4.4 向量检索 | Milestone 2 — 记忆检索升级（长期） |
| Phase 2.1 WorkingMemory 桥接 | Milestone 3 — 上下文压缩优化的延伸 |
