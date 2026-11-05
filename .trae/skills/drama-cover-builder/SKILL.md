---
name: drama-cover-builder
description: 中文短剧封面全链路：从简介生成封面标题（3 方案 × 4 版本 = 12 条，编号 T1—T12）、按选定标题生成横/竖封面图、从视频抽帧。触发词：短剧封面、做封面、生成短剧标题、抽帧、合成封面。不用于非短剧封面、通用海报、剧本正文或分镜、日历选题、图片精修；pipeline 侧调度/导出不归本 skill（草稿封面写入用本 skill 的 `write-draft`）。默认先出 12 条标题并停在用户选择门禁，不得自动进入出图阶段。
---

# Drama Cover Builder（短剧封面流水线）

阶段模块：Stage 0 建任务目录与 `job.json` → Stage 1 标题生成（核心）→ 用户门禁 → Stage 2 AI 封面生成（核心）→ Stage 2b Pillow 兜底。每阶段可独立调用：既能直接走 CLI，也能 import Python API；阶段之间只通过目录与 `job.json` 交接。剪映草稿封面写入已内置到本 skill（`scripts/drama_cover.py write-draft`：`draft_cover.jpg` + 原生 `composition`），不再依赖 `lib/jy_cover.py`。

## 文件结构与读取路由

| 文件 | 内容 | 何时必读 |
| --- | --- | --- |
| `references/handoff-contract.md` | 任务目录、`job.json` 字段、状态机、标题与封面映射、参考图选择规则 | 建目录、读写 `job.json`、跨宿主交接时**必读** |
| `references/title-spec.md` | 标题生成唯一执行标准（方案层、版本层、版4 机制族、素材白名单/黑名单、钩子手法、递进链、打脸四式、质检清单） | 生成或修改任何标题时**必读** |
| `references/cover-spec.md` | 出图硬约束（无文字/无武器/无儿童/无血腥）、三版构图、尺寸、质检门禁 | 进入 Stage 2 时**必读** |
| `references/prompt-spec.md` | 出图提示词五段结构（身份锚点→构图→画面元素→硬约束→尺寸）、元素提取规则、落盘格式 | 生成 Stage 2 提示词时**必读** |
| `references/channels.md` | 出图渠道矩阵（doubao-jimeng / volcano-ark / trae-host）、统一后处理、adapter 接口 | Stage 2 选渠道或接新渠道时必读 |
| `references/scorecard.md` | 七维评分、硬门槛、矩阵推荐标注、落选稿去向 | 12 条出稿后、交付门禁前读取 |
| `references/failure-cases.md` | 失败模式分类、真实翻车记录、已验证标题台账、规范升级触发条件 | 每部剧收尾时写入；季度复盘时通读 |
| `references/platform-rules.md` | 审核红线、平台标题规格、烧字字数档位、压缩规则 | 进入 Stage 2 前、涉及平台标题或烧字时必读 |
| `references/ab-testing.md` | 发布侧 A/B 设计（当前未启用，预留） | **默认不读**；用户明确要求发布对照实验时再读 |
| `assets/compose_styles/pillow_styles.json` | Pillow 4 样式 × 2 尺寸族的视觉参数 | 只用 Stage 2b 兜底时读取/调参 |
| `references/archive/v3-spec.md` | v3 历史规范，仅追溯用 | 不主动读取 |

标题细则只在 `title-spec.md` 维护。本文件与 spec 冲突时，**以 spec 为准**；本文件只保留不可破纪律摘要。

## 一、能力边界与任务路由

### 1.1 适用范围

- 短剧封面标题生成、改写、评分与 3×4 方案矩阵；
- 按选定标题生成横版 2848×1600 / 竖版 1080×1920 封面图；
- 从短剧视频抽帧作为底图或身份锚点。

### 1.2 不适用范围

不直接交付：完整剧本、分镜、剧情扩写、非短剧的通用海报、电商/品牌视觉、日历与选题规划、图片精修与调色、视频剪辑成片。

越界时用一句话说明：`本 Skill 只做短剧封面标题与封面图，<用户所求成品> 更适合由对应能力完成。` 随后给不超过两个转化选项：把主题转为 12 条封面标题；或先完成封面、把其余模块交给对应能力。不强行附带标题或图片。

### 1.3 判定与路由表

每次调用先判定，再行动。不得看到"封面"就直接跑全链路。

