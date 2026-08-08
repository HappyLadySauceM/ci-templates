ARG PYTHON_BASE_IMAGE=python:3.12-slim
ARG KUBECTL_VERSION=v1.36.2
FROM ${PYTHON_BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git jq openssh-client docker.io docker-buildx skopeo \
    && rm -rf /var/lib/apt/lists/* \
    && arch="$(dpkg --print-architecture)" \
    && case "$arch" in amd64) kubectl_arch=amd64 ;; arm64) kubectl_arch=arm64 ;; *) echo "unsupported architecture: $arch" >&2; exit 1 ;; esac \
    && curl --fail --silent --show-error --location "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${kubectl_arch}/kubectl" --output /usr/local/bin/kubectl \
    && chmod 0755 /usr/local/bin/kubectl

WORKDIR /workspace
COPY pyproject.toml VERSION README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

ENTRYPOINT ["ci-templates"]
