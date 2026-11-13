# 多组封面执行手册（按发布账号组 · 即梦精修链）v1.1 · 2026-11-12

> 用途：每部剧开图前按本手册逐步对照执行，不再靠记忆拼流程。
> 与 `cover-spec.md`（构图与硬约束）、`prompt-spec.md`（提示词结构）、`title-spec.md`（标题矩阵）、`scorecard.md`（评分）冲突时，以那四份为准；本手册只管**执行顺序、参数与检查点**。

---

## 0. 适用范围

- 一部剧从「剧集目录 + 简介 + 参考封面」到「各组成品 + 画布写入 + 交付」的全过程。
- 每部剧**每组固定 4 张**（3 横 + 1 竖），组数由门禁分组选择表达式决定（`title-spec.md` §9.6）：单组 4 张、双组 8 张，更多组按每组 4 张递增。
- 出图通道默认 `doubao-jimeng`（豆包 + 即梦），精修默认走即梦 `normal` 超清，无积分才跳过。

---

## 1. 能力清单（各管一段，不互相替代）

| 环节 | 用什么 | 边界 |
| --- | --- | --- |
| 流程与规范 | 本 skill（Stage 0 / 1 / 1.5 / 2） | 只管内容与契约，不管画质 |
| 出图 | `image_edit`（主）、`image_gen`（无参考图时） | 管构图、光影、爆点元素；固定出方图 |
| 画质精修 | 即梦 CLI `node upscale image --mode normal` | 只提清晰度、保构图；`pro` 会重绘 |
| 尺寸与裁切 | 本地 PIL（`Image.LANCZOS`） | 唯一能把图变成精确像素的手段 |
| 画布写入 | `design-workspace +create` / `+get` | 产物必须用生成工具返回的原始短链 |
| 交付 | `FileBatchUpload` → `RichMediaCard` | 图片一律走 RichMediaCard，不走 present_files |

---

## 2. 主流程（12 步）

| # | 步骤 | 关键动作 | 落盘 |
| --- | --- | --- | --- |
| 1 | Stage 0 建任务区 | 建 `cover_job/` 七子目录 + `b_group`、`_dl`；复制参考图；写 job.json rev1 | `job.json`（`stage=prepared`）、`01_input/synopsis.txt`、`01_input/reference.jpg` |
| 2 | 素材卡片 | 从简介抽：主角身份 / 关键道具或异能 / 核心冲突 / 地位财富反转 / 禁区 | 对话内呈现 |
| 3 | Stage 1 标题 | T1—T12（3 方案 × 4 版本）+ N1—N12（投放层四标准 × 3，心理学三维度＝标准 2/3/4），共 24 条；不再另出 B1—B12 | `02_titles/titles.json`、`titles.md`、`prompt.txt` |
| 4 | 七维评分 | 每条算 7 项 0—5，合计 0—35，出均值；标推荐（每版本最高、全表 4 个；投放层同规则） | 写进 `titles.json` 的 `scores`（7 元素数组）/ `total` |
| 5 | 门禁 | **分组选择表达式**（`title-spec.md` §9.6）：`t1 t2 n1，t5 t9 n12` = 2 组；写 `selected_groups`（首组同步 `selected`）、`gate=done`、回填 `cover_map`、竖版绑最高分 | `job.json` rev+1 |
| 6 | Stage 1.5 提示词 | 每组 4 条五段结构（身份锚点→构图→画面元素+≥2 冲突点→硬约束→尺寸），用本组选中的标题；同标题跨组时，后续组每条追加「爆点加码」段 | `03_prompts/prompts.json`、`prompts.md` |
| 7 | A 组出图 | `image_edit` 每批 **2 个请求**；出方图 | 源图落 `_dl/src/a1..a4.png` |
| 8 | 其余组出图 | 从第二组起逐组使用本组选中的标题；同标题特例时同构图、加大光效/规模/压迫/姿态/天地比例 | `_dl/src/<组>1..4.png` |
| 9 | 即梦精修 | `canvas create` → 逐组 `resource upload` 与 `node create image` → 批量 `node upscale image --mode normal --credit-ceiling 0 --wait` → 逐张 `resource download` | `_dl/up/<组>1..4.png`、`_dl/im_res.json`、`_dl/im_up.json` |
| 10 | 裁切与精确尺寸 | 横版裁 `(0,744,3072,2472)`、竖版裁 `(672,0,2400,3072)`；LANCZOS → 2848×1600 / 1080×1920 | `04_covers/` 每组 4 张（B 组在 `b_group/`） |
| 11 | 质检与登记 | 分组接触表看图 + 右下角 700×260 放大 2 倍水印图；生成 `covers_manifest.json`；job 推 `covers_ready` | `_dl/final_report.json`、`covers_manifest.json`、`job.json` |
| 12 | 画布 + 交付 | `FileBatchUpload` 取全部成品短链 → `+create` 每组 4 item → `+get` 回读 → RichMediaCard 分组交付 | 画布 item、按组卡片 |

---

## 3. 逐步检查清单

### 3.1 Stage 0
- [ ] 剧集目录下的 `cover_job/` 已建，含 `01_input` `02_titles` `03_prompts` `04_covers` `04_covers/b_group` `04_covers/derived` `05_video` `06_publish/receipts` `_dl`
- [ ] 参考封面已复制为 `01_input/reference.*`，`reference_sha256` 已记
- [ ] `job.json` 里 `sizes.horizontal=[2848,1600]`、`sizes.vertical=[1080,1920]`
- [ ] 未指定裁剪秒数时 `video.cut_s=null`、`workflow.cut_s=null`；指定则只改该字段

