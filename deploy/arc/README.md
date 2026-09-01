# ARC runner baseline

This directory is the non-secret source of truth for the organization runner
scale sets. It is synchronized into the GitOps repository by the
`ci-templates` release workflow. Environment overlays in `deploy` provide the
GitHub App Secret, private-registry pull secret, node labels and cluster-local
policy; no credential belongs here.

The controller is installed in `arc-system`. Runner pods are split between
`arc-runners-standard` (direct runner pods, max eight) and
`arc-runners-builder` (privileged Docker-in-Docker, max four). The two pools
must be scheduled only on nodes labelled `workload.happyladysauce.local/ci=true`.

Before enabling the ApplicationSet, the deploy repository must contain the
SOPS-managed `arc-github-app` Secret in both runner namespaces. Its data keys
are `github_app_id`, `github_app_installation_id`, and
`github_app_private_key`; the App must have organization self-hosted-runner
read/write access and no repository contents access. The same namespaces need
the deploy-managed `arc-registry-pull` image-pull Secret and `arc-registry-ca`
CA Secret. These are intentionally prerequisites rather than generated here.

Provision at least two tainted CI worker nodes (minimum 32 vCPU, 64 GiB RAM,
250 GiB local SSD each), label them with the key above, and keep ordinary
workloads from that taint. The quotas reserve capacity for eight standard and
four builder pods; the ARC controller itself is highly available with two
replicas.
