# 断点续跑（产物级缓存）+ reconfigure 收敛 规格

> 状态：**待批准后执行**。本轮只改 `pipeline/`、`route_c_verify/`、`tests/` 内代码；
> 不动 `<剧集根>` 数据、不动草稿内容、不启停剪映/watcher、不改 `specs/stage-rerun/spec.md`。
> 本规格是 `specs/stage-rerun/spec.md` §5「edit 子步断点续跑契约」的落地实现（同目录下补充，不重写）。

## 0. 目标

1. 把十余个模块顶层的 `sys.stdout.reconfigure(encoding="utf-8")` 收敛到各自入口，
   库导入期不再改全局流；单独运行时仍保持 UTF-8 输出（GBK 控制台不炸）。
2. 按方案 A（产物级内容缓存）实现「失败后重跑 = 续跑」：demucs / ASR / 翻译三处命中即复用，
   其余步骤照常执行；**缓存只影响"算"，不影响"存"**，删缓存 100% 回到现状。

非目标：
- 不做阶段级断点（不跳过建稿/预设/裁剪/终检/导出）。
- 不覆盖 `pipeline_ui/ui_pipeline.py`（UI 自动剪辑链路）。
- 不改 Console 前端按钮语义、不改 watcher 调度；Console「重跑」已按原参数重发命令，
  缓存默认开启后天然成为续跑入口。
- 不引入自动重试、不做跨 workdir/跨剧共享。

## 1. 硬约束

- key 必须含：`CACHE_VERSION` + 阶段参数 + 源文件指纹（路径/大小/mtime_ns）+ 模型或渠道版本。
- 原子写：先写 `*.tmp` 再 `os.replace`；目录型产物用「清单 + 原地产物」校验大小。
- manifest 缺失、版本不符、任一产物大小不符 → 视为 miss（并覆盖写新的）。
- 缓存根固定在 `<workdir>/__cache__/`（`_` 前缀，`prune_workdirs` 保护；`stream_manifest clean`
  随 `_work_*` 一起删）。不复制大文件：demucs 产物原地复用，只写清单。
- 命中打印 `♻️ 复用第N集 …（缓存）`；`--no-cache` 时不读不写不打印。
- 缓存内容异常只降级为 miss，**绝不**让缓存本身抛错中断流水线（`load/save` 内部全 try/except）。

## 2. 批次一：reconfigure 收敛

规则：模块顶层删除 `sys.stdout.reconfigure(encoding="utf-8")`；有 `if __name__ == "__main__":`
的，把该行作为 `__main__` 块第一句（`batch_runner.py` 是入口，保留现状）。

| 批次 | 文件 | 处理 |
|---|---|---|
| A（纯库） | `pipeline/jianying_ui.py:2`、`pipeline_ui/jianying_ui.py:2` | 直接删除（无 `__main__`，无导入方） |
| B（核心库） | `pipeline/pipeline.py:33` | 移入 `__main__`（L3637） |
| B | `pipeline/verify_draft.py:30` | 移入 `__main__`（L678） |
| B | `pipeline/stream_manifest.py:38` | 移入 `__main__`（L911） |
| B | `pipeline/repair_translation.py:29` | 移入 `__main__`（L259） |
| B | `pipeline/split_long_subtitles.py:31` | 移入 `__main__`（L242） |
| C（可独立运行） | `pipeline/archive_job.py:45` | 移入 `__main__`（L523） |
| C | `pipeline/prune_workdirs.py:20` | 移入 `__main__`（L124） |
| C | `pipeline/_framefix_draft.py:22` | 移入 `__main__`（L517） |
| C | `pipeline/spotcheck_asr.py:10` | 移入 `__main__`（L114） |
| C | `pipeline/merge_drafts.py:30` | 移入 `__main__`（L526） |
| C | `pipeline/series_runner.py:32` | 移入 `__main__`（L776） |
| C | `pipeline/run_status.py:29` | 移入 `__main__`（L134） |
| C | `pipeline/verify_export.py:19-23` | 删除顶层 guarded 调用，移入 `main()` 首行（`sys.stdout` 为 None 时 try/except 兜底） |
| C | `route_c_verify/render_draft.py:22` | 移入 `__main__`（L278） |

