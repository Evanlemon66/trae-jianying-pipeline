# 任务清单：断点续跑 + reconfigure 收敛

> 依赖：`specs/console-resume-cache/spec.md` 已批准。
> 顺序即执行顺序；每完成一项立即标记，批次间不得并行改同一文件。

## [x] T1 批次 A：纯库去掉顶层 reconfigure
- 文件：`pipeline/jianying_ui.py`、`pipeline_ui/jianying_ui.py`
- 改动：删除第 2 行 `sys.stdout.reconfigure(encoding="utf-8")`。
- 验证：`python -m py_compile` 两文件。

## [x] T2 批次 B：核心库 reconfigure 移入 `__main__`
- 文件：`pipeline/pipeline.py`、`verify_draft.py`、`stream_manifest.py`、
  `repair_translation.py`、`split_long_subtitles.py`
- 改动：删顶层调用；在各文件 `if __name__ == "__main__":` 首行重加。
- 验证：`py_compile` + 假 stdout 导入冒烟（spec §2 脚本）输出 `IMPORT_OK`。

## [x] T3 批次 C：脚本类 reconfigure 移入 `__main__`/`main()`
- 文件：`archive_job.py`、`prune_workdirs.py`、`_framefix_draft.py`、`spotcheck_asr.py`、
  `merge_drafts.py`、`series_runner.py`、`run_status.py`、`verify_export.py`、
  `route_c_verify/render_draft.py`
- `verify_export.py`：顶层 guarded 块删除，移入 `main()` 首行（保留 None 兜底）。
- 验证：`py_compile` + 导入冒烟 `IMPORT_OK`。

## [x] T4 更新 `tests/test_verify_stdout.py` 的 C1 断言
- 改动：断言 `def main(` 之前的源码不含 `sys.stdout.reconfigure`。
- 验证：`python -m pytest tests/test_verify_stdout.py -q`。

## [x] T5 新增 `pipeline/step_cache.py`
- 按 spec §3 实现 API（configure/enabled/fingerprint/make_key/load/save/dir_manifest/note）。
- 验证：`py_compile`；T7 单测。

## [x] T6 接入三处缓存 + `--no-cache`
- `pipeline/pipeline.py`：`run_pipeline(use_cache=True)`、三处接入、`--no-cache`、
  `_DEMUCS_VERSION/_ASR_VERSION/_TRANSLATE_VERSION/_cache_tag`。
- `pipeline/batch_runner.py`：argparse `--no-cache`；L590 / L848 两处 `run_pipeline` 透传；
  docstring 补用法。
- 验证：`py_compile` 两文件；`python pipeline/batch_runner.py --help` 出现 `--no-cache`。

## [x] T7 新增 `tests/test_step_cache.py`
- 覆盖：miss→save→hit；产物 size 变化→miss；版本不同→miss；`configure(False)` 不读不写。
- 验证：`python -m pytest tests/test_step_cache.py -q`。

## [x] T8 回归测试
- 命令：`python -m pytest tests/test_verify_stdout.py tests/test_step_cache.py tests/test_runner_stage.py tests/test_runner_cover_fallback.py -q`
- 通过标准：全绿，无新增失败。

## [x] T9 Console 续跑冒烟（失败任务 20261117-070939-51cc）
- 重启 Console（加载新代码）。
- `POST /api/runs/20261117-070939-51cc/rerun`，确认新 run 的 `cmd` 与原记录一致、状态 running、
  日志出现缓存初始化行并越过 ASR 34% 段（观察到 1-2 集 demucs/ASR 完成即可 `POST /api/run/stop`）。
- 再次同入口重跑，确认日志出现 `♻️ 复用…`。
- 失败处理：若入口报错，记录响应体与日志尾部后停止，不在 Console 侧临时改代码绕过。