| 输入状态 | 必须采取的动作 | 禁止行为 |
| --- | --- | --- |
| 剧名 + 简介 + 剧集目录，且要求落盘 | 先走 Stage 0 建任务目录（Trae 侧 `job_manifest.create_job()`），再进 Stage 1 | 跳过目录与 `job.json` 直接开写 |
| 只有简介 | 先出素材卡片 → 生成 12 条标题 → 停在门禁 | 自动进入 Stage 2/3 |
| 简介 + 底图 | 同上；底图仅登记为身份锚点 | 直接把文字压在底图上交付 |
| 只有视频 | 只抽帧，给 3 个候选帧 | 顺手生成标题或写草稿 |
| 只有标题、无简介 | 追问「主角身份 / 关键道具或异能 / 最大冲突」三项 | 凭空补剧情、猜设定 |
| 已选定 3 条标题 | 进入 Stage 2，按版位锁构图 | 重新发散、替换用户已选标题 |
| 用户自带 3 条自定义标题 | 直接进 Stage 2，不改写、不评分（除非用户要求） | 擅自改写用户的标题 |
| 只要 1 张竖版投流图 | 只做竖版 1080×1920 | 顺带产出 3 张横版 |
| 只要写入草稿 | 用 `scripts/drama_cover.py write-draft`（本 skill 内置，写 `draft_cover.jpg` + 原生 composition） | 只写 jpg 不写 composition（剪映会拿首帧覆盖） |
| 非短剧 / 海报 / 剧本 / 日历 | 说明边界并给转化选项 | 硬套 3×4 结构 |

### 1.4 追问规则

- 优先执行而非追问：能从简介、附件、上下文推断的，直接补足并用一句话注明假设。
- 只有以下情况追问：三项核心信息（身份 / 道具或异能 / 冲突）同时缺失；平台或尺寸要求不明且会显著改变产物；用户要求"改标题"但没给原稿。
- 单轮只问一个阻断性问题，最多三个；不为账号定位、语气、受众等次要信息中断任务。

## 二、Stage 0：任务目录与 job.json

读取 `references/handoff-contract.md`。这一步只做三件事：建目录、落简介、定参考图。

Stage 0 的建目录入口已从脚本迁到 **Trae 侧库函数** `lib/job_manifest.py::create_job()`（没有 CLI 子命令；`scripts/drama_cover.py` 已移除 `prepare`）：

```powershell
python -c "import sys; sys.path.insert(0,'lib'); import job_manifest as jm; jm.create_job(series_dir=r'D:\短剧\重生八零，打猎卖货养妻女')"
```

- 返回 `{job_dir, job_path, job, reference, synopsis, video}`；`job.json` 已存在时抛 `FileExistsError`，需要 `force=True` 重建（对应历史 `history` 事件仍记 `prepare`）。
- `out_dir` 可覆盖 `cover_job` 的位置；`host` 默认 `trae`。

| 规则 | 说明 |
| --- | --- |
| 默认目录 | `<剧集目录>/cover_job`；`out_dir` 可覆盖 |
| 目录一次建全 | `01_input` / `02_titles` / `03_prompts` / `04_covers` / `05_video` / `06_publish` / `06_publish/receipts`（receipts 为占位目录，Stage 3 才写内容，缺失不阻断前序阶段） |
| 自动找输入 | 简介按 `简介.txt → synopsis.txt → 详细简介.txt`；参考图按契约 §5；mp4 唯一时才记 `video.path`，多个则只记数量与命名规律 |
| 简介只认原文 | 写入 `01_input/synopsis.txt`，Stage 1 只从这里读，不沿用聊天里的改写版 |
| 参考图只复制 | 复制为 `01_input/reference.*`，并记 `reference_original` 与 `reference_sha256`，原图不动；找不到也继续建目录 |
| 默认映射 | `cover_1<-T1`、`cover_2<-T2`、`cover_3<-T3`，`titles.gate=pending`；门禁后按用户点选改写 |
| 不覆盖 | `job.json` 已存在时抛 `FileExistsError`，只有 `force=True` 才重建 |

宿主无本地执行能力（如豆包客户端纯对话）时，**不依赖任何脚本**，按下面的顺序直接执行——本 skill 的 Stage 0/1/2 全部知识都在 `SKILL.md` + `references/` 内，豆包导入后即可完成；本地执行段（草稿封面写入 `write-draft`、视频登记、发布、清理）回 Trae：

