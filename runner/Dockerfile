# Runner image used by both ARC Scale Sets. The builder Scale Set adds the
# Docker-in-Docker sidecar; the standard Scale Set runs this image without a
# host Docker socket.
ARG RUST_IMAGE=rust:1.97.1-slim-bookworm@sha256:2775a09d208ff0d7c1f50490c45b62db929e87ba1dcbc3f2132ac71a704bcdd3
FROM ${RUST_IMAGE} AS rust-toolchain
RUN rustup component add clippy rustfmt

FROM ubuntu:24.04@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517

ARG RUNNER_VERSION=2.333.0
ARG RUNNER_SHA256=7ce6b3fd8f879797fcc252c2918a23e14a233413dc6e6ab8e0ba8768b5d54475
ARG KUBECTL_VERSION=v1.36.2
ARG HELM_VERSION=v3.21.3
ARG HELM_AMD64_SHA256=15e041a93a590dce8100f39385cd98c84a765c9e36aeeb9e2dc6ff9e4769e2e0
# The minimal Ubuntu image does not ship a CA bundle. Bootstrap APT through
# the signed Ubuntu InRelease over HTTP, then install ca-certificates before
# any HTTPS downloads. Operators may override this with an internal HTTPS
# mirror when the base image already trusts its CA.
ARG UBUNTU_MIRROR=http://archive.ubuntu.com/ubuntu

ENV DEBIAN_FRONTEND=noninteractive \
    RUNNER_ALLOW_RUNASROOT=0 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Ubuntu's docker.io package may already reserve the `docker` group with a
# distribution-specific GID. The builder sidecar deliberately exposes its
# socket as GID 123, so create or reuse whichever group owns that GID instead
# of assuming that the name is available.
RUN sed -i "s#http://archive.ubuntu.com/ubuntu#${UBUNTU_MIRROR}#g; s#http://security.ubuntu.com/ubuntu#${UBUNTU_MIRROR}#g" /etc/apt/sources.list.d/ubuntu.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential ca-certificates curl git jq make openssh-client \
       pkg-config python3 python3-pip python3-setuptools skopeo xz-utils \
       docker.io docker-buildx libicu74 libssl3 libkrb5-3 zlib1g \
       libasound2t64 libatk-bridge2.0-0t64 libatk1.0-0t64 libatspi2.0-0t64 \
       libcairo2 libcups2t64 libdbus-1-3 libdrm2 libgbm1 libglib2.0-0t64 \
       libgtk-3-0t64 libnspr4 libnss3 libpango-1.0-0 libwayland-client0 \
       libx11-6 libx11-xcb1 libxcb1 libxcomposite1 libxdamage1 libxext6 \
       libxfixes3 libxkbcommon0 libxrandr2 libxshmfence1 xvfb \
       fonts-liberation fonts-noto-color-emoji fonts-unifont \
    && rm -rf /var/lib/apt/lists/* \
    && socket_group="$(getent group 123 | cut -d: -f1 || true)" \
    && if [ -z "$socket_group" ]; then groupadd --gid 123 docker-socket; socket_group=docker-socket; fi \
    && groupadd --gid 1001 runner \
    && useradd --uid 1001 --gid 1001 --groups "$socket_group" --create-home --shell /bin/bash runner \
    && arch="$(dpkg --print-architecture)" \
    && test "$arch" = amd64 \
    && curl --fail --silent --show-error --location \
       "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz" \
       --output /tmp/runner.tar.gz \
    && echo "${RUNNER_SHA256}  /tmp/runner.tar.gz" | sha256sum --check --strict \
    && mkdir -p /home/runner \
    && tar --extract --gzip --file /tmp/runner.tar.gz --directory /home/runner \
    && rm -f /tmp/runner.tar.gz \
    && curl --fail --silent --show-error --location \
       "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
       --output /usr/local/bin/kubectl \
    && chmod 0755 /usr/local/bin/kubectl \
    && curl --fail --silent --show-error --location \
       "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz" \
       --output /tmp/helm.tar.gz \
    && echo "${HELM_AMD64_SHA256}  /tmp/helm.tar.gz" | sha256sum --check --strict \
    && tar --extract --gzip --file /tmp/helm.tar.gz --strip-components=1 --directory /usr/local/bin linux-amd64/helm \
    && chmod 0755 /usr/local/bin/helm \
    && rm -f /tmp/helm.tar.gz \
    && chown -R runner:runner /home/runner

COPY --from=rust-toolchain /usr/local/cargo /usr/local/cargo
COPY --from=rust-toolchain /usr/local/rustup /usr/local/rustup
ENV RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/home/runner/.cargo \
    PATH=/usr/local/cargo/bin:/usr/local/rustup/toolchains/1.97.1-x86_64-unknown-linux-gnu/bin:$PATH
RUN mkdir -p /home/runner/.cargo \
    && chown -R runner:runner /home/runner/.cargo

WORKDIR /home/runner
COPY pyproject.toml VERSION README.md /tmp/ci-templates/
COPY src /tmp/ci-templates/src
RUN python3 -m pip install --break-system-packages --no-cache-dir /tmp/ci-templates \
    && rm -rf /tmp/ci-templates

USER runner
ENTRYPOINT ["/home/runner/run.sh"]
