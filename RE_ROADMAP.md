# Overseer → 小说写作客户端 — 重构路线图

> 将 AI 动作防火墙改造为人机协作的小说写作系统。
> 保留 TUI 终端交互、HITL 机制、内核/插件架构。

---

## 设计哲学

### 核心命题

Overseer 原本解决的问题是"AI 该不该做某件事，谁来判断"。小说写作场景下，这个问题变成：**"这段叙事该不该这样写，谁来判断"**。

两者的底层结构一致——都是一个自主推进的 AI 循环，在关键节点暂停，交由人类决策。区别只在于"推进的是什么"和"决策的依据是什么"。

### 从防火墙到写作系统的语义映射

```
防火墙语义                    写作语义
─────────                    ─────────
AI 发起动作 → 拦截判定        AI 生成叙事 → 一致性审查
风险分级                      创作决策分级
审计日志                      写作历史/版本
自适应规则                    作者偏好学习
```

### 两种笔记方式的融合

系统同时支持两种知识组织方式：

**线性写作**（Plan → Step）：有顺序、有因果的叙事推进，适合"写故事"。

**卡片笔记**（CardPool）：无固定顺序、靠链接产生结构的素材积累，适合"想故事"。

两者的关系是循环的：

```
卡片池 ──素材注入──→ 线性写作
  ↑                      │
  └───── 素材沉淀 ←──────┘
```

写作中产生的好素材回流到卡片池，卡片池中的积累为后续写作提供素材。

---

## 数据模型

### 层级结构

当前结构是扁平的 CO 列表。改造后引入三级层次：

```
Novel（作品）
 ├── CardPool（卡片池，挂在 Memory 上）
 └── Storyline（支线，原 CO）
      └── Plan（大纲）
           ├── Step 1（场景单元）
           ├── Step 2
           └── Step 3
```

### Novel（新增模型）

作品是最顶层容器。世界观、角色设定在此层共享，所有支线隶属于同一部作品。

```
Novel
  id: UUID
  title: str                   # 作品名
  genre: str                   # 类型（奇幻/悬疑/科幻/...）
  synopsis: str                # 简介/梗概
  world_settings: JSON         # 作品级世界观设定
  created_at, updated_at

  关系:
  1:N → Storyline
  1:N → Memory（通过 novel_id 隔离）
```

没有 Novel 层的三个问题：
- 世界观是作品级的，不是支线级的。多部小说的设定会互相污染。
- 支线之间的分叉/合并关系无处挂靠。
- 导出单位是作品，不是散落的支线。

**卡片冷启动：世界观导入**

卡片系统的价值依赖密度。新作品的卡片池是空的，"反向沉淀"和"AI 关联发现"都需要写作推进后才产生素材。解决方案：在 Novel 创建流程中加入世界观导入步骤。

```
Novel 创建流程扩展：
  1. 作者输入标题、类型、简介（现有流程）
  2. 作者输入一段自由文本描述世界观和主要角色（新增）
  3. 一次 LLM 调用，将自由文本拆解为结构化卡片：
     - world_setting × N（魔法体系/科技水平/社会结构...）
     - character_profile × N（每个提及的角色一张）
     - plot_rule × N（从描述中提取的隐含规则）
  4. 作者审阅卡片列表 → 确认 / 删除 / 编辑
  5. 确认后批量写入 Memory（novel_id = 当前作品）
```

复用现有 MemoryExtractor 的 LLM judge 机制——从"对话中提取记忆"改为"世界观描述中提取卡片"，逻辑相似。

### Storyline（原 CognitiveObject，扩展）

每个 CO 对应一条故事支线。

```
CognitiveObject 新增字段:
  novel_id: FK → Novel         # 属于哪部作品
  parent_id: FK → self         # 从哪条支线分叉
  branch_point: str            # 分叉点描述
  pov: str                     # 视角（第一/第三人称）
  tone: str                    # 基调（沉重/轻快/讽刺）
  word_count: int              # 累计字数
```

`context` JSON 扩展为叙事结构：

```json
{
  "goal": "支线的核心冲突/走向",
  "world_state": {
    "time": "...",
    "location": "...",
    "active_characters": [],
    "unresolved_tensions": []
  },
  "plot_beats": [
    {"chapter": 1, "beat": "...", "characters": []}
  ],
  "foreshadowing": [
    {"hint": "...", "target_beat": "...", "resolved": false}
  ],
  "character_arcs": {
    "角色名": {"current_state": "...", "arc_direction": "..."}
  }
}
```

CO 状态语义映射：

| 原状态 | 写作语义 |
|--------|---------|
| `created` | 支线已创建，尚未开始写作 |
| `running` | 正在写作中（AI 推进） |
| `paused` | 暂停（Step 间自动暂停，或人工暂停） |
| `completed` | 支线写作完成 |
| `aborted` | 支线废弃 |

### Step（原 Execution，扩展）

Step 是场景单元，不是段落（粒度介于段落和章节之间）。

```
Execution 新增字段:
  chapter: int                 # 所属章节
  scene_title: str             # 场景标题
  content_text: Text           # AI 生成的正文
  author_draft: Text           # 作者输入的草稿（人工写模式）
  final_text: Text             # 最终定稿文本
  write_mode: str              # author_expand / ai_write / manual_only
  accepted: bool               # 是否被作者接受
```

