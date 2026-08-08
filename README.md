# CI Templates

This repository contains the reusable Python CI/CD control image used by project workflows.

The image owns generic operations: configuration validation, changed-service detection, independent service version/tag calculation, Harbor tag operations, GitOps snapshot copying, Argo health checks, smoke command execution, DeepSeek release summaries, and locked Helm chart auditing/mirroring. Project repositories keep only a thin `.github/workflows/pipeline.yml` and project-owned manifests.

The control flow intentionally does not scan or sign images. A project must still keep credentials in the runner or deployment secret manager; secrets never belong in JSON, YAML, source, logs, or release metadata.

Build locally with `python -m pytest` and `docker build -t ci-templates:dev .`. The publish workflow creates the Harbor control image; production workflows should pin it by digest after the first publish.

`charts-check` downloads every chart declared in a version 1 YAML manifest, verifies its source SHA256, applies only explicitly listed template removals or repository-owned replacements, renders twice, and rejects nondeterministic output, duplicate resources, or any rendered `Secret`. `charts-mirror` runs the same gates before pushing the packaged chart to the configured OCI repository and refreshing optional vendored charts. Registry credentials are supplied through Helm's runtime registry configuration; they are never fields in the manifest.
