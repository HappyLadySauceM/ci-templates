# 全景

ci-templates 是组织的 CI/CD 控制面；应用仓库只保留薄 workflow 与
`.ci/pipeline.yaml`，不再把 Python 代码、宿主机路径或 Docker socket 带进
项目流水线。

```text
应用仓库 (Knowledge-Core / Knowledge-Core-Web)
  pipeline.yml → hls-standard / hls-builder ARC Scale Set
       │
       ├─ standard: plan、质量门禁、release notes、GitOps、Argo、smoke
       │       └─ GitHub Artifacts：计划、编译产物、candidate digest、摘要
       │
       └─ builder: BuildKit + Pod 内 Docker-in-Docker（matrix，最多 4）
                         │
                         ▼
              Harbor candidate → GitOps digest snapshot
                         │
                         ▼
                    Argo CD ApplicationSet
                         │
                         ▼
              argo-wait + smoke → Harbor active tag + 聚合 Release
```

## ARC 运行池

| 池 | Namespace | 最大 runner | 用途 |
| --- | --- | ---: | --- |
| `hls-standard` | `arc-runners-standard` | 8 | 质量、部署、Argo、smoke、清理 |
| `hls-builder` | `arc-runners-builder` | 4 | 特权 Docker-in-Docker 构建与 prewarm |
| controller | `arc-system` | 2 replicas | 管理两个 Scale Set |

两个池调度到带 `workload.happyladysauce.local/ci=true:NoSchedule` 的专用节点；
标准池不挂宿主机 socket，builder 的 privileged 只存在于隔离 namespace。ARC
values 的非密钥真源在 [deploy/arc](../deploy/arc/)，由 ci-templates 发布时同步
到 deploy 仓库。

## 镜像与 GitOps

- 控制镜像：`control_image_repository:vVERSION`，用于提供 CLI。
- runner 镜像：`runner_image_repository:<runner_version>`，含 Actions runner、
  kubectl、Helm、Rust 工具链和 CLI；Harbor 对该版本 tag 启用不可变策略。
- 应用 candidate：`<service>:sha-<GITHUB_SHA>`；质量门禁通过后由 Harbor API
  将 manifest 挂到配置的 active tag，无需 Docker promotion。
- GitOps snapshot：以 GitOps 分支头为 CAS，写入 `.source-revision` 与 digest
  overrides；禁止 force-push，远端前进时重试。

平台 ARC Chart 从 [k3s/charts.lock.yaml](https://github.com/HappyLadySauceM/deploy/blob/main/k3s/charts.lock.yaml)
审计并镜像到 Harbor OCI，见 [Chart](charts.md)。

## 密钥边界

配置文件只描述路径、仓库、镜像和 smoke 参数。GitHub App、Harbor 凭据、
kubeconfig、DeepSeek key 分别来自 ARC/部署 Secret 或 GitHub `release`
environment；不写入 workflow、日志、artifact 或 Release 正文。

## 控制镜像发布

`ci-templates-publish` 在 `main` 变更后运行测试、构建控制/runner 镜像，并将
controller 镜像从 GHCR 按 digest 镜像到 Harbor。第一次迁移可由受信任旧 runner
或管理员工作站 bootstrap runner 镜像；之后发布 workflow 使用 `hls-builder`
自举。