`write_mode` 三种模式：

| 模式 | 流程 | 适用场景 |
|------|------|---------|
| `author_expand` | 作者写草稿 → AI 扩写 → 作者审阅 | 关键场景，作者有明确想法 |
| `ai_write` | AI 自主生成 → 作者审阅 | 过渡场景，作者信任 AI |
| `manual_only` | 作者全写 → AI 仅更新上下文 | 作者灵感爆发，不需要 AI |

### Memory（扩展 category + 新增链接）

Memory 加入 `novel_id` 做作品隔离：

```
Memory 新增字段:
  novel_id: FK → Novel (nullable)
    - novel_id 非空 → 该作品的专属素材
    - novel_id 为空 → 跨作品的通用偏好
```

category 扩展：

| 原有 category | 保留 | 说明 |
|--------------|------|------|
| `preference` | 是 | 作者的写作偏好 |
| `decision_pattern` | 是 | 审阅习惯 |
| `domain_knowledge` | 是 | 通用知识 |
| `lesson` | 是 | 写作经验 |

| 新增 category | 说明 |
|--------------|------|
| `world_setting` | 世界观设定（魔法体系/科技/社会结构） |
| `character_profile` | 角色档案（外貌/性格/动机/关系） |
| `plot_rule` | 剧情规则（"不可杀死主角"/"第3章揭示真相"） |
| `style_guide` | 文风指南（"用短句"/"模仿鲁迅"） |
| `scene_fragment` | 场景碎片/灵感片段 |
| `dialogue_snippet` | 对话片段 |
| `imagery` | 意象/象征物 |

### MemoryLink（新增模型 — 卡片间链接）

```
MemoryLink
  id: UUID
  source_id: FK → Memory
  target_id: FK → Memory
  relation: str    # extends / contradicts / inspires / same_character / same_location
  created_at
```

卡片之间形成网状结构，支持从一张卡片发现关联素材。

### StepCardRef（新增模型 — Step 引用卡片）

```
StepCardRef
  id: UUID
  step_id: FK → Execution
  card_id: FK → Memory
  usage: str       # material / constraint / inspiration
```

作者可以把卡片"钉"到某个 Step 上，AI 生成时参考被钉的卡片内容。

---

## 内核语义迁移

### 迁移原则

内核/插件分离架构不动。三个内核组件做语义重映射，代码结构保留。

划分标准从"移除后是否还是防火墙"变为：**"移除后是否还能保证叙事质量和作者控制权"**。是 → 插件。不是 → 内核。

### FirewallEngine → NarrativeEngine

五层检查从"安全防护"变为"叙事一致性守卫"：

| 层级 | 原语义 | 新语义 | 机制 |
|------|--------|--------|------|
| L1 | 参数过滤 | **设定一致性** | 检查生成内容是否违反世界观（死人复活、科技矛盾） |
| L2 | 循环检测 | **情节重复检测** | 相似场景/对话模式重复出现时告警 |
| L3 | 权限分级 | **创作决策分级** | 四级权限映射到叙事决策（见下表） |
| L4 | 输出沙箱 | **风格沙箱** | 文风、用词、视角必须符合支线设定 |
| L5 | 元认知熔断 | **叙事质量熔断** | AI 不确定怎么推进时主动请求作者指引 |

创作决策四级分级：

```
AUTO（自动生成，不打断）:
  - 过渡段落（角色移动、场景切换衔接）
  - 环境描写的细节补充
  - 已确定风格的对话续写

NOTIFY（生成后通知）:
  - 新角色首次出场的描写
  - 背景信息补充段落

CONFIRM（需作者确认）:
  - 关键对话（影响角色关系）
  - 情节转折点
  - 伏笔的埋设

APPROVE（需完整预览+审批）:
  - 角色死亡/重大命运转折
  - 支线合并点
  - 结局段落
  - 违反已有设定的内容
```

**关键约束**：这条线画在哪里决定产品成败。写代码时被打断确认一个文件写入不影响效率，但写小说时被频繁打断会摧毁创作心流。AUTO 的覆盖范围必须足够大。

### HumanGate → AuthorGate

保留 asyncio.Event 等待机制。Intent 枚举扩展：

```
现有 Intent:
  APPROVE | REJECT | ABORT | FORCE_ABORT | CONFIRM_COMPLETE | IMPLICIT_STOP | FREETEXT

新增:
  AUTHOR_WRITE      # 进入人工编写模式，提交后 AI 扩写
  AI_WRITE          # AI 全权生成本段
  MANUAL_ONLY       # 人工全写，AI 只更新上下文
  BRANCH            # 从此处分叉出新支线
  REDIRECT          # 接受内容但改变后续走向
  EDIT              # 在 AI 生成基础上人工修改
  ORGANIZE_CARDS    # 暂停写作，先整理卡片素材
```

### PerceptionBus → NarrativePerception

记录/统计的信号从"审批行为"变为"写作行为"：

| 感知维度 | 信号 |
|---------|------|
| 接受率 | 不同内容类型（对话/描写/动作/内心独白）的接受/拒绝率 |
| 叙事节奏 | 连续多段同类型内容时建议变化（全是对话→加入描写） |
| 伏笔追踪 | 埋设时间过长的伏笔提醒回收 |
| 角色平衡 | 某角色消失过久时发出信号 |
| 写作节奏 | 作者选择"人工写"vs"AI写"的比例变化趋势 |