配套：
- `tests/test_verify_stdout.py` 的 C1 断言改为「`def main(` 之前的源码不含 `sys.stdout.reconfigure`」，
  保持"导入期不崩"这一语义（比原先的字符串匹配更贴合新约定）。
- 每批完成后：`python -m py_compile <本批文件>`；
  再跑导入冒烟（用无 `reconfigure` 的假 stdout 导入各库，任何残留顶层调用都会 `AttributeError`）：

```python
# python - <<'PY' 等价写法，Windows 用临时命令
import sys
class S:
    def write(self, s): return len(s)
    def flush(self): pass
sys.stdout = S(); sys.stderr = S()
sys.path.insert(0, "pipeline")
import pipeline, verify_draft, stream_manifest, repair_translation, split_long_subtitles
import archive_job, prune_workdirs, _framefix_draft, merge_drafts, series_runner, run_status, verify_export
print("IMPORT_OK")
```

## 3. 批次二：新增 `pipeline/step_cache.py`

约 110 行，唯一职责：内容指纹 + 原子清单读写。

```python
CACHE_VERSION = 1
CACHE_DIRNAME = "__cache__"

def configure(use_cache: bool) -> None         # 全局开关；JY_NO_CACHE=1 初始关闭
def enabled() -> bool
def fingerprint(path: str) -> dict             # {"path": abspath, "size": n, "mtime_ns": n}
def make_key(stage: str, params: dict, sources: list[dict]) -> str
                                               # sha256(canonical_json([CACHE_VERSION, stage, params, sources]))
def cache_file(workdir: str, stage: str, key: str) -> str
                                               # <workdir>/__cache__/<stage>/<key>.json
def load(workdir, stage, key) -> dict | None   # 校验 version/key；payload["files"] 存在时逐个校验 size
def save(workdir, stage, key, payload: dict) -> str | None   # 注入 version/key/created_at，tmp+replace
def dir_manifest(root: str, workdir: str) -> list[dict]      # [{"rel","size"}]，按 rel 排序
def note(stage: str, label: str, detail: str = "") -> None   # print("  ♻️ 复用…（缓存）")
```

失败语义：`load`/`save` 任意异常返回 `None`/不写；`load` 校验失败不删旧文件（下次 save 覆盖）。

## 4. 批次二：接入三处

新增模块级常量（`pipeline.py` 顶部）：

```python
_DEMUCS_VERSION = "v1"      # 分离模型/参数变更时手动 +1
_ASR_VERSION = "v1"         # ASR 降级链变更时手动 +1
_TRANSLATE_VERSION = "v1"   # 翻译链变更时手动 +1
def _cache_tag(section):    # canonical json(_ENV_PROFILE[section]) + 版本号，随机器画像自动失效
```

1. **demucs（串流逐集，L2935-2974）**
   - key：`make_key("demucs", {"model_tag": _cache_tag("demucs")}, [fingerprint(ep["path"])])`
   - 命中：从 payload 取 `vocal`/`bgm` 的 workdir 相对路径，拼回绝对路径，`note("demucs", f"第{i}集 人声分离")`，
     跳过 `run_demucs`；后续挂轨逻辑不变。
   - 未命中：原逻辑 `run_demucs(src_for_demucs, workdir/demucs_epNN)`，成功后
     `save(..., {"vocal": rel, "bgm": rel|None, "files": dir_manifest(demucs_epNN, workdir)})`。
   - 单集（非串流）分支同样处理（key 用 `video_path or existing_audio`，产物在 `workdir` 根）。