**Stage 0（建任务）**

1. 向用户收集：剧集目录绝对路径、简介全文（或其中一份简介文件的内容）、参考图（可后补）。
2. 读 `references/handoff-contract.md` §1—§3，输出三样东西让用户落盘：
   - 目录清单（`cover_job/` 六个子目录）；
   - `job.json` **全文**（字段一个不能少；无执行环境下 `reference_sha256` / `prompt_sha256` / `image_sha256` 等 hash 字段写 `null`，回到 Trae 时用 `validate` 补算）；
   - `01_input/synopsis.txt` 全文（即简介原文）。

**Stage 1（12 条标题 + 门禁）**

3. 用户回复“已建目录”后，读 `references/title-spec.md` 生成 12 条，把 `02_titles/prompt.txt`、`titles.json`、`titles.md` 三个文件**全文**输出给用户保存。**不需要输出 job.json**——`stage=titles_done` 由 Trae 侧 `record-titles` 登记。
4. 停在门禁：用户回复 3 个编号后，只输出 `02_titles/selection.json`（`{"picks":["T9","T10","T7"],"selected_by":"user"}`）让用户保存。**不再手写 job.json**——`titles.selected`、`cover_map` 回填、`stage=titles_selected` 全部由 Trae 侧 `ingest`/`select` 生成。竖版标题默认取 `selected` 三条中**总分最高**的一条，同分取 `selected` 中靠前的一条；若 `vertical_cover.title_id` 已被人工显式写入则优先保留。硬约束：`titles.gate` 只允许 `pending` / `done` 两值，`titles.selected` 必须是 3 个对象 `{slot,title_id,title}`——禁止自造枚举（如 `gate="selected"`）或字符串数组；已有违规形态由 Trae 侧 `validate --fix` 修复。

**Stage 2（即梦出图）**

5. 读 `references/cover-spec.md` 与 `references/prompt-spec.md`，按门禁选定的 3 条标题反推提示词（五段结构；构图由封面位决定：1 爆点仰拍 / 2 冷暖对比 / 3 守护挡身；竖版胸像逆光），提示词写入 `03_prompts/prompts.json` 与 `prompts.md` 全文输出给用户保存（`channel` 按 `references/channels.md` 填写）。**不需要输出 job.json**——`prompts` 登记由 Trae 侧 `ingest` 从 prompts.json 重建。
6. 调用即梦：每张上传 `01_input` 里的参考图作身份锚点 + 对应提示词，逐张生成；按 cover-spec 四项硬约束（无文字/无武器/无血腥/无儿童）自检，不合格重做该张。
7. 4 张图存入 `04_covers/`，**文件名可任意**（`cover_01_hit` / `封面图1` / `T2 封面` / `竖版` 都行，png/jpg/jpeg/webp）——Trae 侧 `ingest` 会按竖版宽高比与编号自动识别并改名到契约名。**不需要输出 job.json**——`cover_map` 登记与 hash 由 `ingest` 重建。
7.5. 若用户在发起任务时指定了**裁剪秒数 / 派生平台 / 只剪辑不导出**，再输出 `dispatch.json` 全文让用户存入 `cover_job/`（无需指定则不输出，缺省=不裁剪、不派生、先剪辑再导出）：

```json
{"version": 1, "mode": "full", "cut_s": 15, "derive": ["youtube", "tiktok"], "priority": 0}
```

`mode` 只有 `full`（剪辑+导出）/ `edit_only`（只剪辑停在草稿）；`cut_s` 正数秒、`0`=不裁；`derive` 支持 `ig`/`facebook`/`all`、`[]`=不派生。该文件由 Trae 侧 `ingest` 归并进 `job.json.workflow`，豆包**不写 job.json**；用户事后手改 `job.json.workflow` 并把 `intent_revision` +1 即为最终意图（见契约 §7）。

**回 Trae（本地执行段）**

8. 用户回到 Trae 说“封面已就位”，Trae 依次：`validate --fix`（补算全部 hash 并校验）→ `ingest`（登记+规范化+改名+推进 stage）→ `record-video`（登记 pipeline 导出目录）→ 草稿封面（`scripts/drama_cover.py write-draft`）/ 发布 / 清理；或一条 `python lib/cover_watch.py --root "D:/短剧"` 全自动跑完（含串流导出 mp4 与 Toast 通知）。豆包侧至此交付完毕。