---

## 插件层适配

### LLMPlugin

代码不改，改 PromptPolicy（系统提示词）。从"你是执行AI任务的助手"变为"你是协助人类创作小说的写作伙伴"。

LLMDecision 输出语义变化：

| 原字段 | 新语义 |
|--------|--------|
| `next_action` | 下一个场景的写作方向建议 |
| `tool_calls` | 写作工具调用（write_scene / develop_character 等） |
| `human_required` | 需要作者决策 |
| `confidence` | AI 对当前叙事方向的确信度 |
| `reflection` | 对已写内容的自我评估 |
| `task_complete` | 支线写作完成 |

### PlanPlugin → OutlinePlugin

```
generate_plan()       → 生成章节大纲
subtask               → 场景/Step
checkpoint_reflect()  → 章节结束时的剧情回顾与走向建议
```

**关键**：大纲生成后作者必须能手动编辑——拆分/合并/插入/重排 Step。AI 觉得合适的场景划分和作者脑子里的节奏几乎一定不一样。

### ContextPlugin

最核心的改动。`build_prompt()` 需要组装：

```
当前世界状态
+ 活跃角色档案
+ 未解决伏笔
+ 上几段正文（上下文窗口）
+ 当前 Step 在大纲中的位置
+ 被钉的卡片内容
+ 作者风格偏好
+ NarrativeEngine 输出的约束
```

上下文压缩策略：保留关键剧情节拍和人类决策，压缩过场描写。

**Token 预算分配**：当各类信息争抢 context window 空间时，按固定优先级裁决，而非依赖"智能压缩"。优先级从高到低：

```
NarrativeEngine 输出的约束（硬约束，不可压缩） → 预留 ~500 tokens
当前 Step 大纲 + 被钉的卡片内容                 → 预留 ~1000 tokens
最近 2 个 Step 的 final_text                    → 预留 ~2000 tokens
角色当前状态快照                                 → 预留 ~500 tokens
未解决伏笔列表                                   → 预留 ~300 tokens
远章摘要链                                       → 填充剩余空间
作者风格偏好                                     → 嵌入 system prompt，不占动态空间
```

超出总预算时从底部开始裁剪。具体数值可配置，但优先级顺序固定。这比让 LLM 判断"哪些信息更重要"更可控——写到第 30 章时，第 2 章的伏笔不会因为"太旧"被丢弃。

### ToolPlugin

内置工具替换为写作专用工具集：

| 工具 | 用途 |
|------|------|
| `write_scene` | 生成一个完整场景 |
| `expand_draft` | 基于作者草稿扩写 |
| `develop_character` | 深化角色描写 |
| `create_dialogue` | 生成对话 |
| `describe_setting` | 场景环境描写 |
| `plot_twist` | 生成情节转折建议 |
| `resolve_foreshadowing` | 回收伏笔 |
| `timeline_check` | 检查时间线一致性 |
| `export_chapter` | 导出章节 |

MCP 工具仍可接入（如联网查资料），权限分级不变。

### MemoryPlugin

扩展检索维度：
- 按角色名检索
- 按地点/时间线检索
- 按伏笔状态检索（未解决的伏笔）
- 按卡片链接关系展开关联卡片

---

## Step 交互流程

每个 Step 开始时自动暂停，等待作者选择写作模式。这是系统最核心的交互循环：

```
Step 开始
 │
 ├── 展示：Step 大纲 + 上一段结尾 + 系统推荐的关联卡片
 │
 ├── 作者选择模式：
 │    │
 │    ├─ [1] 人工写 → AI 扩写
 │    │    → 打开编辑区，作者输入草稿
 │    │    → 提交后 AI 扩写（参考上下文 + 卡片 + 风格设定）
 │    │    → 作者审阅扩写结果 → 接受 / 再改 / 重写
 │    │
 │    ├─ [2] AI 全写 → 人审
 │    │    → AI 生成完整场景
 │    │    → 作者审阅 → 接受 / 拒绝 / 编辑
 │    │
 │    ├─ [3] 人工全写
 │    │    → 作者输入完整文本
 │    │    → AI 仅做：更新世界状态、角色位置、伏笔追踪
 │    │    → 自动进入下一个 Step
 │    │
 │    └─ [4] 先整理卡片
 │         → 进入卡片浏览/编辑模式
 │         → 创建、关联、补充卡片
 │         → 整理完毕 → 回到 Step → 选 1/2/3
 │
 └── Step 完成 → 更新世界状态 → 自动暂停 → 下一个 Step
```

### 与现有机制的关系

- Step 暂停复用 `checkpoint_on_subtask_complete` 机制
- 写作模式选择通过 AuthorGate 的新增 Intent 处理
- 模式 1 和 2 中的审阅流程复用现有 HITL 的 APPROVE/REJECT/EDIT 流程
- 模式 3 中 AI 更新上下文复用 `ContextPlugin.merge_step_result()`

---

## TUI 改造

### 主界面（HomeScreen）

