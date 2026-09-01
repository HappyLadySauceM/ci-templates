# Chart 审计与镜像

平台 Helm Chart 的真源是 deploy 仓库的锁文件 [k3s/charts.lock.yaml](https://github.com/HappyLadySauceM/deploy/blob/main/k3s/charts.lock.yaml)。本镜像提供两条命令：

```text
ci-templates charts-check --manifest k3s/charts.lock.yaml --root .
ci-templates charts-mirror --manifest k3s/charts.lock.yaml --root .
```

在 deploy 仓库根执行。`charts-mirror` 会先跑与 `charts-check` 相同的门禁，通过后再推 OCI 并刷新可选 vendor。

Registry 登录走 Helm 运行时配置（runner 的 Docker/Helm 凭据），**不是**锁文件字段。

## Manifest version 1

根对象必须 `version: 1`，`destination` 必须是 `oci://` URL。`charts` 为非空列表，`name` 唯一。

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `name` | 是 | Chart 名 |
| `repository` | 是 | 上游 Helm 仓库 URL；也支持 `oci://` registry |
| `sourceName` | 否 | 上游实际 Chart 名；用于同一个 OCI Chart 以不同 values 渲染多个锁项 |
| `version` | 是 | 上游版本（下载用） |
| `sha256` | 是 | 上游压缩包的小写 SHA256（64 位 hex） |
| `targetVersion` | 是 | 推到内部 OCI / 写入 Chart.yaml 的版本 |
| `releaseName` | 是 | Helm release 名 |
| `namespace` | 是 | 渲染时的 namespace |
| `values` | 否 | 仓库内 values 路径列表（相对仓库根，禁止 `..`） |
| `includeCRDs` | 否 | 默认 `false` |
| `removeTemplates` | 否 | 要掏空的 Chart 内模板路径 |
| `replaceTemplates` | 否 | Chart 内路径 → 仓库内替换文件 |
| `vendorPath` | 否 | 通过后刷新到该 Git 目录（Harbor 用 `k3s/charts/harbor`） |

示例（结构与 deploy 锁文件一致，数值以锁文件为准）：

```yaml
version: 1
destination: oci://harbor.happyladysauce.local/helm
charts:
  - name: harbor
    repository: https://helm.goharbor.io
    version: 1.19.1
    sha256: 593f47e1ff6cdb58bd571708535ccef436f44ccf842c5f7925cd7b03e827edc1
    targetVersion: 1.19.1-hls.2
    releaseName: harbor
    namespace: harbor
    values:
      - k3s/harbor/base/values.yaml
    removeTemplates:
      - templates/core/core-secret.yaml
    replaceTemplates:
      templates/jobservice/jobservice-dpl.yaml: k3s/chart-patches/harbor/jobservice-dpl.yaml
    vendorPath: k3s/charts/harbor
```

路径必须是安全相对路径。压缩包内不得含绝对路径、`..`、符号链接或设备节点。

## 门禁

`charts-check` 对每一项：

1. 按 `repository` + `version` 下载，校验 SHA256
2. 只执行锁文件声明的 `removeTemplates`（替换为注释占位：「Resource is supplied by the deployment secret manager」）和 `replaceTemplates`
3. 用给定 values **连续渲染两次**，拒绝非确定输出
4. 拒绝重复 Kubernetes 资源
5. 拒绝任何渲染出的 `Secret`（凭据必须由 KSOPS / 部署 Secret 管理器提供）
6. 若声明了 `vendorPath` 且未加 `--allow-missing-vendors`，vendor 目录必须已存在且与处理后的 Chart 一致

`charts-mirror` 通过上述检查后：把包装好的 Chart 推到 `destination`，版本为 `targetVersion`；若有 `vendorPath` 则刷新 Git 内 vendor 副本。

## 与 Argo CD 的关系

多数平台组件的 ApplicationSet `appType` 为 `helm` 或 `helmValues`，从 `harbor.happyladysauce.local/helm` 拉 Chart。Harbor 自身是例外：vendor 进 Git，`appType: gitHelm`。升级与回滚步骤见 [deploy 变更指南](https://github.com/HappyLadySauceM/deploy/blob/main/docs/change-guide.md) 与 [deploy 运维](https://github.com/HappyLadySauceM/deploy/blob/main/docs/operations.md)。