唯一规则：**不在对话里手写 job.json**（它是 Trae 侧 CLI 的收口产物），也绝不编造 hash；只交内容产物，文件内容必须完整可复制，不给“略”。

## 三、输入建模：素材卡片

Stage 1 开始前，先从简介中抽出要素，**以卡片形式呈现给用户核对**。这是防编造的前置闸门，比事后拦截更省返工。

| 栏位 | 内容 | 简介出处 |
| --- | --- | --- |
| 主角身份 | | 引用简介原句 |
| 关键道具/异能 | | 引用简介原句 |
| 核心冲突 | | 引用简介原句 |
| 地位/财富反转 | | 引用简介原句 |
| 禁区（简介未写） | 背叛方式、反派下场、亲人死因等 | — |

缺失分级：

- 可从简介推断 → 直接补并注明「推断」；
- 关键道具/异能 与 核心冲突 同时缺失 → 追问（见 §1.4）；
- 用户补充新素材后，更新卡片再继续，不沿用手写前的旧要素。

## 四、Stage 1：标题生成

读取 `references/title-spec.md`。三条不可破纪律（细则以 spec 为准）：

1. **素材边界**：简介没写的事件一律不存在；只允许在 spec §2.1 的五个白名单维度上放大。
2. **形式契约**：每条 60—80 字（版3 反问允许 55—70），全繁体、第三人称、无主角本名。
3. **产出结构**：固定 3 方案 × 4 版本 = 12 条（版1/2/3 叙事版 + 版4 机制版），一条不多一条不少。

生成流程按 spec §8 执行：素材圈定 → 白名单对照 → 12 格矩阵生成 → §6 质检 → 差异化检查 → 评分（`scorecard.md`）→ 交付门禁。

## 五、Stage 1 输出契约与用户门禁

### 5.1 交付格式（固定）

12 条**平铺编号**（T1—T12），每条必须包含：

| 编号 | 方案×版本 | 标题全文 | 字数 | 钩子手法 / 机制族 | 打脸式 | 简介出处 | 七维总分 | 推荐 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |

- 12 条必须完整可复制，不得只给摘要、骨架或占位；
- 版4 的「钩子手法」列写机制族（M1—M8）；
- 「简介出处」写清该条最亮素材来自简介哪一句；
- 「推荐」按 `scorecard.md` §3 标注，只推荐不排序、不删条；
- 用户要「再来几条」时，替换最弱条目，不追加第 13 条。

### 5.2 失败处理

简介撑不满 12 条差异化时，明确说明「只能先交付 N 条，缺的是 <素材类型>」，并给出可补齐的方向。不硬凑、不静默降级、不用编造填空。

### 5.3 用户门禁（强制）

12 条交付后**停下**，按 T 编号让用户依次为三个封面位各选 1 条：

1. 封面1（爆点构图）→ 用户回复一个编号
2. 封面2（冷暖对比构图）→ 用户回复一个编号
3. 封面3（守护挡身构图）→ 用户回复一个编号

- 每次只选一条，可以分三轮，也可以一次给出三个编号；
- **不按版本锁定**：任意编号可选入任意封面位，构图由封面位决定；版4 被选中时沿用该封面位构图；
- 12 条超过选择题上限，因此**不用 AskUserQuestion**，改为编号点选；
- 用户点满 3 个编号前不得进入 Stage 2；Agent 不代选、不预设默认项；
- 用户也可绕过 12 条自带标题，此时先确认「该标题对应哪版构图」再合成。

落盘方式：

```powershell
python lib/job_manifest.py select --job "<cover_job>" --picks "T6,T11,T1"   # 依次为封面1/2/3
```

选定结果写入 `job.json`（`titles.selected` + `selected_by="user"` + `cover_map` 回填），并在 `titles.json` 原位标注（选中条加 `selected/slot`，顶层记 `gate`）；不复制文本、不另产文件。冻结含义：此后改选必须显式操作且 `revision` 递增，Stage 2 发现 revision 不一致要把旧封面标 `stale` 重出。

**Stage 2 直调兜底**：若用户未完成门禁而 Stage 2 被直接调用，`resolve` 自动选 3 条：默认**推荐模式**（`titles.json` 的 `summary.recommended` 推荐池按总分取前 3，不足 3 条从全量补足，推荐池缺失退化为全量评分），`--mode score` 切**评分模式**（全 12 条按总分取前 3）；总分相同时推荐条目优先，再平按 内容兑现→信息缺口→情绪峰值。写回 `selected_by="auto"` + `auto_mode` 并追加 history——这是可审计的兜底而非静默代选，用户事后改选则旧封面标 `stale`。`resolve --no-auto` 可关掉该行为。