```
┌─ 作品/支线 ──────┬─ 正文 ────────────────────────┐
│                  │                                │
│ ▼ 《三体》       │  第三章 · 红岸基地              │
│   ├─ 主线        │                                │
│   │  ├ Ch.1      │  叶文洁站在发射塔前，寒风       │
│   │  ├ Ch.2      │  从大兴安岭的方向吹来。她       │
│   │  └ Ch.3 ●    │  盯着眼前的面板，手指悬在       │
│   ├─ 支线A       │  发射按钮上方......             │
│   └─ 支线B       │                                │
│                  ├────────────────────────────────┤
│ ▼ 《新作》       │  [钉的卡片] 叶文洁 · 红岸基地   │
│   └─ 主线        ├────────────────────────────────┤
│                  │  Step 4/12: "叶文洁做出选择"    │
│                  │                                │
│                  │  [1]我来写  [2]AI写  [3]全手写  │
│                  │  [4]整理卡片                    │
│                  │  > _                           │
└──────────────────┴────────────────────────────────┘
```

左侧面板：Novel → Storyline 的树形结构（用 Textual `Tree` widget）。

右侧上方：正文展示区，支持滚动阅读。

右侧下方：Step 信息 + 写作模式选择 + 交互输入。

### 新增/改造 Screen

| Screen | 说明 |
|--------|------|
| `HomeScreen` | 改造：左侧改为 Novel/Storyline 树，右侧加正文展示区 |
| `NovelCreateScreen` | 新增：创建作品（标题、类型、简介、基础世界观） |
| `CardPoolScreen` | 新增/改造自 MemoryScreen：卡片列表 + 链接图谱双视图 |
| `CardEditScreen` | 新增/改造自 MemoryEditScreen：卡片创建/编辑 + 关联管理 |
| `CharacterScreen` | 新增：角色档案管理（筛选 character_profile 类型的卡片） |
| `TimelineScreen` | 新增：时间线可视化（按 plot_beats 排列） |
| `BranchCompareScreen` | 新增：两条支线从分叉点开始的对比 |
| `OutlineEditScreen` | 新增：大纲编辑（拆分/合并/插入/重排 Step） |
| `ExportScreen` | 新增/改造自 ArtifactListScreen：导出章节/支线/全书 |
| `SystemScreen` | 保留：内核状态展示 |

### 快捷键

| 按键 | 操作 |
|------|------|
| `n` | 新建支线 |
| `N` | 新建作品 |
| `b` | 从当前节点分叉新支线 |
| `s` | 开始/继续写作 |
| `x` | 暂停写作 |
| `e` | 编辑当前段落 |
| `r` | 全屏阅读模式 |
| `o` | 编辑大纲 |
| `m` | 打开卡片池 |
| `c` | 打开角色档案 |
| `t` | 打开时间线 |
| `a` | 导出/查看产出物 |
| `0` | 作品仪表盘 |
| `Space` | 接受当前段落，继续下一段 |
| `j/k` | 上下移动 |
| `f` | 切换过滤 |
| `i` | 系统状态 |
| `q` | 退出 |

---

## 实施阶段

### Phase 1：数据模型 + 最小可用

> 目标：能跑起来，能写一个场景，能选写作模式。

**1.1 新增 Novel 模型**
- `models/novel.py`：Novel ORM
- `database.py`：migration，新增 `novels` 表
- `services/novel_service.py`：Novel CRUD

**1.2 扩展 CognitiveObject**
- 新增 `novel_id`, `parent_id`, `branch_point`, `pov`, `tone`, `word_count` 字段
- Migration 兼容：现有 CO 的 `novel_id` 默认为 NULL（向后兼容）

**1.3 扩展 Execution**
- 新增 `chapter`, `scene_title`, `content_text`, `author_draft`, `final_text`, `write_mode`, `accepted` 字段

**1.4 扩展 Memory**
- 新增 `novel_id` 字段
- 扩展 category 枚举

**1.5 新增 StepVersion 模型**
- 版本历史，每次生成/编辑自动 append
- 附带 `context_snapshot`（Step 完成时快照 Storyline.context，回退时可选同步回退世界状态）

**1.6 修改 PromptPolicy**
- 替换 `FirewallEngine` 中的系统提示词为写作导向
- 正文生成请求加入 `consistency_check` 输出要求（含 character_changes、foreshadowing）
- 解析失败时静默通过 + 记录日志，不暂停写作

**1.7 扩展 HumanGate Intent**
- 新增 `AUTHOR_WRITE`, `AI_WRITE`, `MANUAL_ONLY` 三种 Intent
- Step 开始时自动暂停并展示模式选择

**1.8 $EDITOR 外部编辑器集成**
- 实现 `$EDITOR` 调起逻辑（参考 `click.edit()`）
- Textual TextArea 作为 fallback
- config.yaml 新增 `editor` 配置节（mode: external/builtin, command, template）

**1.9 Novel 创建时世界观导入**
- 作者输入自由文本描述世界观和主要角色
- 一次 LLM 调用拆解为结构化卡片（world_setting / character_profile / plot_rule）
- 作者审阅确认后批量写入 Memory
- 复用 MemoryExtractor 的 LLM judge 机制

**1.10 TUI 最小改造**
- `CreateScreen` 支持创建 Novel + Storyline（含世界观导入步骤）
- `InteractionPanel` 支持三种写作模式选择
- `ExecutionLog` 支持显示长文本正文

