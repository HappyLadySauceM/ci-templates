# ARC runner baseline

This directory is the non-secret source of truth for the organization runner
scale sets. It is synchronized into the GitOps repository by the
`ci-templates` release workflow. Environment overlays in `deploy` provide the
GitHub App Secret, private-registry pull secret, node labels and cluster-local
policy; no credential belongs here.

The controller is installed in `arc-system`. Runner pods are split between
`arc-runners-standard` (direct runner pods, max four at 8 CPU / 8 GiB each) and
`arc-runners-builder` (privileged Docker-in-Docker, max one at 6 GiB total).
The
two pools must be scheduled only on nodes labelled
`workload.happyladysauce.local/ci=true`.

Before enabling the ApplicationSet, the deploy repository must contain the
SOPS-managed `arc-github-app` Secret in both runner namespaces. Its data keys
are `github_app_id`, `github_app_installation_id`, and
`github_app_private_key`; the App must have organization self-hosted-runner
read/write access and no repository contents access. The same namespaces need
the deploy-managed `arc-registry-pull` image-pull Secret and `arc-registry-ca`
CA Secret. Both runner namespaces and `arc-system` also require the
environment-managed `arc-proxy` Secret containing upper- and lower-case HTTP(S)
proxy and no-proxy variables, because listeners run in `arc-system`. These are
intentionally prerequisites rather than generated here.

Provision dedicated tainted CI workers before raising either cap. The current
single-node pool reserves 32 CPU / 32 GiB for four standard runners and 6 GiB
for one builder pod; node hostPath `/var/lib/hls-ci-cache` holds language
caches. The ARC controller itself is highly available with two
replicas.
