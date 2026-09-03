# 飞书 CICD 任务看板

本文是流水线任务看板的配置与运维真源。任务看板把高频 CICD 群卡片替换为一个
持续更新的项目视图；Release、PR、Issue、Review 和评论通知不受影响。

## 架构与边界

```text
GitHub 主流水线（ARC）
  └─ workflow_run: requested / in_progress / completed
       └─ feishu-pipeline-task（ubuntu-latest，与 ARC 隔离）
            ├─ GitHub API：本次提交与贡献者
            ├─ 飞书通讯录：群成员 + GitHub 自定义字段
            └─ 飞书 Task V2：清单、分组、任务、关注人
```

每个仓库的主流水线对应一个长期复用的任务，标题固定为
`CICD：<owner/repository>`。任务保持未完成，状态由它在统一清单
`CICD 流水线` 中所在的自定义分组表示：

| GitHub 事件或结论 | 飞书分组 |
| --- | --- |
| 初始化、尚无运行 | 未触发 |
| `requested`、`in_progress` | 执行中 |
| `success`、`neutral`、`skipped` | 执行完毕 |
| `failure`、`cancelled`、`timed_out`、`action_required`、`stale` | 执行出错 |

终态一直保留，下一次运行开始时再移到“执行中”。同步器在任务 `extra` 中保存
`run_id`、`run_attempt` 和阶段，延迟到达的旧事件不会覆盖新状态。所有成员、
分组操作成功后才写入同步游标，因此 API 中途失败可以安全重跑。监听 workflow
按仓库串行执行，避免多个事件同时读取旧游标后互相覆盖。

## 飞书应用准备

升级现有 **GitHub Actions Listener** 自建应用，不再只依赖群自定义机器人：

1. 启用机器人能力，将机器人加入看板所在群。
2. 开通并发布以下权限：
   - `task:task:write`
   - `task:tasklist:write`
   - `im:chat.members:read`
   - `contact:contact.base:readonly`
   - `contact:user.employee:readonly`
3. 将应用通讯录可见范围覆盖目标群成员。
4. 在管理后台新增 TEXT 或 HREF 类型的 GitHub 身份自定义字段，并开启
   “允许开放平台 API 调用”。记录字段 ID（例如 `C-...`），不要只记录显示名。
5. 成员字段可填写 `login`、`@login` 或 `https://github.com/login`。同一 login
   若对应多个群成员，同步器会拒绝自动关联，避免提醒错误的人。

自定义字段列表接口只返回字段定义。同步器先分页获取目标群成员，再按 50 人一批
读取用户 `custom_attrs`，因此只会关联当前群内成员。匹配成功者作为任务
`follower`（关注人）；这是 Task API 的原生通知机制，不是在普通字符串描述中
伪造 `@`。每次只移除同步器上次自动添加的关注人，人工关注人保持不变。

## GitHub 配置

在接入仓库配置以下 Actions 值：

| 名称 | 位置 | 说明 |
| --- | --- | --- |
| `FEISHU_APP_SECRET` | `release` environment Secret | 自建应用 App Secret |
| `FEISHU_APP_ID` | Repository/Organization Variable | 自建应用 App ID |
| `FEISHU_CHAT_ID` | Repository/Organization Variable | 目标群 Open Chat ID（`oc_...`） |
| `FEISHU_GITHUB_ATTR_ID` | Repository/Organization Variable | GitHub 身份自定义字段 ID |
| `FEISHU_TASK_TRACKER_ENABLED` | Repository/Organization Variable | 完成初始化后设为 `true` |
| `DEEPSEEK_API_KEY` | `release` environment Secret | 终态提示语；调用失败使用固定文案 |

Secret 不得写入 workflow、仓库文件、日志、任务描述或任务 `extra`。App ID、群 ID
和字段 ID 不是密钥，但仍通过 Variables 管理，避免在各仓库复制配置。

## 接入 workflow

监听文件必须存在于仓库默认分支，否则 GitHub 不会触发 `workflow_run`。任务同步
使用 GitHub 托管 runner，避免 ARC 整体故障时错误状态也无法上报。不要 checkout
触发流水线的代码；只读取 GitHub API 和事件正文。

```yaml
name: feishu-pipeline-task

on:
  workflow_dispatch:
  workflow_run:
    workflows: [knowledge-core-pipeline]
    types: [requested, in_progress, completed]

permissions:
  actions: read
  contents: read

concurrency:
  group: ${{ github.repository }}-feishu-pipeline-task
  cancel-in-progress: false

jobs:
  track:
    if: github.event_name == 'workflow_dispatch' || vars.FEISHU_TASK_TRACKER_ENABLED == 'true'
    runs-on: ubuntu-latest
    timeout-minutes: 5
    environment: release
    steps:
      - uses: HappyLadySauceM/ci-templates/.github/actions/feishu-pipeline-task@21d1752cc7f4fda3237cd7229be20ee3e608f1cf
        with:
          operation: ${{ github.event_name == 'workflow_dispatch' && 'provision' || 'sync' }}
          app-id: ${{ vars.FEISHU_APP_ID }}
          app-secret: ${{ secrets.FEISHU_APP_SECRET }}
          chat-id: ${{ vars.FEISHU_CHAT_ID }}
          github-custom-attr-id: ${{ vars.FEISHU_GITHUB_ATTR_ID }}
          workflow-name: knowledge-core-pipeline
          github-token: ${{ github.token }}
          deepseek-api-key: ${{ secrets.DEEPSEEK_API_KEY }}
```

