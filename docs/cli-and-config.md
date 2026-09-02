# CLI 与配置

命令入口是 `ci-templates`（`python3 -m ci_templates` 等价）。配置来自 `--config` / `CI_PROJECT_CONFIG`，也兼容旧的 `CI_PROJECT_CONFIG_JSON`；未指定时依次查找 `.ci/pipeline.yaml`、`.ci/pipeline.yml`、`ci-pipeline.yaml`、`ci-pipeline.json`。

## 命令

| 命令 | 主要参数 | 作用 |
| --- | --- | --- |
| `validate` | `--config` | 加载配置，打印 `{"project","services"}` |
| `changes` | `--base`（必填）、`--head`（默认 `HEAD`）、`--details-file` | 按路径分类服务；可选写出脱敏 release 上下文 |
| `versions` | `--service`、`--repo` | 读 `version_file`，计算下一 patch 的服务 tag |
| `snapshot` | `--source`、`--target` | 本地复制 deploy 目录 |
| `promote-snapshot` | `--source-sha`（必填）、`--deploy-source`（默认 `deploy`） | 克隆 GitOps、复制快照、写 digest 与 `.source-revision`、fast-forward 推送 |
| `rollback-snapshot` | `--revision` | revert 指定 GitOps 提交并推送 |
| `build` | `--service`、`--tag`（默认 `dev`）、`--artifact-manifest`、`--preserve-previous`、`--reuse-existing` | BuildKit 构建并推送；不可变候选 tag 重试时可复用远端镜像 |
| `prewarm` | | 把 `base_images` 预热到 Harbor |
| `promote-candidate` | `--service`、`--tag` | 通过 Harbor tag API 把候选 manifest 提升为配置的 active tag，不需要 Docker daemon |
| `cleanup-candidate` | `--service`、`--tag` | 删除 Harbor 候选 tag |
| `cleanup-previous` | `--service` | 确认后删除 Harbor `:previous` |
| `restore-previous` | `--service` | 从 `:previous` 恢复 `:dev` |
| `argo-wait` | `--revision`、`--services`（逗号分隔） | 等待 Application Synced + Healthy，并可匹配期望 digest |
| `smoke` | `--repo` | 配置了 `smoke_endpoints` 且有 `KUBECONFIG` 时跑 API proxy 检查，否则执行 `smoke_command` |
| `summarize` | `--services`、`--changes-file`、`--output` | 一次 DeepSeek 请求，写出权限 `0600` 的正文文件 |
| `release` | `--services`、`--changes-file`、`--summary-file` | fast-forward `main`、打聚合 tag、创建 GitHub Release |
| `status` | `--sha`、`--state`、`--description`、`--context`、`--target-url` | GitHub commit status |
| `charts-check` | `--manifest`、`--root`、`--allow-missing-vendors` | 审计锁定 Chart |
| `charts-mirror` | `--manifest`、`--root` | 审计后推 OCI 并刷新 vendor |

`changes` 的 JSON 输出字段：

| 字段 | 含义 |
| --- | --- |
| `build_services` | 源码 / Dockerfile / `shared_paths` 变更，需要重建镜像 |
| `deploy_services` | `deploy/<svc>` 变更，只需更新 GitOps |
| `release_services` | `build ∪ deploy`，进入 Release 与 argo-wait |
| `deploy_changed` | 是否存在部署路径变更 |
| `paths` | 变更路径列表 |
| `services` | 与 `release_services` 相同（兼容字段） |

只改 Dockerfile、没有业务路径时，变更归入 Shared bucket，不单独开服务章节。

`argo-wait`：传入 `--services` 时等待各服务的 `{kustomize_name}-dev`，并用 `CI_GITOPS_IMAGE_OVERRIDES_JSON` 对齐 digest；未传服务时回退到配置里的单个 `argocd_application`。需要 `argocd_server`。

`smoke`：存在 `KUBECONFIG` 且配置了 `smoke_endpoints` 时，对配置的 namespace 做 API proxy readiness 检查；否则把 `smoke_env` 注入后执行 `smoke_command`。

`release` 必须能读到 `--changes-file` 或 `CI_RELEASE_CHANGES_FILE`。上下文里的 `head` 必须等于当前 checkout。不支持 `CI_RELEASE_METADATA_JSON`。

## Pipeline 字段

对应 `src/ci_templates/config.py` 的 `Pipeline`。