### Phase 2：卡片系统 + 阅读 + 全局视图

> 目标：卡片的创建、链接、引用完整可用。阅读和全局视图基础版上线。

**2.1 新增 MemoryLink 模型**
- 卡片间链接关系

**2.2 新增 StepCardRef 模型**
- Step 引用卡片

**2.3 CardPoolScreen**
- 列表视图（基于现有 MemoryScreen）
- 关联卡片展示

**2.4 卡片钉入流程**
- Step 暂停时展示推荐卡片（关键词匹配）
- 作者手动钉卡片到 Step
- AI 生成时注入被钉卡片内容

**2.5 反向沉淀**
- 写作过程中一键提取文本为新卡片

**2.6 ReaderScreen**
- 全屏阅读模式（将支线所有 final_text 按 Step 顺序拼接渲染）
- 基础操作：j/k 滚动、翻页、文首/文末、从阅读位置进入编辑

**2.7 DashboardScreen 基础版**
- 支线进展（Step 完成数）+ 字数统计
- 数据来自已有模型的聚合查询，无需新模型

**2.8 章节完成时生成 ChapterDigest**
- 一次 LLM 调用生成结构化摘要（summary, key_events, character_changes, foreshadowing）
- 存入 Storyline.context.chapter_digests[]

**2.9 版本列表 UI + 回退操作**
- Step 详情中展示 StepVersion 列表
- 回退时提供"仅回退文本" vs "回退文本 + 世界状态"两个选项

### Phase 3：叙事引擎 + 长篇一致性

> 目标：五层检查完成语义迁移，感知系统适配写作场景，长篇上下文管理可用。

**3.1 NarrativeEngine 规则层**
- L2：情节重复检测（文本相似度 difflib/编辑距离，重复度 > 70% 触发）
- L4：风格沙箱（POV 人称代词检查、句长统计）
- 纯 Python 实现，无 LLM 调用

**3.2 NarrativeEngine 批量审查**
- 章节完成后用经济模型做全面审查（矛盾扫描、角色行为一致性、伏笔状态更新）
- 结果存入 NarrativePerception，下一章写作时作为约束注入
- 不阻塞写作过程

**3.3 创作决策分级边界定义 + 自适应**
- L3：AUTO/NOTIFY/CONFIRM/APPROVE 边界定义
- L1 + L5：已内嵌在 consistency_check 中（Phase 1 已实现）
- 根据作者审阅行为自适应调整边界

**3.4 NarrativePerception**
- 内容类型接受率统计（对话/描写/动作/内心独白）
- 叙事节奏检测（连续多段同类型内容时建议变化）
- 伏笔到期提醒（同章 5 Step / 跨章 2 章 → 注入提醒到 prompt）
- 角色出场平衡（消失过久时发出信号）

**3.5 ContextPlugin 改造**
- 近章全文 + 远章摘要链
- Token 预算分配机制（固定优先级裁决，超出预算从底部裁剪）
- 按需原文回溯（needs_recall 解析 → 结构化索引定位 Step → 加载原文 → 重新生成）

**3.6 后台 LLM 卡片关联发现**
- 触发：新卡片创建时 / 作者暂停写作超 30 秒时
- 经济模型判断卡片间潜在叙事联系，结果存入 MemoryLink（source=ai_suggested）

**3.7 偏好持久化**
- 稳定的审阅模式写入 Memory（AI 写描写 AUTO / 对话 CONFIRM）

### Phase 4：TUI 深度改造

> 目标：完整的写作体验。

**4.1 Novel/Storyline 树形视图**
- 替换 COList 为 Tree widget
- 支持 Novel → Storyline → Chapter 三级展开

**4.2 正文展示区**
- 富文本渲染（Markdown）
- 支持滚动回看
- 当前写作位置高亮

**4.3 ReaderScreen 增强**
- 段落缩进、章节跳转、字数统计

**4.4 大纲编辑**
- OutlineEditScreen：拆分/合并/插入/重排 Step

**4.5 支线分叉与对比**
- 一键从当前 Step 分叉新支线
- BranchCompareScreen 对比两条支线

**4.6 DashboardScreen 完整版**
- 伏笔追踪、角色出场统计、写作活动统计

**4.7 CardPoolScreen 增强**
- 区分手动关联（实线） vs AI 建议（虚线）

**4.8 导出**
- 按章节导出 Markdown
- 按支线导出完整文本
- 全书导出

### Phase 5：高级特性

**5.1 卡片图谱视图**
- 可视化卡片链接关系网络

**5.2 时间线视图**
- 按 plot_beats 可视化叙事时间线
- 跨支线时间线对比

**5.3 角色关系图**
- 基于 character_profile 和剧情自动生成角色关系

**5.4 多模型路由**
- 主推理用高能力模型（写正文）
- 经济模型用于一致性检查、上下文压缩、大纲建议

---

## 补充设计：七个关键问题的解决方案

以下是对上述架构设计的深化——每个问题对应一个具体的实现方案。其落地步骤已合并进"实施阶段"的各 Phase 中。

### 问题 1：叙事引擎的检查靠什么——规则还是 LLM？

防火墙的五层检查有明确依据（Schema 校验、路径白名单），是确定性判断。但"这段叙事是否违反世界观"是模糊判断。如果五层全调 LLM，每个 Step 的延迟和成本翻五倍，写作心流归零。

