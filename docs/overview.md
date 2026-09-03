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
       └─ builder: BuildKit + Pod 内 Docker-in-Docker（最多 8，与 standard 超卖共节点）
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

组织 self-hosted runner 由 ARC Scale Set 提供，叙事真源见 [ARC 实现](arc.md)。
当前 `maxRunners`：`hls-standard` 8（request 4 CPU / 4Gi，limit 8 CPU / 8Gi），
`hls-builder` 8（DinD `"4"` / 4Gi + runner `"4"` / 1Gi + init `500m` /
`256Mi`，CPU/内存 limit 配额 68/42Gi，request 仍 64/40Gi）。两个池调度到
`workload.happyladysauce.local/ci=true`；standard 不挂宿主机 socket，builder
的 privileged 只存在于隔离 namespace。

非密钥 values 在 [deploy/arc](../deploy/arc/)，由 `ci-templates-publish` 同步到
deploy 仓库。集群 Secret 与节点引导见
[deploy/docs/ci-runners.md](https://github.com/HappyLadySauceM/deploy/blob/main/docs/ci-runners.md)。

## 镜像与 GitOps

- 控制镜像：`control_image_repository:vVERSION`，用于提供 CLI。
- runner 镜像：`runner_image_repository:<runner_image_tag>`，含由 `runner_version` 固定的 Actions runner、
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

## 飞书任务看板

流水线状态可通过独立的 `workflow_run` 监听同步到统一飞书任务清单。监听运行在
GitHub 托管 runner，与 ARC 故障域隔离；一个项目主流水线复用一个任务，通过
`未触发`、`执行中`、`执行完毕`、`执行出错` 四个分组展示当前状态。本次提交
贡献者按飞书通讯录 GitHub 自定义字段匹配，并作为任务关注人。

应用权限、Secret/Variable、接入 workflow、初始化、切换、回滚与排障见
[飞书 CICD 任务看板](feishu-task-board.md)。

## 控制镜像发布

`ci-templates-publish` 在 `main` 变更后运行测试、构建控制/runner 镜像，并将
controller 镜像从 GHCR 按 digest 镜像到 Harbor，再把 `deploy/arc` 快照推到
deploy。第一次迁移可由受信任旧 runner 或管理员工作站 bootstrap runner 镜像；
之后发布 workflow 使用 `hls-builder` 自举。详见 [ARC 实现](arc.md)。
