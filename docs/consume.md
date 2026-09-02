# 消费指南

给要接入或修改应用仓库流水线的人。ARC 池、`runs-on` 标签和 GitOps 路径见
[ARC 实现](arc.md)。当前已接入的例子：Knowledge-Core 与
Knowledge-Core-Web 的 `.github/workflows/pipeline.yml`。

## 项目仓库保留什么

- `.github/workflows/pipeline.yml`：job 图、并发、environment、密钥注入和
  `hls-standard` / `hls-builder` runner 选择
- `.ci/pipeline.yaml`：项目与服务配置（schema v2）
- 各服务源码、Dockerfile、`deploy/<service>/`、根 `VERSION`

不要把本仓库的 Python 源码 vendoring 进应用仓库，也不要在项目仓库重写
build、promote、GitOps 或 release 逻辑。

## 配置从哪来

新项目把配置放进 `.ci/pipeline.yaml`，workflow 通过
`CI_PROJECT_CONFIG=.ci/pipeline.yaml` 和 `--config` 传给 CLI。CLI 仍兼容旧的
`CI_PROJECT_CONFIG_JSON` 与 `ci-pipeline.json`，但显式文件路径优先，避免宿主
机残留环境变量覆盖仓库配置。

字段表见 [CLI 与配置](cli-and-config.md)。配置只放仓库路径、服务清单、镜像
坐标和 smoke 参数；Harbor 密码、kubeconfig、GitHub App 私钥必须来自
GitHub environment secrets 或集群 Secret。

## 推荐 job 切分

```text
push dev
  → plan: validate + changes + release context
      ├─ standard: Go/Rust/Node 质量门禁并上传 GitHub Artifacts
      ├─ builder: prewarm + service matrix 构建不可变 candidate
      └─ standard: release-notes
  → standard deploy-release:
        promote-snapshot(CAS retry) → apply ApplicationSet
        → argo-wait → smoke → Harbor tag promotion + release
        → 失败且冒烟未通过则 rollback-snapshot
  → standard cleanup-candidates
```

要点：

1. `changes --base <main-sha> --details-file changes.json` 在构建前运行一次；
   `changes.json`、编译产物、candidate digest 和 release summary 都通过
   GitHub Artifacts 跨 job 传递，不依赖宿主机目录。
2. `hls-standard` 只执行质量、GitOps、Argo 和 smoke；`hls-builder` 才允许
   Docker-in-Docker 特权构建。matrix 的 `max-parallel` 不得超过 builder
   Scale Set 的 `maxRunners: 1`。池容量与缓存见 [ARC 实现](arc.md)。
3. `build --tag sha-<GITHUB_SHA> --reuse-existing` 使用不可变 candidate；
   重试时复用已存在的远端 manifest，避免覆盖 tag。
4. `promote-snapshot` 以 GitOps 分支头为 CAS，远端前进时重新 clone 并重试；
   禁止 force-push。digest override 写入 deploy 的 kustomization。
5. `argo-wait --revision <gitops-sha> --services <release_services>` 只等待
   本次受影响的 Application，并核对期望 digest。
6. `smoke` 在有 `KUBECONFIG` 且配置了 `smoke_endpoints` 时走 Kubernetes API
   proxy，否则执行配置里的 `smoke_command` 和 `smoke_env`。
7. 冒烟成功后，即使后续 Harbor promotion 或 Release 步骤失败，也不要回滚
   已验证的 GitOps snapshot，以便安全重试。只有候选已成功提升为 active tag
   才由 cleanup job 回收；promotion 失败时保留候选 tag，避免 digest 被垃圾回收。

## Runner 与密钥边界

