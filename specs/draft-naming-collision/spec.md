# 基础草稿 / 扩展草稿命名与查找碰撞 · 规格（只读核查结论 + 待批准修法）

> 状态：只读核查完成，**未改任何代码**。修法待用户批准后执行。
> 范围：`lib/cover_watch.py`、`pipeline/preset_ops.py`、`pipeline/pipeline.py`、`pipeline/series_runner.py`、
> `.trae/skills/jianying-editor/examples/robust_auto_export.py`、`lib/job_manifest.py`、`pipeline/export_config.py`。
> 关联规格：`specs/export-parity/spec.md`（产物一致性，与本规格正交）。

## 1. 结论

**同一部剧内，基础名 ↔ `<基础名>_b` 不会互相取错；但有三处会取错或取不到，且其中一处已在真实草稿库中发生。**

**若不改会出事**：真实库已有 2 部剧只剩 `_b` 克隆、基础草稿不存在，watcher 会把它们误判为「无串流草稿」重新走 edit；
`series_runner --all-stream` 的前缀 `*串流*` 会把克隆草稿也算进全量导出；手动 `--profile B` 会造出 `_B` 与 watcher 期望的 `_b` 大小写不一致。

## 2. 五个问题的核查结果

### Q1 按名字找草稿：精确还是前缀/模糊？传基础名会不会返回 `_b`？

`find_stream_draft`（`lib/cover_watch.py` L595-601）是**前缀 + 必须以此结尾**的 glob：

```
glob(draft_root + f"{series_name}*串流") → os.path.isdir → max(mtime)
```

- 传基础名 `<剧名>_第01-80集串流`：克隆目录以 `_b` 结尾，不满足「以串流结尾」→ **不会返回 `_b`**。
- 但它是**前缀匹配**，不是等值匹配：若存在另一部剧名以当前剧名为严格前缀（如 `X` 与 `X2`），会互相命中并靠 mtime 抢占。
  当前实际剧名清单中不存在这种严格前缀重叠，但规则本身没有分隔符护栏。
- 单集草稿 `<剧名>_第NN集` 不以「串流」结尾，**永远查不到**。
- 同一规则的镜像实现在 `pipeline/series_runner.py` L76-84 `_find_draft`。
- 反例（唯一真正会命中克隆的查找）：`pipeline/series_runner.py` L87-92 `_all_stream_drafts` 使用 `*串流*`，
  会同时返回基础草稿与 `_b`/`_c` 克隆；仅在 `--mode export --all-stream`（L336-337）使用。
- 精确等值查找：`pipeline/pipeline.py` L409-412 `find_draft_by_name`（`d["name"] == name or basename == name`）。

### Q2 反方向：按 `_b` 找会不会命中基础草稿 / `_bb` / `_b_2`？

不会。反向查找全部是**字符串拼接 + `os.path.isdir`**，没有任何 glob：

- `_account_draft_name`（L623-625）：`f"{base_draft}_{prof}"`。
- `_missing_account_drafts`（L636-639）：对拼接结果 `os.path.isdir` 精确判断。
- `_export_targets`（L642-646）：同一口径。
- 导出器 `_find_draft_dir`（`robust_auto_export.py` L1426-1434）：`os.path.join(root, draft_name)` + `isdir`。

`_profile_of_draft`（L628-633）用 `startswith(base + "_")` 截后缀，会把 `..._b_2` 读成版本 `b_2`、`..._bb` 读成 `bb`；
它们不在 `profiles` 里，属于「未知后缀」，不会被当成 `b`。

### Q3 剧名/草稿名含 `_b` 时是否歧义？真实目录清单对照

命名规则 = `<剧名>_第NN-NN集串流`（`pipeline/batch_runner.py` L410/L431/L433），克隆 = 再拼 `_<组>`。
因为基础草稿必须以「集串流」结尾，而克隆以 `_<组>` 结尾，**两者在字符串层面天然可区分**；
剧名里出现 `_` 不影响（例：`重回60_带全家顿顿吃肉第二季`）。

真实草稿库（`%LOCALAPPDATA%\JianyingPro\User Data\Projects\com.lveditor.draft`）：

```
11月12日 / cici / default / ui_pipeline_ac
八零猎王，风雪赶山养妻护女_第01-146集串流_b          ← 只有 _b，无基础草稿
八零猎王：赶山捕兽遇旺妻_第01-142集串流
八零猎王：赶山捕兽遇旺妻_第01-142集串流_b
被踹下悬崖，我化身海东青赶山宠妻_第01-80集串流
被踹下悬崖，我化身海东青赶山宠妻_第01-80集串流_b
奶爸赶山：虎王认我当兄弟_第01-84集串流_b              ← 只有 _b，无基础草稿
重回81，东北猎王护全家_第01-183集串流
重生1983，带着五六半去赶山致富第一季_第01-263集串流
```

结论：`find_stream_draft("<八零猎王，风雪赶山养妻护女>")`、`find_stream_draft("<奶爸赶山：虎王认我当兄弟>")`
均返回 `None`（L597-600）。后果链：`_pending_account_outputs` L694-696 直接返回 `[]` →
`check_trigger` 认为没有待补版本 → `build_plan` 走「无草稿 → edit」分支，对只剩克隆的剧重新剪辑。

### Q4 组名大小写归一：四处是否同一口径？