## 六、Stage 2：AI 封面生成

读取 `references/cover-spec.md`（硬约束与构图）、`references/prompt-spec.md`（提示词五段结构）与 `references/channels.md`（渠道矩阵），按选定标题生成提示词并交渠道出图。封面是**重新生成的图**，不是底图压字；底图只作身份锚点（人物、服装、猛兽种类、环境），构图、光影、致富元素、黑影对比全部重做。

### 6.1 输出契约

| 产物 | 尺寸 | 构图来源 |
| --- | --- | --- |
| 封面1 横版 | 2848×1600 | 版1 爆点仰拍 |
| 封面2 横版 | 2848×1600 | 版2 冷暖对比 |
| 封面3 横版 | 2848×1600 | 版3 守护挡身 |
| 竖版 | 1080×1920 | 竖版胸像逆光 |

- 模型原始输出必须用 `PIL Image.LANCZOS` 缩放到精确像素，不接受近似尺寸；
- 每张交付时附四项质检结论：无文字 / 无武器 / 无儿童 / 无血腥，任一项 Fail 即重做该张；
- 三张横版构图必须肉眼可分，不能都是"主角居中金光"；
- 交付时给文件绝对路径，不只给图片说明；
- 出图前先过 §七 的三步审核扫描；需烧字时按 `platform-rules.md` C 层档位检查行数与安全区。

### 6.2 Stage 2b：Pillow 兜底

AI 出图不可用时走本地合成：

```powershell
python scripts/drama_cover.py compose-pillow --base base.jpg --style all --out ./cover_out
```

- `--size vertical | horizontal | all`
- `--style classic_red_stroke | shadow_outline | neon_glow | bottom_tape | all`
- `--title` 可选，不传即无标题封面
- 视觉参数调 `assets/compose_styles/pillow_styles.json`

Pillow 路线出的是文字压图封面，与 Stage 2 的 AI 重绘图是两条路，不混用在同一批交付里。

## 七、平台审核与投放适配

读取 `references/platform-rules.md`。规则分三层，不可混用：

- **A 层通用红线**（绝对化用语、收益承诺、低俗擦边、血腥、未成年人、封建迷信、群体对立、真实人物）：所有标题与文案必须过，与 `title-spec.md` 的事实边界并列，冲突时事实边界优先。
- **B 层平台规格**（平台填写标题上限、封面烧字建议、安全区）：只能作为建议值，标注「待核实」；涉及硬限制时先核对平台官方文档，核实后写日期。
- **C 层项目字数档位**（竖版每行 4—7 字 ≤2 行、横版每行 5—9 字 ≤2 行、投放标题 10—18 字）：可直接执行，超限先删修饰再断行，不缩字号硬塞。

三条执行约束：

1. 60—80 字完整标题只用于策划与矩阵比稿，**不直接作为平台标题或封面烧字**；
2. 投放短标题由 12 条压缩而来，不新增简介外信息，不计入 12 条，不算"第 13 条"；
3. 审核不过时只改表达不改事实，被改掉的表达记入 `failure-cases.md`，标注「审核规避」。

Stage 2 出图前执行三步扫描：字面（A 层）→ 语义（承诺/迷信/对立/借势）→ 画面（cover-spec 四项）。Stage 2b 烧字时按 C 层档位检查行数与安全区。

## 八、复盘与规范升级

每部剧收尾（Stage 2 完成或用户中途放弃）后，向 `references/failure-cases.md` 补记 5 行：采用的格位、是否用自定义标题、被否原因归类、下次要试的机制。

季度或满 5 部剧时通读台账，按 `failure-cases.md` §4 的四个触发条件判断是否启动 v5 规范修订；没有数据支撑就不改规范。

## 九、命令与 Python API