2. **ASR（L3114-3128）**
   - key：`make_key("asr", {"v": _ASR_VERSION, "skip_local_ai": bool, "tag": _cache_tag("whisper")},
     [fingerprint(u["path"])])`（`__jy_norm__` 缓存文件 mtime 跨轮稳定；缺失则重建，key 自然变化）。
   - 命中：`cn_subs = payload["subs"]`，`note("asr", f"第{u['idx']}集 识别")`；后续静音回截/裁剪/翻译照旧。
   - 未命中：原 `run_asr_chain`；`cn_subs` 非空时写入 `workdir/asr_epNN.json`
     `{"subs": [...], "files": [{"rel": "asr_epNN.json", "size": ...}]}`（对齐 stage-rerun §5 命名）。
3. **翻译（L3179-3194）**
   - key：`make_key("translate", {"v": _TRANSLATE_VERSION, "tag": _cache_tag("translate")},
     [])` + `params={"src": [s["text"] for s in cn_subs]}`（文本内容即指纹）。
   - 命中：`en_texts = payload["en"]`，长度必须等于 `cn_subs`，否则当 miss。
   - 未命中且返回非空、长度匹配：写 `workdir/translation_epNN.json`
     `{"src": [...], "en": [...], "files": [...]}`；失败/空结果**不写缓存**（保证下轮重试）。
   - `45s` 立即重试分支产生的成功结果同样落缓存。

## 5. 参数与入口

- `run_pipeline(..., use_cache=True)` 新关键字；函数开头 `step_cache.configure(use_cache)`。
- `pipeline.py` argparse 增加 `--no-cache`（帮助：忽略产物缓存，全量重算）。
- `pipeline/batch_runner.py` 增加 `--no-cache`；两处 `pl.run_pipeline(...)`（L590 串流、L848 逐集）
  透传 `use_cache=not args.no_cache`；模块 docstring 补一行用法。
- `pipeline_ui/console/runner.py`：**不改**。重跑端点已按 `rec["params"]` 重发原命令，
  默认开关即续跑；`--no-cache` 留给 CLI/排障。
- Console 侧仅需重启服务加载新代码（当前进程运行旧代码）。

## 6. 验收

1. 批 A/B/C 每批：`py_compile` 通过 + 第 2 节假 stdout 导入冒烟输出 `IMPORT_OK`。
2. `python -m pytest tests/test_verify_stdout.py tests/test_runner_stage.py tests/test_runner_cover_fallback.py -q` 全绿
   （`test_verify_stdout.py` 按新 C1 断言）。
3. 新增 `tests/test_step_cache.py`（纯本地、秒级）：
   - miss → save → load 命中；产物文件被改大小后 `load` 返回 None；
   - `CACHE_VERSION` 不同 → miss；`configure(False)` 时 save/load 均不生效。
4. Console 续跑冒烟（用失败任务 `20261117-070939-51cc`）：
   - 重启 Console → `POST /api/runs/20261117-070939-51cc/rerun` → 新 run 的 `cmd` 与原命令逐字一致，
     日志出现缓存初始化行，任务越过上次失败的 ASR 34% 段（允许观察到第 1-2 集 demucs/ASR 完成后手动停止）。
   - 第二次同入口重跑 → 日志出现 `♻️ 复用第1集 …`，同一阶段耗时显著下降。
5. 回滚验证：`<workdir>/__cache__` 改名后重跑，行为与改动前一致。

## 7. 风险与边界

- `__jycache__` 里 `__jy_norm__` 若被重建（mtime 变），该集 demucs/ASR 自动 miss，代价 = 现状重算。
- 换 ASR/翻译渠道（改 `_env_profile.json`）时，`_cache_tag` 变化自动失效；未覆盖的渠道内部变更需手动 +版本号。
- `--clean-workdir` / `stream_manifest clean` 会删缓存，语义 = 全量重算。
- UI 链路（`ui_pipeline.py`）不接入，不受影响。
