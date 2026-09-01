ARG PYTHON_BASE_IMAGE=python:3.12-slim@sha256:e5c9fa26ffb76e11e0f054f30dc2523a2f9693f0c36c0cf1e39b27e152d899fc
ARG KUBECTL_VERSION=v1.36.2
ARG HELM_VERSION=v3.21.3
ARG HELM_AMD64_SHA256=15e041a93a590dce8100f39385cd98c84a765c9e36aeeb9e2dc6ff9e4769e2e0
ARG HELM_ARM64_SHA256=67f58155079ff9ffab98ba5c88daff0ed9b542f3a4732f5dd426dde7dd0f5244
FROM ${PYTHON_BASE_IMAGE}

ARG KUBECTL_VERSION
ARG HELM_VERSION
ARG HELM_AMD64_SHA256
ARG HELM_ARM64_SHA256

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git jq openssh-client docker-cli docker-buildx skopeo \
    && rm -rf /var/lib/apt/lists/* \
    && arch="$(dpkg --print-architecture)" \
    && case "$arch" in amd64) kubectl_arch=amd64; helm_sha256="${HELM_AMD64_SHA256}" ;; arm64) kubectl_arch=arm64; helm_sha256="${HELM_ARM64_SHA256}" ;; *) echo "unsupported architecture: $arch" >&2; exit 1 ;; esac \
    && curl --fail --silent --show-error --location "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${kubectl_arch}/kubectl" --output /usr/local/bin/kubectl \
    && chmod 0755 /usr/local/bin/kubectl \
    && curl --fail --silent --show-error --location "https://get.helm.sh/helm-${HELM_VERSION}-linux-${kubectl_arch}.tar.gz" --output /tmp/helm.tar.gz \
    && echo "${helm_sha256}  /tmp/helm.tar.gz" | sha256sum --check --strict \
    && tar --extract --gzip --file /tmp/helm.tar.gz --strip-components=1 --directory /usr/local/bin "linux-${kubectl_arch}/helm" \
    && chmod 0755 /usr/local/bin/helm \
    && rm /tmp/helm.tar.gz

WORKDIR /workspace
RUN git config --system --add safe.directory /workspace
COPY pyproject.toml VERSION README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

ENTRYPOINT ["ci-templates"]
