# Changelog

## v1.5.1 - 2026-11-13
- 文档：多版本成品命名章节补充「版本封面」约束——`draft_cover.jpg` 为草稿目录级文件，克隆/重建时必须按本组竖版图重写（`job_manifest.resolve_profile_cover`），B/C 版不得沿用 A 版封面。

## v1.5.0 - 2026-03-04
- Security hardening:
  - sanitized draft project names and blocked path traversal/out-of-root delete.
  - restored TLS verification for SAMI TTS by default.
  - added cloud download URL/header/size guards.
- API/CLI standardization:
  - unified machine-readable `--json` output for key scripts.
  - added strict mode for validator (`--strict`).
  - centralized runtime config (`scripts/utils/config.py`).
- Quality engineering:
  - expanded unit tests for security guards.
  - added repo hygiene and data schema checks.
  - added CI lint/format/test/schema pipeline.
- Repo organization:
  - removed tracked runtime artifacts and cache binaries.
  - added compatibility wrappers and common logger utility.