**方案：三级判定策略——规则优先，LLM 内嵌，批量后置**

```
即时规则检查（零成本，不阻塞）:
  L2 情节重复  → 文本相似度（difflib/编辑距离），重复度 > 70% 触发
  L3 决策分级  → 关键词规则表（"死""杀""永别" → APPROVE，"走到""说道" → AUTO）
  L4 风格沙箱  → POV 人称代词检查（第一人称支线不应出现"他想"）
                 句长统计（设定"短句风格"时平均句长超阈值则警告）

即时 LLM 检查（零增量成本，内嵌在生成请求中）:
  L1 设定一致性 + L5 叙事质量 → 合并到正文生成的同一次 LLM 调用
  要求输出包含 consistency_check 字段：
  {
    "content": "正文...",
    "consistency_check": {
      "violations": [],
      "confidence": 0.85,
      "quality_concern": null,
      "character_changes": {},
      "new_foreshadowing": []
    },
    "needs_recall": null
      // 非空时触发按需原文回溯，见问题 6 补充方案
      // 示例：{"query": "第5章中铁门上的符号描述", "reason": "当前段落涉及角色再次面对这扇门"}
  }
  不额外调用，零增量成本。

  **容错策略**：`consistency_check` 解析失败时**静默通过 + 记录日志**，不暂停写作。
  理由：防火墙场景下解析失败必须保守（`human_required=True`），因为后果是执行危险操作。
  写作场景下最坏后果是漏掉一个设定矛盾，延迟批量检查可以兜底。
  频繁因 JSON 格式错误打断作者会摧毁心流。

延迟批量检查（章节完成后，后台执行）:
  每完成一个章节，用经济模型做一次全面审查：
  - 完整章节 vs 世界观设定的矛盾扫描
  - 角色行为一致性（性格是否突变）
  - 伏笔状态更新
  结果存入 NarrativePerception，下一章写作时作为约束注入。
  不阻塞写作过程。
```

**落地步骤**：
1. Phase 1：正文生成请求里加 `consistency_check` 输出要求，解析后记录
2. Phase 3：实现 L2/L4 的规则引擎（纯 Python，无 LLM 调用）
3. Phase 3：实现章节完成后的批量审查任务

### 问题 2：正文在哪里编辑

三种写作模式都涉及作者输入长文本。终端里的 TextArea 不是合格的文本编辑器——没有撤销历史、没有选词替换。这会直接劝退写作者。

**方案：$EDITOR 外部编辑器为主，内置 TextArea 为辅**

```
作者选择 [1]人工写 或 [3]全手写 时：

默认行为（$EDITOR 模式）：
  1. 生成临时文件 /tmp/overseer_step_{id}.md
  2. 文件头部写入只读的上下文提示（注释包裹）：
     <!-- 场景：宝黛初会 | 上一段结尾：...贾母笑道... -->
     <!-- 钉入卡片：[角色]林黛玉 [角色]贾宝玉 -->
     （以下开始写作）
  3. 调起 $EDITOR（vim/nano/vscode）
  4. 用户保存退出后，系统读取内容（去掉注释头）
  5. 模式 1 则将内容作为 author_draft 提交给 AI 扩写

回退行为（无 $EDITOR 或配置 editor: builtin）：
  使用 Textual TextArea，适合短文本输入

配置项：
  editor:
    mode: external          # external | builtin
    command: "vim"           # 覆盖 $EDITOR
    template: true           # 是否写入上下文提示头
```

类似 git commit 的编辑体验——终端用户对这种模式很熟悉。

**落地步骤**：
1. Phase 1：实现 `$EDITOR` 调起逻辑（参考 `click.edit()`）
2. Phase 1：Textual TextArea 作为 fallback
3. config.yaml 新增 `editor` 配置节

### 问题 3：Step 缺少版本历史

Step 只有三个文本字段，重写后旧版本丢失。"之前那个版本写得更好"是写作中的高频需求。

**方案：StepVersion 追加表**

```
StepVersion（新增模型）
  id: UUID
  step_id: FK → Execution
  version_number: int          # 自增
  source: str                  # ai_generated / author_draft / author_edit / ai_expand
  content: Text                # 该版本的完整文本
  created_at: datetime

操作对应：
  AI 生成   → append version(source=ai_generated)
  作者草稿  → append version(source=author_draft)
  AI 扩写   → append version(source=ai_expand)
  作者编辑  → append version(source=author_edit)
  作者接受  → Step.final_text = 该版本的 content

回退：
  作者在 Step 详情中看到版本列表
  选择任意版本 → 设为 final_text
  不删除其他版本
```

直接存全量文本。单个 Step 几千字，版本数通常 < 10，存储可忽略。

**版本回退与上下文一致性**

回退单个 Step 的文本后，后续 Step 的上下文（世界状态、角色位置、伏笔状态）仍基于旧版本生成，产生不一致。解决方案：每个 StepVersion 附带上下文快照。

```
StepVersion 扩展：
  context_snapshot: JSON (nullable)
    - Step 完成时，将当时的 Storyline.context 快照存入
    - 回退时，作者可选择是否同时回退世界状态到该快照
    - 不自动回退（避免破坏后续已确认的内容）
    - 提供选项："仅回退文本" vs "回退文本 + 世界状态"
```

