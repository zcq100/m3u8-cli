# CI 与发布流水线设计

日期：2026-11-01
状态：已确认

## 背景

仓库有两个远程：`origin`（自建 Gitea `git.r1a.co:5000`）与 `github`（`github.com/zcq100/m3u8-cli`）。
此前所有发布都是本地手工执行 `uv build` 与 `scripts/build_extension.py`，没有测试门禁，也没有 CI。

用户要求：`src/` 有变化时构建并发布到 PyPI；`chrome-extension/` 有变化时打包发布到 GitHub Release；
并且每次修改插件或命令代码都要相应提升版本号。

## 决策

| 决策点 | 选择 | 理由 |
|--------|------|------|
| CI 平台 | GitHub Actions | 支持 `gh release`，Secret 管理简单 |
| PyPI 触发 | 合并/推送到 `master` 且 `src/**`、`pyproject.toml`、`uv.lock` 变化 | PR 阶段只校验不发布 |
| PyPI 认证 | 仓库 Secret `PYPI_API_TOKEN` | 配置简单，不依赖 PyPI 端一次性设置 |
| 测试门禁 | 发布前必须通过单元测试 | 避免发出坏包 |
| 扩展发布形态 | 每次一个 `chrome-extension-v<version>` tag + Release | 与 Python 的 `0.2.x` 版本线互相独立 |
| Gitea 同步 | 不自动镜像，只在 README 说明 | 改动最小，避免在 CI 里持有 Gitea 凭证 |

## 工作流

### `.github/workflows/ci.yml`

- 触发：`push`（全部分支）与 `pull_request`
- `python` job：Python 3.12 → `uv sync --locked` → `uv run python -m unittest discover -s tests -v`
- `extension` job：Node 20 → 对 `lib/background/content/popup.js` 执行 `node --check` → `node --test chrome-extension/lib.test.js`
- 只验证，不发布

### `.github/workflows/publish-pypi.yml`

- 触发：`push` 到 `master`，路径 `src/**`、`pyproject.toml`、`uv.lock`
- `concurrency: pypi-publish`，`cancel-in-progress: false`
- 流程：`uv sync --locked` → 单元测试 → 校验 `pyproject.toml` 与 `src/m3u8_cli/__init__.py` 版本一致
  → `uv build` → 查询 `https://pypi.org/pypi/m3u8-cli/<version>/json`，已存在则失败并提示升版本
  → `uv publish --token ${{ secrets.PYPI_API_TOKEN }}`
- 版本检查失败信息必须明确指出要改哪两个文件

### `.github/workflows/release-extension.yml`

- 触发：`push` 到 `master`，路径 `chrome-extension/**`
- `permissions: contents: write`；`concurrency: extension-release`
- 流程：Node 20 跑 `node --test chrome-extension/lib.test.js` → `python3 scripts/build_extension.py`
  → 从 `manifest.json` 读版本 → `git ls-remote --tags origin refs/tags/chrome-extension-v<version>` 已存在则失败
  → `gh release create chrome-extension-v<version> dist/m3u8-cli-chrome-extension-<version>.zip --generate-notes`

## 版本规则

- 改 `src/**` → 同时提升 `pyproject.toml` 与 `src/m3u8_cli/__init__.py` 的版本号，两者必须一致
- 改 `chrome-extension/**` → 提升 `chrome-extension/manifest.json` 的 `version`
- CI 不自动改版本号，只做校验；未提升版本号时发布任务会明确失败

## 一次性人工配置

1. PyPI 创建限定项目 `m3u8-cli` 的 API token
2. GitHub 仓库 Settings → Secrets and variables → Actions 新建 `PYPI_API_TOKEN`
3. 确认 GitHub 默认分支为 `master`

## 已知限制

- 推送到 Gitea（`origin`）不会触发 CI，需要额外 `git push github master`
- 扩展 Release 依赖 `gh` CLI 与默认 `GITHUB_TOKEN`，因此不能在同一 push 中重复发布同一版本
- 不做自动升版本、不做 Gitea Actions、不为 Python 包建 Release
