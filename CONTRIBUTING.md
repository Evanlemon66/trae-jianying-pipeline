# 贡献指南

欢迎贡献。流程是标准的 Fork + PR：

## 流程

1. **Fork** 本仓库到你自己的账号
2. 克隆你的 Fork，基于 `dev` 建分支：`git checkout -b feat/你的改动 origin/dev`
3. 提交改动并推送到你的 Fork
4. 向本仓库的 **`dev` 分支**发 Pull Request（不要直接对 main）
5. CI 通过后由仓库主审查合并；dev 定期合入 main 并打版本 tag

## 本地验证（提交前必跑）

```powershell
$py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
# 1. Python 语法
& $py -m py_compile pipeline\pipeline.py   # 以及你改动的其它 .py
# 2. 预设解析自检（如动了预设相关逻辑）
& $py pipeline\pipeline.py --list-presets
# 3. 安装器画像自检（如动了 install_deps.ps1）
powershell -File pipeline\install_deps.ps1 -DetectOnly
```

## 硬性规则

- **禁止提交个人配置/密钥**：`pipeline/vc_config.json`、`pipeline/presets.json`（已 gitignore，CI 会做泄漏检查）
- **禁止手搓剪映草稿 JSON**：草稿相关逻辑必须从模板复制生成（见使用指南「剪映草稿的坑」）
- commit message 前缀约定：`feat:` / `fix:` / `docs:` / `refactor:` / `test:`
- 改动依赖或安装逻辑时，同步更新 `使用指南.md` 对应章节

## PR 描述写清楚三件事

改了什么 / 为什么改 / 怎么验证的（贴命令或输出）。