| Runner / Secret | 用途 |
| --- | --- |
| `hls-standard`（当前最多 4，各 8 CPU / 8Gi） | plan、质量门禁、release notes、GitOps、Argo、smoke、cleanup |
| `hls-builder`（当前最多 1，DinD 12 CPU / 12Gi + runner 12 CPU / 2Gi） | BuildKit / Docker-in-Docker、base image prewarm、service matrix |
| `arc-github-app` | ARC 在每个 runner namespace 注册 ephemeral runner |
| `HARBOR_DOCKER_CONFIG_JSON`、`HARBOR_CA_PEM` | Harbor pull/push 与 TLS |
| `K3S_RELEASE_KUBECONFIG` | deploy-release 的集群访问 |
| `GH_APP_ID`、`GH_APP_PRIVATE_KEY` | 访问源仓库、deploy、status 与 Release |
| `GITOPS_TOKEN` | ci-templates runner snapshot 推送 |
| `DEEPSEEK_API_KEY` | release-notes 的单次摘要 |
| `CI_PROJECT_CONFIG` | `.ci/pipeline.yaml` 配置路径 |
| `CI_GITOPS_IMAGE_OVERRIDES_JSON` | promote-snapshot / argo-wait 的 digest |
| `CI_RELEASE_CHANGES_FILE` | 未传 `--changes-file` 时的 release 上下文 |
| `BUILD_CPU_PERCENT` | 构建并行比例，默认 75 |
| `CI_BUILDER_NAME` | 可选的 Buildx builder 名；默认 `ci-templates`，不同名称使用独立状态 |
| `CI_REGISTRY_CA_FILE` | 可选的 Harbor CA 文件；内容指纹变化会触发 builder 重建 |

标准 runner 不挂宿主机 Docker socket；builder 使用 Pod 内隔离的 Docker-in-
Docker，并将 Harbor CA 以 Secret 挂入 daemon。每个 runner Pod 的 workspace
和 runner 临时目录都是 ephemeral。节点语言/工具缓存在 `/var/lib/hls-ci-cache`，
由 ARC 挂到 `/cache`，workflow 不要自己去挂，也不要启用 GitHub Actions cache。
不要在 workflow 中写 `/opt/actions-runner`、`/etc/rancher/k3s`、
`/var/lib/rancher` 或宿主机 `_cache`。

GitHub environment secrets 需要在所有读取它们的 job 上声明
`environment: release`。ARC 的 GitHub App 只授予 organization
self-hosted-runner read/write，不授予仓库 contents。

Buildx builder 默认名为 `ci-templates`，用于在同一 runner 上跨服务复用
BuildKit cache。需要在同一 Docker daemon 上隔离不同流水线时，可通过
`CI_BUILDER_NAME` 指定 1–63 个字符的安全名称（仅字母、数字、`.`, `_`, `-`）；
每个名称使用独立的资源 marker。未设置时保持历史 marker 路径不变。
设置 `CI_REGISTRY_CA_FILE` 时，marker 只保存 CA 文件的 SHA-256 指纹；CA 内容
变化会受控重建 builder，不会写入 marker。

## 控制镜像与 runner 镜像

控制镜像发布到 `control_image_repository`，runner 镜像发布到
`runner_image_repository:<runner_image_tag>`；`runner_version` 固定 Actions Runner
二进制版本，runner 镜像内还包含 kubectl、Helm、Rust 工具链和 `ci-templates` CLI。首次迁移时可
从受信任旧 runner 或管理员工作站 bootstrap 一次，之后发布 workflow 使用
`hls-builder` 自举。

controller 镜像使用 digest pin；runner tag 必须在 Harbor 启用不可变 tag。ARC
values、namespace、Secret、专用 CI 节点和 chart mirror 的职责划分见
[ARC 实现](arc.md)，不要把私钥或账号写入 values。

## 控制镜像（非 ARC 场景）

接在 `hls-standard` / `hls-builder` 上的应用 workflow **直接调用 runner 镜像里的
`ci-templates` CLI**，不必设置 `CI_IMAGE`，也不必开 PR 去 pin 控制镜像 digest。

只有在不用 ARC runner 镜像、仍要单独 `docker run` 控制镜像时，才 pin digest：

```yaml
env:
  CI_IMAGE: harbor.happyladysauce.local/knowledge-core/ci-templates:v1.1.12@sha256:...
```

本仓库 `ci-templates-publish` 成功后会在 job summary 打出 `image@digest`。未 pin
digest 的可变 tag 不得用于那种非 ARC 的生产调用。

## 本地试跑

```text
PYTHONPATH=src python3 -m ci_templates validate --config /path/to/.ci/pipeline.yaml
PYTHONPATH=src python3 -m ci_templates changes --config /path/to/.ci/pipeline.yaml --base origin/main
docker build -t ci-templates:dev .
```

向 GitOps 或 Harbor 推送的命令不要在无凭据的笔记本上对生产仓库执行。