这样回退操作是可控的：作者知道自己在做什么，系统不做隐式的连锁修改。

**落地步骤**：
1. Phase 1：新增 `StepVersion` 模型
2. Phase 1：每次生成/编辑时自动 append
3. Phase 2：TUI 里加版本列表和回退操作

### 问题 4：没有阅读模式

所有流程围绕"写"，但写作中大量时间在"读"——读前文找感觉，读整条支线判断节奏。

**方案：ReaderScreen——全屏阅读模式**

```
按 [r] 进入：

┌─────────────────────────────────────────────┐
│  《三体》 · 主线 · 第三章                     │
│─────────────────────────────────────────────│
│                                             │
│  叶文洁站在发射塔前，寒风从大兴安岭的方       │
│  向吹来。她盯着眼前的面板，手指悬在发射       │
│  按钮上方。                                  │
│                                             │
│  二十年前的那个雪夜......                     │
│                                             │
│─────────────────────────────────────────────│
│  Ch.3 Step 4/12 │ 8,432 字 │ [e]编辑 [q]返回 │
└─────────────────────────────────────────────┘

操作：
  j/k ↑↓      逐行滚动
  PgUp/PgDn   翻页
  g/G          文首/文末
  数字+Enter   跳到指定章节
  e            从阅读位置进入编辑
  q/Esc        返回
```

将支线所有 `final_text` 按 Step 顺序拼接渲染。

**落地步骤**：
1. Phase 2：实现 ReaderScreen 基础版
2. Phase 4：增强排版（段落缩进、章节跳转）
3. 快捷键新增 `r`

### 问题 5：卡片关联发现能力弱

卡片有链接机制，但依赖作者手动关联。卡片的核心价值是发现意外联系。

**方案：后台关联发现 + 写作时上下文推荐**

```
被动发现（后台，不阻塞）：
  触发：新卡片创建时 / 作者暂停写作超 30 秒时
  流程：
    1. 取最近的卡片 A
    2. 从 CardPool 同作品中取候选（tag 粗筛，限 20 张）
    3. 一次经济模型调用：判断 A 与候选之间的潜在叙事联系
    4. 结果存入 MemoryLink，标记 source=ai_suggested
    5. CardPoolScreen 中 AI 建议显示为虚线（vs 手动关联的实线）
    6. 作者确认（变实线）或删除

主动推荐（Step 暂停时）：
  1. 从 Step 大纲提取关键词（角色名、地点、事件）
  2. tag 匹配 + MemoryLink 链接扩展 → 候选集
  3. 候选 > 5 张时用 LLM 排序
  4. 展示 top 3-5 张，一键钉入
```

**落地步骤**：
1. Phase 2：Step 暂停时的关键词匹配推荐（纯规则）
2. Phase 3：后台 LLM 关联发现
3. Phase 4：CardPoolScreen 区分手动关联和 AI 建议

### 问题 6：长篇小说的上下文一致性

10 万字装不进 context window。写到第 30 章时，AI 不记得第 2 章的细节。关键词检索也不够——第 2 章的伏笔关键词不会出现在第 30 章的 query 里。

**方案：章节摘要链 + 显式伏笔追踪 + 角色状态快照**

```
章节摘要链（ChapterDigest）：
  每完成一个章节，一次 LLM 调用生成结构化摘要：
  {
    "chapter": 3,
    "summary": "叶文洁在红岸基地做出了向宇宙发射信号的决定...",
    "key_events": ["叶文洁按下发射按钮", "雷志成发现异常信号记录"],
    "character_changes": {"叶文洁": "从犹豫转为决绝"},
    "new_foreshadowing": ["异常信号记录未被销毁"],
    "resolved_foreshadowing": [],
    "world_state_delta": {"红岸基地": "信号已发射"}
  }

  存储：Storyline.context.chapter_digests[]
  后续章节 prompt 加载：最近 2 章全文 + 更早章节的摘要链

显式伏笔追踪：
  埋设：
    - consistency_check 输出中检测到 → 自动记录
    - 或作者手动标记
    - 写入 context.foreshadowing[]

  提醒：
    - 每个 Step 开始时检查未解决伏笔
    - 超过阈值（同章 5 Step / 跨章 2 章）→ 注入提醒到 prompt
    - 阈值可配置

  回收：
    - consistency_check 中声明 resolved
    - 或作者手动标记
    - foreshadowing[].resolved = true

角色状态快照：
  每个 Step 完成后，从 consistency_check 提取 character_changes
  更新 context.character_arcs
  后续 prompt 注入角色当前状态，而非让 AI 从全文推断
```

**补充方案：按需原文回溯**

摘要链是有损压缩。第 2 章的摘要不可能保留所有细节——一个角色的口头禅、一段对话的具体措辞、一个场景里的天气，这些信息在摘要中被丢弃。当第 30 章需要引用这些细节时，摘要链无法提供。

不引入向量数据库。原因：写作中需要的跨章回溯是**叙事关联**（"他打开那扇门"→ 需要第 5 章对这扇门的描写），不是**语义相近**（向量搜索擅长的）。向量搜索对叙事关联的召回率不可靠，且引入额外依赖（embedding 调用、向量存储、数据同步）的成本不值得。

