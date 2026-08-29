# CI Templates

本仓库提供组织复用的 Python CI/CD **控制镜像**。通用操作都在镜像里：配置校验、变更服务检测、版本/tag 计算、Harbor 标签、GitOps 快照、Argo 健康等待、冒烟、聚合 DeepSeek 发布摘要，以及锁定 Helm Chart 的审计与 OCI 镜像。项目仓库只保留薄的 `.github/workflows/pipeline.yml` 和项目自有 manifest。

当前版本见 [VERSION](VERSION)。发布到 Harbor 的镜像为：

```text
harbor.happyladysauce.local/knowledge-core/ci-templates:vMAJOR.MINOR.PATCH
```

生产 workflow **按 digest pin** 该镜像。`ci-templates-publish` 在推到 `main` 且相关路径变更时，把 `VERSION` 对应的 tag 推到 Harbor；已存在的不可变 tag 会跳过重建。

本控制流**不扫描、不签名**镜像。凭据只放在 runner 或部署 Secret 管理器里；密码、token、私钥不得出现在 JSON、YAML、源码、日志或 Release 正文。发布摘要只消费一份有界、已脱敏的 diff 上下文（临时文件）。commit、分支、workflow 和凭据元数据不会发给 DeepSeek，也不会写入 Release。

## 读者地图

| 你是谁 | 先读 |
| --- | --- |
| 要理解控制面与 deploy / Harbor / Argo 的关系 | [全景](docs/overview.md) |
| 要给应用仓库接流水线 | [消费指南](docs/consume.md) |
| 要查命令或 `CI_PROJECT_CONFIG` 字段 | [CLI 与配置](docs/cli-and-config.md) |
| 要审计或镜像平台 Chart | [Chart](docs/charts.md)；锁文件在 [deploy](https://github.com/HappyLadySauceM/deploy/blob/main/k3s/charts.lock.yaml) |
| 要做聚合发布或升级本镜像 | [发布](docs/release.md) |

GitOps 真源与「谁改哪」见 [deploy 文档](https://github.com/HappyLadySauceM/deploy)。

## 命令地图

`ENTRYPOINT` 是 `ci-templates`。workflow 里典型调用：

```text
docker run --rm --network host \
  -v "$GITHUB_WORKSPACE:/workspace" \
  -e CI_PROJECT_CONFIG_JSON \
  -w /workspace "$CI_IMAGE" <command>
```

| 命令 | 作用 |
| --- | --- |
| `validate` | 加载流水线配置 |
| `changes` | 输出 `build_services` / `deploy_services` / `release_services` |
| `versions` | 读服务 `version_file` 并计算下一 patch tag |
| `snapshot` / `promote-snapshot` / `rollback-snapshot` | 本地复制或推送到 deploy 仓库 |
| `build` / `prewarm` / `promote-candidate` / `cleanup-candidate` | 镜像构建与 Harbor 标签 |
| `cleanup-previous` / `restore-previous` | `:dev` / `:previous` 生命周期 |
| `argo-wait` | 等待 Application Synced + Healthy |
| `smoke` | 项目 `smoke_command` 或集群内置冒烟 |
| `summarize` / `release` | 一次模型摘要 + 聚合 GitHub Release |
| `status` | 写 GitHub commit status |
| `charts-check` / `charts-mirror` | 锁定 Chart 审计与 OCI 推送 |

完整参数与配置 schema 见 [CLI 与配置](docs/cli-and-config.md)。

`changes` 必须在构建前用 `--base <main-sha> --details-file <runner-temp-file>` 跑一次，并把该文件只读挂进后续的 `summarize` / `release`。`release` 通过 `--changes-file` 或 `CI_RELEASE_CHANGES_FILE` 读取同一路径；旧的 `CI_RELEASE_METADATA_JSON` **不再支持**。

只改 `deploy/<service>/` 时，流水线更新 GitOps 快照并做部署校验，不重建镜像。快照推送在远端分支已前进时会克隆最新提交并重放本项目范围内的快照；**禁止 force-push**。

## 聚合发布（摘要）

成功提升只推送一个项目级 tag：`vMAJOR.MINOR.PATCH`，并创建一个标题相同的 GitHub Release。根目录 `aggregate_version_file` 控制该版本。服务自己的 `version_file` 可保留作文档，不再各自发 Release。

- 共享 / CI / 构建变更汇总在 **Shared changes**
- 只有 `services/<svc>` 或 `deploy/<svc>` 业务路径变更时才出现服务章节
- 只改 Dockerfile 的重建归入 Shared
- Release 列出 **Affected services**
- 仓库根级、仅部署相关的变更记为共享部署配置

同 commit 重试会复用已指向该 commit 的聚合 tag 与 Release，冲突 tag 会被拒绝。历史上的 `<aggregate_release_prefix>-vMAJOR.MINOR.PATCH` 在选下一 patch 或重试时仍计入。

`summarize --changes-file ... --services ... --output ...` 只发一次有界 DeepSeek 请求并写出 Release 正文；`release --summary-file ...` 复用该文件，不再请求模型。

从旧的按服务 Release 做一次性迁移：先核对旧 tag，在已提升的 SHA 上用变更上下文创建新的聚合 Release，再执行 `gh release delete <legacy-tag> --yes`（不要加 `--cleanup-tag`），只删 GitHub Release 记录、保留 Git tag。细节见 [发布](docs/release.md)。

## 构建并行与制品

镜像构建复用名为 `ci-templates` 的稳定 Buildx builder，以便同一 runner 上跨服务保留 BuildKit cache。编译并行默认是 runner 有效 CPU（亲和性与 cgroup 配额）的 75%，至少 1。用 `BUILD_CPU_PERCENT` 调整比例；`BUILD_JOBS` 仅作有界紧急覆盖。BuildKit GC 按配置水位回收。

`build` 可接收一份经 SHA256 校验的 artifact manifest，让只负责打包的 Dockerfile 使用质量作业已经编好的二进制，而不再编译一次。

## 本地验证

```text
PYTHONPATH=src python3 -m unittest discover -s tests -v
docker build -t ci-templates:dev .
```

需要 Python 3.10+。镜像内还包含 git、jq、docker CLI / buildx、skopeo、kubectl、helm。