### 3.2 Stage 1 / 评分
- [ ] T1—T12 齐，3 方案 × 4 版本，跨方案差异可辨
- [ ] N1—N12 齐，四标准 × 3 条，字数 32—40
- [ ] 策划层 60—80 字（版3 反问 55—70），全繁体、第三人称、无主角本名
- [ ] 每条**当场给出七维分项 + 合计 + 均值**（只给推荐不给分＝不合格）
- [ ] 硬门槛：内容兑现 < 4 或具体画面 < 3 的已重写
- [ ] 无编造：角色新增行为、死因、下场、简介外数额时间地点一律没写
- [ ] 推荐标注：每版本 3 条中总分最高，全表 4 个

### 3.3 Stage 1.5 提示词
- [ ] 每组 4 条都是五段结构，顺序未打乱
- [ ] 每条 ≥2 个可拍冲突点，且没有违规物件
- [ ] 每组构图都由封面位锁定（1 仰拍 / 2 冷暖 / 3 守护 / 竖版胸像）
- [ ] 同标题跨组时，后续组各条都有至少一项「爆点加码」的可观测描述
- [ ] 硬约束四句写全（无文字 / 无武器 / 无血腥 / 无儿童）

### 3.4 出图
- [ ] 每张方图里人物头顶 ≥ 10%、脚 ≤ 80%（横版要裁 16:9，可用带高只有 56%）
- [ ] 无文字、无武器、无儿童、无血腥、无生硬竖线分屏
- [ ] 三张横版构图肉眼可分，不是同一张换个角度
- [ ] 眼瞳颜色正常（发光金瞳、异色瞳需重出）

### 3.5 即梦精修
- [ ] `authorizedCredits=0`、`generationState=succeeded`、3072×3072
- [ ] 每组 4/4 全部成功；任一失败先补跑该张，不静默少一张
- [ ] 超清后构图与源图一致（被重绘说明用了 `pro`）

### 3.6 裁切 / 交付
- [ ] 成品尺寸精确 2848×1600 / 1080×1920，无近似
- [ ] 全部成品右下角放大复核无「即梦AI」水印
- [ ] `covers_manifest.json` 每条有 `group`；B 组有 `variant_of`
- [ ] 画布每组 4 个 item `+get` 回读全部命中
- [ ] `job.json` 推进到 `covers_ready`，`revision` 递增，`history` 追加

---

## 4. 固定参数速查

| 项 | 值 |
| --- | --- |
| 横版成品 | 2848 × 1600 |
| 竖版成品 | 1080 × 1920 |
| 即梦超清产物 | 3072 × 3072 |
| 横版裁切框 | `(0, 744, 3072, 2472)` → 高 1728 → 缩 2848×1600 |
| 竖版裁切框 | `(672, 0, 2400, 3072)` → 宽 1728 → 缩 1080×1920 |
| 即梦水印实测区 | y≈2820—2960、x≈2400—2900（右下），裁切框必须完全避开 |
| A 组命名 | `cover_01_hit.png` / `cover_02_contrast.png` / `cover_03_guard.png` / `cover_04_vertical.png` |
| B 组命名 | `04_covers/b_group/` 下同名 + `_B` |
| manifest | `covers_manifest.json`：`group`、`variant_of`、`title_id`、`file`、`size`、`bytes`、`sha256`、`crop` |

---

## 5. 已踩过的坑（每部剧复检）

| 症状 | 原因 | 处理 |
| --- | --- | --- |
| `image_edit` 返回 `Length mismatch: request_list (4) and content (3)` | 一次 4 个请求不稳定 | 每批 **2 个**请求 |
| 显式 width/height 不生效，只出方图 | 宿主固定方图 | 接受方图，靠裁切拿横竖比例 |
| 人物被切脚 | 方图人物占 50%+，16:9 带高只有 56% | 提示词写死「头顶约 36%、双脚约 66%、高度只占 28%」重出 |
| 人物眼睛发光/异色 | 逆光提示被模型演绎成异能 | 明确写「瞳孔自然深褐，不要发光金瞳」，重出后重走精修 |
| `canvas create --name` 报 unrecognized option | 名称是位置参数 | `canvas create "<名>" --use` |
| upscale 结果找不到资源 id | 资源在 `items[].resources[0].resourceId` | 按该路径取，别读顶层 `resourceId` |
| `resources download` 落盘路径报错 | 目标目录不存在 | 先建目录再下载 |
| `python -c` 多语句报 SyntaxError | 宿主限制 | 落 `.py` 文件用 `python -X utf8` 跑 |
| PowerShell 写中文脚本乱码 | 编码 | `[IO.File]::WriteAllText` + `UTF8Encoding($false)` |
| 画布 `+get` 拿不到条目 | 回读结构在 `xml_data` 的 `<get_result><items>` | 解析 `item_id` 字段，不是顶层数组 |
| 画布单次查太多 | 上限 10 个 id | 分组 `--item-ids` |

---

## 6. 每部剧只改这几处

1. 剧集目录名与 `job_id`；
2. `01_input/synopsis.txt` 与参考图；
3. `02_titles` 的候选池（T+N=24 条）+ 七维分 + 分组选择结果；
4. `03_prompts` 的每组 4 条（人物身份锚点、构图元素；同标题跨组时后续组加码）；
5. `video.cut_s`（只在用户指定裁剪时填，其余照常）；
6. 画布名与 `projectId`。

其余（尺寸、裁切框、命名、manifest 字段、质检项）全部照第 4 节照抄，不要重新发明。
