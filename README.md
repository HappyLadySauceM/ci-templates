# CI Templates

This repository contains the reusable Python CI/CD control image used by project workflows.

The image owns generic operations: configuration validation, changed-service detection, independent service version/tag calculation, Harbor tag operations, GitOps snapshot copying, Argo health checks, smoke command execution, aggregated DeepSeek functional summaries, and locked Helm chart auditing/mirroring. Project repositories keep only a thin `.github/workflows/pipeline.yml` and project-owned manifests.

The control flow intentionally does not scan or sign images. A project must still keep credentials in the runner or deployment secret manager; secrets never belong in JSON, YAML, source, logs, or release metadata. Release summaries consume a bounded, redacted diff context from a temporary file. Commit, branch, workflow, and credential metadata is never sent to DeepSeek or written to a release body.

Projects keep optional service `version_file` values for documentation. A successful promotion pushes only one aggregate project tag (`vMAJOR.MINOR.PATCH`) and creates one GitHub Release whose title is the same version tag. Shared/CI/build changes are summarized once under **Shared changes**; service sections appear only when `services/<svc>` or `deploy/<svc>` business paths changed. Dockerfile-only rebuilds stay in the shared section. The release lists **Deployed services** for the images rebuilt in that run. The root `aggregate_version_file` controls the aggregate tag. Retrying a promotion reuses the aggregate tag and Release when they already target the same commit and refuses conflicting tags. Existing `<aggregate_release_prefix>-vMAJOR.MINOR.PATCH` tags still count when choosing the next patch or retrying a commit that already has a prefixed tag.

The workflow should run `changes --base <main-sha> --details-file <runner-temp-file>` before the build and mount that file read-only into `release`. The release command accepts the same path through `--changes-file` or `CI_RELEASE_CHANGES_FILE`; the legacy `CI_RELEASE_METADATA_JSON` input is intentionally unsupported.

For a one-time migration from older service Releases, verify their tags first, create the new aggregate Release at the promoted SHA with the change context, then run `gh release delete <legacy-tag> --yes` without `--cleanup-tag`. This removes only the GitHub Release record and preserves the Git tag.

Build locally with `PYTHONPATH=src python3 -m unittest discover -s tests -v` and `docker build -t ci-templates:dev .`. The pinned `ci-templates-publish` workflow publishes the version from `VERSION` to Harbor; production workflows should pin that image by digest after the first successful publish.

Image builds reuse a stable Buildx builder named `ci-templates` so BuildKit cache mounts survive across services on the same runner. Compile/build parallelism is capped at three CPUs and respects the runner cgroup affinity. BuildKit garbage collection keeps the builder below its configured disk watermarks. Projects may pass a SHA256-checked artifact manifest to `build`; this allows runtime-only Dockerfiles to package binaries produced by the quality job without compiling again.

For aggregate releases, `summarize --changes-file ... --services ... --output ...` performs one bounded DeepSeek request and writes a reusable release body. `release --summary-file ...` consumes that body without making another model request.

`charts-check` downloads every chart declared in a version 1 YAML manifest, verifies its source SHA256, applies only explicitly listed template removals or repository-owned replacements, renders twice, and rejects nondeterministic output, duplicate resources, or any rendered `Secret`. `charts-mirror` runs the same gates before pushing the packaged chart to the configured OCI repository and refreshing optional vendored charts. Registry credentials are supplied through Helm's runtime registry configuration; they are never fields in the manifest.
