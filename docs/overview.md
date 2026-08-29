# 全景

ci-templates 是组织的 CI/CD **控制面镜像**，不是应用仓库里的 workflow 模板文件。项目仓库只保留：

- 薄的 `.github/workflows/pipeline.yml`（编排 job、挂载密钥、调用本镜像）
- 项目自有 manifest（`CI_PROJECT_CONFIG_JSON` 或 `ci-pipeline.json`、各服务 `deploy/`）

通用逻辑全部在本仓库的 Python CLI 里，经 Docker 发布后按 digest 固定使用。

```text
应用仓库 (Knowledge-Core / Knowledge-Core-Web)
  pipeline.yml  →  docker run ... ci-templates:<version>@sha256:... <command>
        │
        ├─ build / promote-candidate  →  Harbor
        │     harbor.happyladysauce.local/knowledge-core/<service>
        │
        └─ promote-snapshot  →  deploy.git
              Knowledge-Core/deploy/  或  Knowledge-Core-Web/deploy/
              dev/common 镜像 digest
                    │
                    ▼
              Argo CD ApplicationSet
                    │
                    ▼
              argo-wait + smoke  →  release（聚合 tag + GitHub Release）
```

平台 Chart 走另一条线：deploy 仓库的 [k3s/charts.lock.yaml](https://github.com/HappyLadySauceM/deploy/blob/main/k3s/charts.lock.yaml) 交给 `charts-check` / `charts-mirror`，推到 `oci://harbor.happyladysauce.local/helm`。见 [Chart](charts.md) 与 [deploy 变更指南](https://github.com/HappyLadySauceM/deploy/blob/main/docs/change-guide.md)。

## 控制镜像

| 项 | 值 |
| --- | --- |
| 仓库 | `harbor.happyladysauce.local/knowledge-core/ci-templates` |
| Tag | `v` + [VERSION](../VERSION) 文件 |
| 生产引用 | `vVERSION@sha256:...`（digest 必填） |
| 入口 | `ci-templates` |
| 工作目录 | `/workspace` |
| 发布 workflow | [`.github/workflows/publish.yml`](../.github/workflows/publish.yml)，runner 标签 `self-hosted, Linux, X64, devops` |

消费方在成功发布新版本后必须更新自己的 pin。不要跟踪浮动的 `:latest` 或未带 digest 的 tag。步骤见 [发布](release.md)。

## Harbor 标签语义

| 标签 | 含义 |
| --- | --- |
| `:dev` | 当前已提升、集群应拉取的应用镜像 |
| `:previous` | 回滚缓冲（`restore-previous`） |
| `:buildcache` | Buildx 缓存 |
| `:sha-<commit>` | 构建候选；冒烟通过后 `promote-candidate`，再 `cleanup-candidate` |

控制面本身不扫描、不签名这些镜像。

## 密钥边界

- 配置 JSON 只描述路径、仓库名、服务列表，不放密码
- Chart 锁文件不放 registry 账号；Helm 使用运行时 registry 配置
- DeepSeek 只看到脱敏后的 diff 片段，看不到 commit / 分支 / workflow / 凭据元数据
- GitOps 推送使用 `GITOPS_TOKEN`；Release 使用 `GITHUB_TOKEN`；集群操作用 `KUBECONFIG`

谁改 deploy 里的快照、谁改 `k3s/`，见 [deploy README](https://github.com/HappyLadySauceM/deploy/blob/main/README.md)。