| 命令 | 作用 |
| --- | --- |
| `record-titles` | 登记 Stage 1 的 12 条标题与提示词快照 |
| `select` | 人工门禁：写 `titles.selected` 并回填 `cover_map` |
| `resolve` | Stage 2 取上下文：标题+参考图+提示词，门禁未过自动选分（`--dry-run`/`--no-auto`） |
| `record-video` | 登记 pipeline 导出目录（自动探测 stream manifest，只记录不复制 mp4；手工指定优先；挂 `cleanup.external`） |
| `set-cut` | 设置/清除导出裁剪秒数 `video.cut_s`（`--cut 7200` 裁剪；`--cut 0` 清除；默认 null 不裁剪，watcher 非 null 时传 `--cut`） |
| `derive` | 从 04_covers 派生平台尺寸到 derived/（youtube/tiktok/instagram-reels/facebook-reels，不写 job.json） |
| `ingest` | Stage 2 收口：从落盘产物（prompts.json+4 图+selection.json）重建 job.json，宽容规范化（selected/stage/文件名）、补 hash、推进 stage，幂等 |
| `title` | 打印标题规范路径（Agent 读规范后在线生成，Ollama 兜底） |
| `art-from-video` | ffmpeg select+thumbnail 抽最佳帧 |
| `compose-pillow` | Pillow 合成，4 样式 × 2 尺寸族，标题可选 |
| `write-draft` | 写剪映草稿封面：`draft_cover.jpg` + 剪映原生 `composition`（只写 jpg 会被首帧覆盖） |
| `full` | 一键链路：底图/抽帧 → 竖版合成 →（可选）`write-draft` |

Stage 0 的建目录**不在脚本里**：入口是 Trae 侧 `lib/job_manifest.py::create_job()`（Python API，无 CLI 子命令；历史事件仍记 `prepare`）。

串流残留清理（发布流程未自动化，默认信任"已发布"）：`python pipeline/stream_manifest.py clean --job "<草稿名>" --dry-run` 先核对，去掉 `--dry-run` 才真删；默认删 `__jycache__`/`_work_*`/成品目录、**不删 C 盘草稿**（连草稿加 `--include-drafts`），严格模式加 `--require-receipts`，**只想清中间产物、保留成品与草稿**用 `--intermediates-only`。

手工发布回执（不手写 job.json）：`python lib/job_manifest.py publish-record --job "<cover_job>" --platform youtube --video-id "<id>" --url "<url>"`，自动写 `06_publish/<platform>.json` + `receipts/<platform>-<时间戳>.json` 并补齐 `job.json.publish`；同平台同 `video_id` 重复登记自动跳过。

`job.json.video.export_state` 记录成品生命周期（`null`/`exported`/`cleaned`）：`record_video` 置 `exported`，clean 真正删成品后自动置 `cleaned`（`--dry-run`/`--intermediates-only` 不回写），不改 `stage`。

导出完成**不等于**归档：`job.json.stage=archived` 是合法 stage，含义为「已归档、不再参与调度」，由 `pipeline/archive_job.py` 写入（watcher 的触发判定对它硬拦，`--phase` / `--once` / `--job` 都绕不过去）。归档是显式操作：默认准入 `publish.status=published` 或 `video.export_state=cleaned`（`--force` 可跳过）；首次归档时有活锁（`.watcher.lock`）或 `.export_state.json` `status=exporting` 则拒绝；`archive_root` 等于或位于 `watch.roots` 之下、或 `watch.roots` 为空时直接拒绝（fail-closed）。整剧目录搬到 `<archive_root>/<剧名>`，并同步改写 `pipeline/_stream_manifests/*.json` 的 `paths.source_dir`（否则后续清理按旧路径找不到源）；`stage` 已 archived 但目录未搬净时，重跑同一命令即续做。注意：自动发布链不写 `publish.status`（只有手工 `publish-record` 会置 `published`），不要依据它判断是否已发布。契约见 `references/handoff-contract.md` §7.4。

剪映草稿封面写入（`draft_cover.jpg` + 原生 composition）已内置到本 skill，不再依赖 `lib/jy_cover.py`：

```powershell
python scripts/drama_cover.py write-draft --cover "<封面图>" --draft-dir "<草稿目录>"
python scripts/drama_cover.py full --video "<mp4>" --title "<标题>" --out "<输出目录>" [--draft-dir "<草稿目录>"]
```

- **必须写 composition**：剪映 5.9 打开草稿时若顶层 `cover` 字段为 `None`，会用第一帧覆盖 `draft_cover.jpg`；只写 jpg 的封面会被吃掉。
- `write-draft` 读取草稿目录里的 `draft_content.json`，依次写 `draft_cover.jpg`、原生 composition，再原子写回草稿。
- `full` 只做「底图/抽帧 → 竖版 `bottom_tape` 合成 →（可选）写草稿」，不做标题生成；`--synopsis` 仅为兼容保留。

