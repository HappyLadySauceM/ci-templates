# CI Templates

This repository contains the reusable Python CI/CD control image used by project workflows.

The image owns generic operations: configuration validation, changed-service detection, independent service version/tag calculation, Harbor tag operations, GitOps snapshot copying, Argo health checks, smoke command execution, and DeepSeek release summaries. Project repositories keep only a thin `.github/workflows/pipeline.yml` and a JSON configuration describing their services.

The control flow intentionally does not scan or sign images. A project must still keep credentials in the runner or deployment secret manager; secrets never belong in JSON, YAML, source, logs, or release metadata.

Build locally with `python -m pytest` and `docker build -t ci-templates:dev .`. The publish workflow creates the Harbor control image; production workflows should pin it by digest after the first publish.