方案是利用已有的结构化索引做按需精确回溯：

```
按需原文回溯（needs_recall）：
  触发：AI 生成正文时输出 needs_recall 字段（非空）

  输出格式（内嵌在生成请求的输出要求中，零增量成本）：
  {
    "content": "正文...",
    "consistency_check": {...},
    "needs_recall": {
      "query": "第5章中铁门上的符号描述",
      "reason": "当前段落涉及角色再次面对这扇门"
    }
  }

  处理流程（ContextPlugin 负责）：
    1. 解析 needs_recall.query
    2. 从结构化索引定位相关 Step：
       - plot_beats 中匹配关键词（铁门、符号）
       - foreshadowing 中匹配相关伏笔
       - character_arcs 中匹配相关角色
       - chapter_digests 中匹配章节号
    3. 加载定位到的 Step 的 final_text 原文
    4. 将原文片段注入 context，重新生成当前 Step
       （或作为补充信息追加到下一轮生成中）

  容错：
    - needs_recall 解析失败 → 静默忽略，同 consistency_check 的容错策略
    - 未定位到相关 Step → 记录日志，不阻塞写作
    - 大部分 Step 不会触发回溯，摘要链已足够
```

这个机制的关键：**不做全量索引，不预计算 embedding，只在 AI 主动请求时按需查询 SQLite**。成本完全可控——偶尔多一次数据库查询和一次重新生成，远低于每个 Step 都做 embedding 的固定开销。

**落地步骤**：
1. Phase 1：consistency_check 中包含 character_changes 和 foreshadowing
2. Phase 2：章节完成时生成 ChapterDigest
3. Phase 3：ContextPlugin 改为"近章全文 + 远章摘要链"
4. Phase 3：伏笔到期提醒
5. Phase 3：按需原文回溯（needs_recall 解析 + Step 定位 + 原文加载 + 重新生成）

### 问题 7：缺少全局视图

作者始终处于局部视角。需要一个纵观全局的地方。

**方案：DashboardScreen——作品级全局概览**

```
按 [0] 进入：

┌─────────────────────────────────────────────┐
│  《三体》 · 仪表盘                            │
│─────────────────────────────────────────────│
│                                             │
│  支线进展                    字数统计         │
│  ─────────                  ─────────       │
│  主线   ████████░░ Ch.8/12   52,340 字      │
│  支线A  ████░░░░░░ Ch.3/8    18,200 字      │
│  支线B  ██░░░░░░░░ Ch.1/5     6,100 字      │
│                              总计 76,640 字   │
│                                             │
│  未解决伏笔 (4)              角色最后出场     │
│  ─────────────              ──────────────   │
│  · 异常信号记录  Ch.3 已过7章  叶文洁  Ch.8   │
│  · 申玉菲的日记  Ch.5 已过3章  大史    Ch.7   │
│  · 三体游戏邀请  Ch.6 已过2章  汪淼    Ch.8   │
│  · 倒计时       Ch.7 已过1章  申玉菲  Ch.5 ⚠ │
│                                             │
│  近期活动                                    │
│  ────────                                   │
│  今日 3 Step · 2,100 字 · AI写/人写 = 2/1   │
│  本周 12 Step · 8,400 字                     │
│─────────────────────────────────────────────│
│  [s]写作  [m]卡片池  [r]阅读  [q]返回        │
└─────────────────────────────────────────────┘
```

数据全部来自已有数据的聚合查询，无需新模型：
- 支线进展：Plan 的 Step 完成数
- 字数：sum(final_text 字数) per Storyline
- 伏笔：context.foreshadowing where resolved=false
- 角色出场：遍历 plot_beats 取 characters 最大 chapter
- 角色超过 N 章未出现标注 ⚠

**落地步骤**：
1. Phase 2：基础版（支线进展 + 字数）
2. Phase 3：加入伏笔追踪和角色出场
3. Phase 4：写作活动统计

---

## 风险与约束

### AUTO/CONFIRM 边界是产品成败的关键

写代码时被打断确认不影响效率。写小说时被频繁打断会摧毁心流。如果 CONFIRM 触发太频繁，产品不可用。

初始策略：**默认偏向 AUTO**，只在以下情况 CONFIRM：
- AI confidence < 0.5
- 涉及角色死亡/重大转折（通过关键词检测）
- 违反已有 plot_rule

随着作者使用积累，自适应调整边界。

### Step 粒度必须可编辑

AI 生成的大纲划分一定和作者的节奏不一样。如果 Step 不能拆分/合并/重排，大纲功能形同虚设。这是 Phase 1 后期就需要的能力。

### 长文本展示是 TUI 的短板

Textual 的 RichLog/TextArea 可以展示长文本，但终端宽度有限。需要在 Phase 4 仔细调试排版，确保正文阅读体验可接受。如果终端体验不够好，可能需要考虑可选的 Web 预览。

### 小说写作是完全替换，不是兼容模式

本次改造是对现有防火墙语义的完全替换。不保留"AI 任务执行"模式，不做双模式兼容。CO 就是 Storyline，Execution 就是 Step，Memory 就是素材卡片。所有命名、提示词、TUI 文案、交互流程统一切换为写作语义。旧数据（如果有）通过 migration 清理或归档，不做向后兼容。
