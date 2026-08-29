# 消费指南

给要接入或修改应用仓库流水线的人。当前已接入的例子：Knowledge-Core 与 Knowledge-Core-Web 的 `.github/workflows/pipeline.yml`。

## 项目仓库保留什么

- `.github/workflows/pipeline.yml`：job 图、并发、environment、密钥注入、`docker run` 调用
- 流水线配置：环境变量 `CI_PROJECT_CONFIG_JSON`，或文件 `ci-pipeline.json` / `CI_PROJECT_CONFIG`
- 各服务源码、Dockerfile、`deploy/<service>/`、根 `VERSION`（聚合版本）
- 可选的每服务 `version_file`（文档用，不再各自发 Release）

不要把本仓库的 Python 源码 vendoring 进应用仓库。不要在应用仓库重写 promote / charts / release 逻辑。

## 配置从哪来

优先读环境变量 `CI_PROJECT_CONFIG_JSON`（Knowledge-Core 的做法）。未设置时读 `--config` 或 `CI_PROJECT_CONFIG`，默认 `ci-pipeline.json`。

字段表见 [CLI 与配置](cli-and-config.md)。JSON 里只放仓库路径与服务清单，不放 Harbor 密码、kubeconfig 或 GitHub App 私钥。

## 推荐 job 切分

Knowledge-Core 的实际顺序：

```text
push dev
  → plan: validate + changes
      ├─ 有 build_services → 质量门禁 + package-candidates（prewarm + build）
      ├─ 仅 deploy 变更 → deploy-verify（kustomize / 配置校验）
      └─ 有任何发布变更 → release-notes（summarize）
  → deploy-release:
        promote-snapshot → 校验 digest → argo-wait → smoke
        → promote-candidate + release
        → 失败且冒烟未通过则 rollback-snapshot
```

要点：

1. `changes --base $(git rev-parse origin/main) --details-file /state/changes.json` 必须在构建之前跑，并把文件只读挂到 `summarize` / `release`。
2. 有镜像变更时先 `prewarm` 再 `build --tag sha-$GITHUB_SHA`。BuildKit 容器网络不可靠解析外网 registry 时，由 runner 主机把基础镜像填进 Harbor。
3. `promote-snapshot` 用 `CI_GITOPS_IMAGE_OVERRIDES_JSON` 写入 digest；deploy-only 时 overrides 为空对象。
4. `argo-wait --revision <gitops_sha> --services <release_services>` 只等本次受影响服务。
5. `smoke` 在设置了 `KUBECONFIG` 时走集群内置检查；否则执行配置里的 `smoke_command`。
6. `release --summary-file /state/release.md --changes-file /state/changes.json` 不再请求模型。
7. 冒烟已通过后，即使后续 release 步骤失败，也不要 `rollback-snapshot`，以便幂等重试。

## 挂载与环境变量

| 挂载或变量 | 用途 |
| --- | --- |
| `-v $WORKSPACE:/workspace` | 应用源码；容器 `WORKDIR` 为 `/workspace` |
| `-v $STATE_DIR:/state` | `changes.json`、`release.md`、artifact manifest |
| Docker socket + `DOCKER_CONFIG` | `build` / `promote-candidate` / Harbor |
| Harbor CA（如 `CI_REGISTRY_CA_FILE` / `SSL_CERT_FILE`） | 私有 registry TLS |
| `CI_PROJECT_CONFIG_JSON` | 流水线配置 |
| `CI_GITOPS_IMAGE_OVERRIDES_JSON` | promote-snapshot / argo-wait 的 digest |
| `GITOPS_TOKEN` | 向 deploy 仓库 fast-forward 推送 |
| `GITHUB_TOKEN` | `release` / `status` |
| `DEEPSEEK_API_KEY` | `summarize` |
| `KUBECONFIG` | `argo-wait` / `smoke` |
| `BUILD_CPU_PERCENT` | 构建并行比例，默认 75 |
| `CI_RELEASE_CHANGES_FILE` | 未传 `--changes-file` 时的 release 上下文 |

密钥来源是 GitHub environment / Actions secrets，或 runner 上的受控文件。不要把它们写进配置 JSON、锁文件、日志或 Release。

当前 Knowledge-Core 使用 GitHub App token 访问 `Knowledge-Core` 与 `deploy`，把该 token 同时当作 `GITOPS_TOKEN` 与 `GITHUB_TOKEN`。kubeconfig 来自节点 `/etc/rancher/k3s/k3s.yaml` 或 `K3S_RELEASE_KUBECONFIG`。仓库不保存 token 明文。

## Pin 控制镜像

```yaml
env:
  CI_IMAGE: harbor.happyladysauce.local/knowledge-core/ci-templates:v1.1.11@sha256:...
```

本仓库 `ci-templates-publish` 成功后会在 job summary 打出 `image@digest`。消费方必须开 PR 更新 `CI_IMAGE`。未 pin digest 的 tag 不得用于生产 workflow。

Runner 标签与 deploy 宿主机约定一致：`self-hosted`、`Linux`、`X64`、`devops`。不要在该 runner 上跑全局 `docker image prune`；只清理名为 `ci-templates` 的 Buildx 缓存与本项目状态目录。

## 本地试跑

```text
PYTHONPATH=src python3 -m ci_templates validate --config /path/to/ci-pipeline.json
PYTHONPATH=src python3 -m ci_templates changes --config /path/to/ci-pipeline.json --base origin/main
docker build -t ci-templates:dev .
docker run --rm -v "$PWD:/workspace" -e CI_PROJECT_CONFIG_JSON='...' ci-templates:dev changes --base HEAD~1
```

向 GitOps 或 Harbor 推送的命令不要在无凭据的笔记本上对生产仓库执行。
