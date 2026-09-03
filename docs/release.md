# 发布

分两件事：应用仓库的**聚合 GitHub Release**，以及本仓库控制镜像的 **Harbor 发布**。

## 应用聚合 Release

成功提升只产生：

- 一个 Git tag：`vMAJOR.MINOR.PATCH`（由根 `aggregate_version_file` 控制，默认 `VERSION`）
- 一个 GitHub Release，标题与 tag 相同

不再为每个服务单独建 Release。服务 `version_file` 可保留作文档。`versions` 命令仍能计算 `{service}-vMAJOR.MINOR.PATCH` 形式的服务 tag，供需要时查阅，但不作为当前提升路径。

### 正文结构

由 `summarize` 或 `release` 内部渲染，章节为：

- `## Shared changes` — 共享 / CI / 构建；只改 Dockerfile 也在这里
- `## Service-specific changes` — 仅当 `services/<svc>` 或 `deploy/<svc>` 业务路径有变更
- `## Affected services` — 本次部署的服务列表
- 若没有服务、只有根级 deploy 变更：`## Deployment scope` + `- Shared deployment configuration`

版本号只出现在 GitHub Release 的 name/tag，不要写进正文标题（避免再出现 `# v0.1.1` 这类误导）。

### 上下文与模型

1. 构建前：`changes --base <main-sha> --details-file changes.json`
2. `summarize --changes-file changes.json --services ... --output release.md`：一次有界 DeepSeek 请求，正文文件权限 `0600`。命令会 `git fetch --tags`，避免浅克隆把下一个 tag 算成 `v0.1.1`。
3. `release --changes-file changes.json --summary-file release.md`：不再请求模型；读入时剥离正文开头的 `# vX.Y.Z`。

`changes.json`、编译产物和 `release.md` 在 job 之间使用 GitHub Artifacts；不
需要把状态目录或 Docker volume 挂到宿主机。

没有 `--summary-file` 时，`release` 会按 shared / 每服务分别请求模型。消费方应走 summarize + summary-file，避免重复计费与不一致。

Diff 上下文会脱敏并截断。commit、分支、workflow、凭据元数据不会发送给 DeepSeek，也不会写进 Release。模型 HTTP `408/429/5xx` 会重试；`401` 等直接失败。

`release` 还会 `fast_forward_main`（要求当前 HEAD 是 `main` 的祖先），然后推 tag。同 commit 重试：若该 tag 已指向同一 commit 则复用 Release；指向其他 commit 则拒绝。

### 旧 prefix tag

新 tag 是 `vMAJOR.MINOR.PATCH`。选择下一 patch 或重试已打过 prefix tag 的 commit 时，仍计入 `<aggregate_release_prefix>-vMAJOR.MINOR.PATCH`。

一次性迁移：

1. 核对旧的按服务 Release 与 Git tag
2. 在已提升的 SHA 上用变更上下文创建新的聚合 Release
3. `gh release delete <legacy-tag> --yes`，**不要**加 `--cleanup-tag`（只删 GitHub Release 记录，保留 Git tag）

## 本仓库：发布控制镜像

1. 改代码与测试
2. 更新 [VERSION](../VERSION)（以及 `pyproject.toml` / `__init__.py` 中的版本，保持一致）
3. 合并到 `main`。路径命中 Dockerfile、`pyproject.toml`、`VERSION`、`README.md`、`src/`、`tests/`、`publish.yml` 时，[`ci-templates-publish`](../.github/workflows/publish.yml) 会：
   - `PYTHONPATH=src python3 -m unittest discover -s tests -v`
   - `python3 -m compileall -q src`
   - 若 `ci-templates:v$VERSION` 尚不存在则 `docker build --network host` 并 push
   - 若 `hls-actions-runner:$runner_image_tag` 尚不存在则使用 `$runner_version` 构建并 push runner 镜像
   - 将 digest 固定的 ARC controller 镜像镜像到 Harbor，并把 `deploy/arc` 快照按 CAS 推送到 deploy
   - 已存在的不可变 tag **跳过重建**
4. job summary 输出控制镜像与 runner 镜像的 `image@digest`
5. 消费仓库只需保持 `.ci/pipeline.yaml` 的 runner 版本与 Harbor 不可变 tag 一致；不再在 workflow 里挂控制镜像或宿主机 Docker

不要把未 pin digest 的 controller 镜像或可变 runner tag 用于生产。Harbor 凭据来自 GitHub `release` environment secrets（`HARBOR_DOCKER_CONFIG_JSON`、`HARBOR_CA_PEM`），不进本仓库。runner tag 由 Harbor immutable policy 保护。

本地：

```text
PYTHONPATH=src python3 -m unittest discover -s tests -v
docker build -t ci-templates:dev .
```
