# 消费指南

给要接入或修改应用仓库流水线的人。ARC 池、`runs-on` 标签和 GitOps 路径见
[ARC 实现](arc.md)。当前已接入的例子：Knowledge-Core 与
Knowledge-Core-Web 的 `.github/workflows/pipeline.yml`。

## 项目仓库保留什么

- `.github/workflows/pipeline.yml`：job 图、并发、environment、密钥注入和
  `hls-standard` / `hls-builder` runner 选择
- `.github/workflows/feishu-notify.yml`：把 GitHub 活动转成飞书卡片；逻辑在
  ci-templates 的复合 Action 里，应用仓不要复制 Python
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
  → notify: 看板未启用时发 CICD 卡；deploy-release 产出 release_tag 时发蓝色发版卡
  → ubuntu-latest task tracker: 看板启用后同步执行中与终态
```

要点：

1. `changes --base <main-sha> --details-file changes.json` 在构建前运行一次；
   `changes.json`、编译产物、candidate digest 和 release summary 都通过
   GitHub Artifacts 跨 job 传递，不依赖宿主机目录。
2. `hls-standard` 只执行质量、GitOps、Argo 和 smoke；`hls-builder` 才允许
   Docker-in-Docker 特权构建。matrix 的 `max-parallel` 不得超过 builder
   Scale Set 的 `maxRunners: 8`。池容量与缓存见 [ARC 实现](arc.md)。
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
| `hls-standard`（最多 8，request 2 CPU / 4Gi，limit 4 CPU / 8Gi） | plan、质量门禁、release notes、GitOps、Argo、smoke、cleanup、飞书通知 |
| `hls-builder`（最多 8，DinD 4 CPU / 4Gi + runner 4 CPU / 1Gi + init 500m / 256Mi；request 更低） | BuildKit / Docker-in-Docker、base image prewarm、service matrix |
| `arc-github-app` | ARC 在每个 runner namespace 注册 ephemeral runner |
| `HARBOR_DOCKER_CONFIG_JSON`、`HARBOR_CA_PEM` | Harbor pull/push 与 TLS |
| `K3S_RELEASE_KUBECONFIG` | deploy-release 的集群访问 |
| `GH_APP_ID`、`GH_APP_PRIVATE_KEY` | 访问源仓库、deploy、status 与 Release |
| `GITOPS_TOKEN` | ci-templates runner snapshot 推送 |
| `DEEPSEEK_API_KEY` | release-notes 的单次摘要，以及 pipeline/publish 末尾 CI 飞书卡的 AI 问候；缺失时使用固定回退文案 |
| `CI_PROJECT_CONFIG` | `.ci/pipeline.yaml` 配置路径 |
| `CI_GITOPS_IMAGE_OVERRIDES_JSON` | promote-snapshot / argo-wait 的 digest |
| `CI_RELEASE_CHANGES_FILE` | 未传 `--changes-file` 时的 release 上下文 |
| `BUILD_CPU_PERCENT` | 构建并行比例，默认 75 |
| `CI_BUILDER_NAME` | 可选的 Buildx builder 名；默认 `ci-templates`，不同名称使用独立状态 |
| `BUILDKIT_IMAGE` | `docker-container` driver 使用的 BuildKit 镜像；ARC builder 固定到 Harbor digest |
| `CI_REGISTRY_CA_FILE` | 可选的 Harbor CA 文件；内容指纹变化会触发 builder 重建 |
| `FEISHU_WEBHOOK_URL` | 组织 Actions secret：飞书自定义机器人 webhook |
| `FEISHU_WEBHOOK_SECRET` | 组织 Actions secret：飞书自定义机器人签名密钥 |

`FEISHU_WEBHOOK_*` 放在组织 Actions secrets，**不要**放进 `release`
environment，否则 PR/Issue 通知会卡在 environment 审批。不要写入
values、SOPS、workflow 明文或日志。

使用本仓库 `publish.yml` 的 `release` environment 也必须配置
`DEEPSEEK_API_KEY` 才能生成真正的 DeepSeek CI 问候；未配置时卡片仍会发送，
但只会显示固定回退文案。应用仓库的 `pipeline.yml` 同理。

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
`BUILDKIT_IMAGE` 控制 `docker-container` driver 的 BuildKit 镜像；
`hls-builder` 使用 Harbor 中与 `moby/buildkit:buildx-stable-1` 相同 digest 的
内部镜像，避免构建启动时访问 Docker Hub。创建 builder 前会显式预拉取该镜像，
让 DinD daemon 使用 workflow 准备好的 Harbor 凭据。
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

## 飞书通知

迁移任务看板前，CI 成败在 `pipeline.yml` / `publish.yml` / Skill-Constructor
`ci.yml` **末尾 job** 发飞书（`if: always()`）。CI 卡 header 为
`CICD：<owner/repository>`，commit 标题显示在正文首行；Workflow、Conclusion、
Branch 等信息使用飞书 Markdown 原生列点。卡片带耗时和 run 链接，**不列
Artifacts**；有 `DEEPSEEK_API_KEY` 时加一句中文问候。
DeepSeek 请求最多重试 3 次；密钥缺失、超时、空响应或失败时记录 warning，
并使用固定中文回退文案，不能阻断 CI 卡片发送。
发版卡只在同一条 pipeline 真正创建了 GitHub Release（`release_tag` 非空）时
发送，蓝色标题，Notes 保留换行且不含正文版本号。
独立 workflow `.github/workflows/feishu-notify.yml` 只覆盖 PR、Issue、Review
和评论，**不再**订阅 `workflow_run` 或 `release`。复合 Action 真源是
[`.github/actions/feishu-notify`](../.github/actions/feishu-notify/)。

### CICD 任务看板

完整配置、接入模板、初始化/启用顺序、回滚与排障以
[飞书 CICD 任务看板](feishu-task-board.md)为准。本节只保留行为和配置摘要。

任务看板接入后，流水线状态不再发群卡片。一个仓库的主流水线对应一个长期
复用的飞书任务；任务保持未完成，用统一清单中的 `未触发`、`执行中`、
`执行完毕`、`执行出错` 四个自定义分组表达状态。共享 Action 真源是
[`.github/actions/feishu-pipeline-task`](../.github/actions/feishu-pipeline-task/)。

监听 workflow 使用 `workflow_run` 的 `requested`、`in_progress` 和 `completed`
事件并运行在 `ubuntu-latest`，这样 ARC 整体不可用时仍可更新错误状态。先手动
执行一次 `operation: provision`，幂等创建清单、分组以及处于 `未触发` 的任务；
后续事件使用 `operation: sync`。重跑没有 `requested` 事件，`in_progress` 会把
任务移回执行中。同步器以 run ID、attempt 和阶段拒绝乱序旧事件。

飞书自建应用需启用机器人能力并加入目标群，至少授予：

- `task:task:write`、`task:tasklist:write`、`task:section:write`
- `im:chat.members:read`
- `contact:contact.base:readonly`、`contact:user.employee:readonly`

管理员还需为保存 GitHub login 的通讯录自定义字段启用“允许开放平台 API
调用”。字段类型只能是 TEXT 或 HREF，值支持 `login`、`@login`、
`https://github.com/login`。同步器只读取目标群成员并按 login 精确匹配，将本次
提交贡献者设为任务关注人；无匹配时回退触发者。人工添加的关注人不会删除。