## 十、cover_watch：落盘两阶段调度

豆包/手工把 Stage 2 产物（4 张契约图 + prompts.json [+ dispatch.json]）落盘后，`lib/cover_watch.py` 自动调度，**串行、每轮只推进一部**：

```powershell
python lib/cover_watch.py                        # 读 pipeline/presets.json 的 watch 段：roots 自动发现
python lib/cover_watch.py plan --root "D:/短剧"  # 只查看：各剧阶段/意图来源/下一步/被挡原因
python lib/cover_watch.py --hold --once          # 只列计划不执行（先核对 job.json 再放行）
python lib/cover_watch.py --phase export --once  # 单轮强制只推进导出（配合 export_policy=manual）
python lib/cover_watch.py status --job "<cover_job>"
python lib/cover_watch.py inspect --job "<cover_job>"   # 只读聚合：intent / edit / export（不落盘、不触发）
```

归档（把整剧搬到独立归档池，watcher 永久不再调度；先 `--dry-run` 核对）：

```powershell
python pipeline/archive_job.py --job "<cover_job>" --archive-root "D:/短剧_存档" --dry-run
python pipeline/archive_job.py --job "<cover_job>" --archive-root "D:/短剧_存档"
```

配置 `<repo>/pipeline/presets.json` 的 `watch` 段（从 `presets.example.json` 复制起步），每轮热加载、改完不用重启：`{"watch":{"roots":["D:/短剧"],"jobs":[],"exclude":[],"settle":30,"interval":5,"derive":[],"export_policy":"immediate","hold":false}}`。`roots` 下的 `<剧集>/cover_job` 自动发现，无需登记。归档池根目录另行配置 `watch.archive_root`（仅 `pipeline/archive_job.py` 使用，必须在 `roots` 之外；`roots` 为空时归档脚本拒绝执行）。

**两阶段**：无串流草稿 → **edit**（validate --fix → ingest 归并 dispatch → batch_runner --no-export 生成草稿+帧对齐）；有草稿 + `workflow.mode=full` + 策略放行 → **export**（导出 mp4 → verify 质检 → record-video → derive）。`mode=edit_only` 永远停在草稿，等人工把 `workflow.mode` 改为 `full` 并 `intent_revision` +1。**export_policy**：`immediate`（默认，剪完即导）/ `after_edit_queue_drained`（全部剧剪完才开始导出）/ `manual`（等 `--phase export`）。**意图**：运行期只读 `job.json.workflow`；`dispatch.json` 仅在 `intent_revision==0` 时归并，人工 +1 后 job.json 永远优先（冲突记日志+桌面通知）。旧版 `export_config.json` 仍兼容。多 job 单 watcher 串行；跨进程由 `.export.lock` 互斥。进度看 Toast / `watcher.log` / `06_publish/pipeline.log` / `plan` / `status`。

触发条件（全满足）：4 张图齐（文件名可任意，`ingest` 自动识别改名）、prompts.json 存在、job.json 可解析、`titles.gate="done"`、`stage ∈ {titles_selected, prompts_ready, covers_ready}`（`covers_done` 等别名自动映射）、video 未导出、无活锁、静默 30s（防抖）。`stage=archived` 一律不触发（硬拦发生在阶段/单剧过滤之前，`--phase` / `--once` / `--job` 都绕不过）。封面自动吃 `cover_04_vertical`；导出后自动质检 `verify_export.py`（`--no-verify` 可关）。

**导出状态与自愈（Trae 侧自动，豆包侧无需写任何新东西）**：导出状态收敛在 `cover_job/.export_state.json` 单一真源（`cover_watch` 自动写，契约 §7.1）。导出失败按 1/2/4/8/16/30 分钟**退避重试**；连续失败达到 `watch.retry_pause_after`（默认 5）**暂停该剧并桌面通知**，需人工排查后 `--phase export` 放行（放行不清零计数）。**素材缺失 / 磁盘不足**属硬失败，**不重试、不计数**，直接置终态并通知，避免空转。导出前默认冷启动剪映（`watch.cold_start`，默认 true）。豆包侧照旧只交 Stage 0/1/2 内容产物，不需要感知或写这些运行态字段。