| 环节 | 代码 | 是否归一为小写 |
|---|---|---|
| 版本判定 | `_prompt_groups` L661 `.strip().lower()` + L662 字符集校验 | 是 |
| 版本列表 | `_job_profiles` L667-685 直接复用上者结果 | 是 |
| 封面路径 | `job_manifest.resolve_profile_cover` L2199 `key = ... .strip().lower()`；`split_account_cover_stem` L2140 `.lower()`；`detect_account_cover_sets` L2152 目录名 `.lower()` | 是 |
| 预设名 | `pipeline.py` L2470-2480 `_find_profile_presets` 大小写不敏感 | 是（不敏感匹配） |
| 草稿后缀 | `_account_draft_name` L623-625 用 `profiles`（已小写） | 是 |
| **克隆落盘** | `preset_ops.clone_draft_with_presets` L327-330 → `export_config.preset_profile_name` L69-74 **只去 `presets_` 前缀、不转小写** | **否** |

唯一的漏点是手动入口：`rebase_presets.py --profile B` 会建出 `..._B`。
Windows 大小写不敏感，`isdir("..._b")` 仍为真，所以目前被文件系统掩盖；
但 `_profile_of_draft`（L628-633）会原样返回 `B`，与 `profiles` 里的 `b` 不相等，
`.export_state.json` 的 profile 键、`job.json.video.outputs.<版本>` 键也会写成 `B`/`b` 两套口径。
注释 L328 声称「组名归一化（Presets_B → b）」，与 `preset_profile_name` 实现不符。

### Q5 契约是否已写死？

已写死，但未显式出现「小写」二字于 `<组>` 处：

- `.trae/skills/drama-cover-builder/references/handoff-contract.md`
  - L119「落盘一律小写」
  - L123 基础版草稿**不加任何后缀**，不写 `_a`
  - L124 扩展版 `<基础草稿>_<版本>`
  - L125 版本名 `[a-z][a-z0-9]{0,7}`，大写/下划线/中文非法
  - L128 版本封面：基础版 `04_covers/cover_04_vertical.*`，扩展版 `04_covers/<组>/cover_04_vertical_<组>.*`
  - L130 版本判定唯一真源 = `prompts.json` 组数
  - L136-137 预设 / 封面 / 草稿三列对应表
  - L139 `presets_<组>` 缺失只把该组标 blocked（A 不阻断），不整体硬失败
  - L141 各版本产物文件集合一致
  - L143 `profiles[0]` 为基础草稿，其余为克隆
- `.trae/skills/drama-cover-builder/SKILL.md`
  - L240 组数即版本数 + 封面/预设对应
  - L244 基础版不加后缀
  - L245 扩展版 `<基础草稿>_<版本>`，`[a-z][a-z0-9]{0,7}`
  - L248 一版一封面、禁止沿用源草稿封面

## 3. 待批准修法（未执行）

| 编号 | 落点 | 改法 | 风险 |
|---|---|---|---|
| N1 | `lib/cover_watch.py` L595-601 `find_stream_draft` | 基础草稿查找改为「等值/分隔符」：优先 `isdir(root/<series_name>_第NN-NN集串流)` 精确命中，再退化为现有 glob；或在 glob 结果里过滤掉 `_<组>` 结尾的目录 | 低；需兼容历史草稿名 |
| N2 | `lib/cover_watch.py` L688-703 `_pending_account_outputs` | `draft is None` 时若发现 `<剧名>*串流_<组>` 存在，返回这些克隆名并标注 `base_missing`，不再静默返回 `[]` | 中；会改变调度分支 |
| N3 | `pipeline/series_runner.py` L87-92 `_all_stream_drafts` | `*串流*` → 排除以 `_<小写版本名>` 结尾的目录，或显式区分基础/克隆两类清单 | 低 |
| N4 | `pipeline/export_config.py` L69-74 `preset_profile_name` | 返回 `.strip().lower()`，与注释及 L125 字符集一致 | 低；`presets_A` 解析结果变为 `a`，需确认无依赖原大小写处 |
| N5 | `handoff-contract.md` L124 / `SKILL.md` L245 | `<组>` 处补一句「`<组>` 一律小写，与 `prompts.json` 组名 lower 后一致」 | 无 |

决策建议：N4 + N5 必须改（口径统一、成本极低）；N1 + N2 必须改（已在真实库触发）；
N3 视是否仍使用 `--all-stream` 决定，若使用则必须改。

## 4. 验收判据

1. 对上述 2 部「只有 `_b`」的剧，`find_stream_draft` / `_pending_account_outputs` 不再返回「无草稿」，而是给出克隆名与 `base_missing` 标记。
2. `find_stream_draft("X")` 对库中不存在严格前缀重叠的剧名，返回唯一且等于基础草稿（非 `_b`）。
3. `rebase_presets.py --profile B` 与 `--profile b` 生成同名目录 `..._b`，`_profile_of_draft` 返回 `b`。
4. `series_runner --mode export --all-stream` 清单中不出现 `_b`/`_c` 克隆（除非显式要求包含）。
5. 静态检查：`python -m py_compile` 通过相关文件；不新增对 `pipeline/presets.json` 与 `<剧集根>` job 数据的写入。

## 5. 待批准项

1. 是否批准 N1-N5 全部落地，或只落地 N4+N5。
2. N2 的调度语义：`base_missing` 时是「重新 edit 基础草稿」还是「用克隆补齐基础」？
3. 是否允许修改 `pipeline/series_runner.py` 与 `pipeline/export_config.py`（两者当前不在另一会话的改动集合内）。