组织 Actions 配置如下，不要把值提交进 git：

| 名称 | 类型 | 用途 |
| --- | --- | --- |
| `FEISHU_APP_SECRET` | Secret | 飞书自建应用密钥 |
| `FEISHU_APP_ID` | Variable | 飞书自建应用 ID |
| `FEISHU_CHAT_ID` | Variable | 看板共享群 Open Chat ID |
| `FEISHU_GITHUB_ATTR_ID` | Variable | GitHub 身份自定义字段 ID |
| `DEEPSEEK_API_KEY` | Secret | 终态任务提示语；失败时有固定回退文案 |

清单名默认是 `CICD 流水线`，目标群以 editor 身份加入清单。任务 `extra` 保存
同步游标和自动关注人集合；任务更新总是最后写游标，确保中途 API 失败后可重试。

### 机器人与密钥

1. 飞书群添加自定义机器人，安全设置只开**签名校验**（不要用 IP 白名单）。
2. 组织 `HappyLadySauceM` → Settings → Secrets and variables → Actions 增加
   `FEISHU_WEBHOOK_URL`、`FEISHU_WEBHOOK_SECRET`，Repository access 授给要通知
   的仓库。
3. 本仓目前是 public，其它仓可以直接 `uses:` 这个 Action。只有改成
   private/internal 之后，才需要在 Settings → Actions → General 页面向下滚到
   **Access**，选 Accessible from repositories in the HappyLadySauceM
   organization。截图里的 Actions permissions 管的是本仓能引用哪些外部
   Action，不是别人能不能引用本仓。

Webhook POST `https://open.feishu.cn/open-apis/bot/v2/hook/<id>`。签名按飞书
官方算法：`timestamp` 为秒，HMAC-SHA256 的密钥是 `timestamp + "\n" + secret`，
对空消息做摘要再 Base64。ARC runner 经 `HTTP_PROXY`/`HTTPS_PROXY` 访问
`open.feishu.cn`。限流 100 次/分钟、5 次/秒；正文 ≤ 20KB。

### 事件白名单

订阅这些事件，避免占满 `hls-standard`（最多 8 个槽）：

- `pull_request`：`opened` / `reopened` / `closed` / `ready_for_review` /
  `converted_to_draft`（**不含** `synchronize`）
- `pull_request_review` / `submitted`
- `issues`：`opened` / `reopened` / `closed`
- `issue_comment` / `created`
- `workflow_dispatch`：手动试推

不订阅 `push`、`workflow_run` 和 `release`。过滤：`issue_comment` / `pull_request_review`
上的 `github-actions[bot]`、`dependabot[bot]`、`renovate[bot]`。CI 卡标题以
`CICD：` 开头；失败用红色。发版卡由 pipeline notify 在有 tag 时发送。

### 接入顺序

1. 先把本仓库（含 Action）推到 `origin/main`。
2. 应用仓复制 [`.github/workflows/feishu-notify.yml`](../.github/workflows/feishu-notify.yml)，
   把 `uses:` 钉成该 Action 发布提交的 40 位 SHA（每次改 Action 后更新）。本仓自己的 notify
   workflow 和 `publish.yml` 末尾 job 用 `uses: ./.github/actions/feishu-notify`，先 checkout。
3. 本地验证 Action：`PYTHONPATH=src python3 -m unittest tests.test_feishu_notify -v`
4. 在已接入仓上 `workflow_dispatch` 一次 `feishu-notify`，确认群里出现卡片。
   该 workflow 失败不会把 `knowledge-core-pipeline` 打红。

改 Action 之后更新各仓 pin 的 SHA。不要把 webhook 或签名密钥提交进 git。

## 本地试跑

```text
PYTHONPATH=src python3 -m ci_templates validate --config /path/to/.ci/pipeline.yaml
PYTHONPATH=src python3 -m ci_templates changes --config /path/to/.ci/pipeline.yaml --base origin/main
docker build -t ci-templates:dev .
```

向 GitOps 或 Harbor 推送的命令不要在无凭据的笔记本上对生产仓库执行。