业务流水线末尾的旧 CICD 卡片加切换条件；Release 卡片不要加该条件：

```yaml
- name: Notify Feishu CI
  if: vars.FEISHU_TASK_TRACKER_ENABLED != 'true'
  # existing feishu-notify invocation

- name: Notify Feishu release
  if: needs.deploy-release.result == 'success' && needs.deploy-release.outputs.release_tag != ''
  # existing release-card invocation
```

## 初始化与启用

严格按下面顺序切换，任何一步失败都不要提前关旧卡片：

1. 先发布并验证 `ci-templates` 的任务 Action。
2. 把监听 workflow 推到应用仓库默认分支，保持
   `FEISHU_TASK_TRACKER_ENABLED` 未设置或为 `false`。
3. 配置飞书应用权限、通讯录范围、自定义字段和全部 GitHub Secret/Variables。
4. 手动运行一次 `feishu-pipeline-task`。`workflow_dispatch` 使用 `provision`，会
   幂等创建统一清单、四个分组、群 editor 权限以及“未触发”任务。
5. 在飞书确认任务标题、清单共享范围和四个分组后，将
   `FEISHU_TASK_TRACKER_ENABLED` 设为 `true`。
6. 触发一次成功流水线，再验证一次失败或取消、失败重跑成功。确认状态依次进入
   执行中、终态，并且贡献者关注人正确更新。
7. Core 试点稳定后，按 Knowledge-Core-Web、ci-templates、Skill-Constructor
   顺序接入；每个仓库先 provision，再启用。

Push 流水线通过同分支上一条不同 run 的 SHA 与当前 SHA 比较提交；PR 流水线
读取 PR commits。比较失败时回退当前 head commit；仍无法取到贡献者时回退
`github.actor`。机器人账号和群外用户不会加入任务。

## 日常操作与回滚

- 重跑失败 job：同一 `run_id` 的 `run_attempt` 增加，`in_progress` 会重新进入
  “执行中”，完成后覆盖终态。
- 人工移动任务：下一次有效流水线事件会恢复到真实状态分组。
- 人工完成任务：下一次同步会把 `completed_at` 恢复为 0，继续复用原任务。
- 临时停用：将 `FEISHU_TASK_TRACKER_ENABLED=false`。业务流水线会恢复 CICD 群
  卡片，任务停留在最后状态；Release、PR、Issue 等消息不变。
- 重新初始化：修复配置后手动运行监听 workflow。已有受管任务不会被重置到
  “未触发”，缺少的清单、分组或任务会补齐。

## 验证与排障

```bash
# Action 单测
PYTHONPATH=src python3 -m unittest tests.test_feishu_pipeline_task -v

# 查监听和主流水线
gh run list --workflow feishu-pipeline-task.yml --limit 10
gh run list --workflow pipeline.yml --limit 10

# ARC 故障与任务监听相互独立；此命令只检查业务 runner
kubectl get pods -A
```

| 现象 | 检查 |
| --- | --- |
| workflow 不出现在 Actions | 文件是否已在 GitHub 默认分支；`workflows:` 名称是否与主 workflow 的 `name:` 完全一致 |
| 手动初始化被 skipped | workflow 的 job 条件必须允许 `workflow_dispatch`，不要只检查 enabled 变量 |
| 获取 tenant token 失败 | App ID/Secret 是否同一应用，应用版本是否已发布 |
| 清单或任务返回 403 | `task:task:write`、`task:tasklist:write` 是否发布，清单是否由当前应用拥有 |
| 群成员读取失败 | 机器人是否在群内，是否有 `im:chat.members:read` |
| 自定义字段找不到 | 使用字段 ID；管理员是否开启 API 调用；字段是否为通用 TEXT/HREF |
| 用户始终匹配不到 | 应用通讯录范围、群成员身份、字段值及 `contact:user.employee:readonly` |
| 出现同名清单/分组/任务 | 不自动选择任意一个；先人工确认并清理重复项，再重跑 provision |
| 任务状态没有更新但主 CI 正常 | 查看独立监听 workflow；它运行在 `ubuntu-latest`，不是 ARC |
| 主 CI runner 失联 | `kubectl get pods -A` 检查 ARC controller、listener、ephemeral runner；任务监听仍应写入错误终态 |

同步器对 HTTP 408、429 和 5xx 最多重试三次。非重试错误会让独立监听 workflow
失败，但不会改变原主流水线结论；修复权限或配置后可直接 Re-run jobs。