| 字段 | 必填 | 默认 | 含义 |
| --- | --- | --- | --- |
| `project` | 是 | | 项目名 |
| `source_repo` | 是 | | GitHub `owner/repo`，用于 Release / status |
| `gitops_repo` | 是 | | deploy 仓库 URL |
| `gitops_path` | 是 | | 快照写入的子目录，例如 `Knowledge-Core` |
| `gitops_branch` | 是 | | 通常 `main` |
| `services` | 是 | 非空列表 | 见下表 |
| `gitops_kustomization` | 否 | `kustomization.yaml` | 写镜像 digest 的文件，Knowledge-Core 用 `dev/common/kustomization.yaml` |
| `shared_paths` | 否 | `[]` | 变更即触发全量 rebuild 的路径 |
| `harbor_registry` | 否 | `harbor.happyladysauce.local` | Harbor 主机 |
| `harbor_project` | 否 | `knowledge-core` | Harbor 项目 |
| `argocd_server` | 否 | `""` | `argo-wait` 需要非空 |
| `argocd_application` | 否 | `""` | 未指定 `--services` 时等待的 Application |
| `smoke_command` | 否 | `[]` | 无 kubeconfig 时的本地冒烟 |
| `release_model` | 否 | `git-independent-service` | 文档/兼容字段 |
| `deepseek_model` | 否 | `deepseek-v4-flash` | `summarize` 模型 |
| `aggregate_release_prefix` | 否 | 由 project 生成 slug | 兼容旧 prefix tag |
| `aggregate_version_file` | 否 | `VERSION` | 聚合版本文件 |
| `release_language` | 否 | `en` | 摘要语言（Knowledge-Core 用 `zh-CN`） |
| `base_images` | 否 | `[]` | `{source,destination}` 列表，供 `prewarm` |
| `deploy_root` | 否 | `deploy` | 相对路径；禁止 `..` 与绝对路径 |
| `schema_version` | 否 | `1` | v2 启用项目 slug 默认值与显式 smoke/runner 配置 |
| `argocd_namespace` | 否 | `argocd` | Argo CD API namespace |
| `application_suffix` | 否 | `-dev` | 服务 kustomize 名到 Application 名的后缀 |
| `smoke_namespace` / `smoke_endpoints` | 否 | 空 | 集群 smoke 的 namespace 与 `{service,port,path}` 列表 |
| `smoke_env` | 否 | `{}` | 无 kubeconfig smoke 命令的环境变量映射 |
| `active_image_tag` / `previous_image_tag` / `cache_image_tag` | 否 | `dev` / `previous` / `buildcache` | 镜像生命周期与 BuildKit cache 标签 |
| `candidate_tag_template` | 否 | `sha-{sha}` | 候选 manifest 标签模板 |
| `git_identity_name` / `git_identity_email` | 否 | `ci-bot` / `ci-bot@noreply.local` | GitOps 提交身份 |
| `status_context` | 否 | `<project-slug>/smoke` | GitHub commit status context |
| `buildkit_reserved_space` / `buildkit_max_used_space` / `buildkit_min_free_space` | 否 | `2GB` / `8GB` / `50GB` | BuildKit GC 水位 |
| `control_image_repository` / `runner_image_repository` / `runner_version` / `runner_image_tag` | 否 | 空 / 空 / `2.337.0` / 跟随 `runner_version` | CI 控制镜像、Actions Runner 二进制版本与不可变 ARC runner 镜像标签 |

## Service 字段

| 字段 | 必填 | 默认 | 含义 |
| --- | --- | --- | --- |
| `name` | 是 | | 服务名；禁止 `/`、`\`、`..` |
| `source_path` | 是 | | 源码目录 |
| `version_file` | 是 | | 文档用版本文件 |
| `dockerfile` | 是 | | Dockerfile 路径 |
| `context` | 是 | | 构建上下文 |
| `image_repository` | 是 | | Harbor 仓库，不含 tag |
| `deploy_snapshot` | 是 | | 应用仓库内快照源，例如 `deploy/gateway` |
| `kustomize_name` | 否 | v2 为 `<project-slug>-<name>`；v1 保留 `knowledge-core-<name>` | Argo Application 名为 `{kustomize_name}{application_suffix}` |
| `artifact_group` | 否 | `""` | 质量制品分组（例如 `go`、`rust`），供 matrix 流水线选择编译器 |

服务名必须唯一。

## 环境变量（命令行为）

| 变量 | 谁读 | 作用 |
| --- | --- | --- |
| `CI_PROJECT_CONFIG_JSON` | 配置加载 | 兼容旧 workflow 的内联 JSON（显式文件配置优先用于新 workflow） |
| `CI_PROJECT_CONFIG` | 配置加载 | 配置文件路径 |
| `CI_GITOPS_IMAGE_OVERRIDES_JSON` | promote-snapshot / argo-wait | kustomize 名 → `{newName,digest}` |
| `CI_RELEASE_CHANGES_FILE` | release | 未传 `--changes-file` 时的路径 |
| `GITOPS_TOKEN` | gitops | 推送 deploy |
| `GITHUB_TOKEN` | github | tag / Release / status |
| `DEEPSEEK_API_KEY` | release | 模型请求 |
| `KUBECONFIG` | argo-wait / smoke | 集群访问 |
| `APPLICATION_NAMESPACE` | smoke | 覆盖配置中的 smoke namespace |
| `BUILD_CPU_PERCENT` | build | 并行比例 |
| `BUILD_JOBS` | build | 紧急覆盖并行数 |
| `HARBOR_USERNAME` / `HARBOR_PASSWORD` | harbor | 删除 tag；也可用 Docker config |

Chart 命令不读 Pipeline JSON，只读 `--manifest`。见 [Chart](charts.md)。
