# CI Templates

This repository contains the reusable Python CI/CD control image used by project workflows.

The image owns generic operations: configuration validation, changed-service detection, independent service version/tag calculation, Harbor tag operations, GitOps snapshot copying, Argo health checks, smoke command execution, aggregated DeepSeek functional summaries, and locked Helm chart auditing/mirroring. Project repositories keep only a thin `.github/workflows/pipeline.yml` and project-owned manifests.

The control flow intentionally does not scan or sign images. A project must still keep credentials in the runner or deployment secret manager; secrets never belong in JSON, YAML, source, logs, or release metadata. Release summaries consume a bounded, redacted diff context from a temporary file. Commit, branch, workflow, and credential metadata is never sent to DeepSeek or written to a release body.

Projects keep service versions isolated in each configured service `version_file`. A successful promotion pushes those service tags and one aggregate project tag (`<aggregate_release_prefix>-vMAJOR.MINOR.PATCH`), then creates one GitHub Release containing a functional-summary section for every affected service. The root `aggregate_version_file` controls only the aggregate tag. Retrying a promotion reuses tags and the Release when they already target the same commit and refuses conflicting tags.

The workflow should run `changes --base <main-sha> --details-file <runner-temp-file>` before the build and mount that file read-only into `release`. The release command accepts the same path through `--changes-file` or `CI_RELEASE_CHANGES_FILE`; the legacy `CI_RELEASE_METADATA_JSON` input is intentionally unsupported.

For a one-time migration from older service Releases, verify their tags first, create the new aggregate Release at the promoted SHA with the change context, then run `gh release delete <legacy-tag> --yes` without `--cleanup-tag`. This removes only the GitHub Release record and preserves the Git tag.

Build locally with `PYTHONPATH=src python3 -m unittest discover -s tests -v` and `docker build -t ci-templates:dev .`. The pinned `ci-templates-publish` workflow publishes the version from `VERSION` to Harbor; production workflows should pin that image by digest after the first successful publish.

`charts-check` downloads every chart declared in a version 1 YAML manifest, verifies its source SHA256, applies only explicitly listed template removals or repository-owned replacements, renders twice, and rejects nondeterministic output, duplicate resources, or any rendered `Secret`. `charts-mirror` runs the same gates before pushing the packaged chart to the configured OCI repository and refreshing optional vendored charts. Registry credentials are supplied through Helm's runtime registry configuration; they are never fields in the manifest.
