# Runner image used by both ARC Scale Sets.  Routine control-plane changes
# inherit the immutable Harbor runner instead of downloading Rust, Ubuntu,
# Actions Runner, kubectl and Helm from the public Internet again.  A runner
# version upgrade deliberately creates a separately reviewed bootstrap image;
# this Dockerfile only produces safe, fast roll-forward revisions of it.
ARG RUNNER_BASE_IMAGE=harbor.happyladysauce.local/infrastructure/hls-actions-runner:2.337.0-r5
FROM ${RUNNER_BASE_IMAGE}

USER root

WORKDIR /home/runner
COPY pyproject.toml VERSION README.md /tmp/ci-templates/
COPY src /tmp/ci-templates/src
RUN python3 -m pip install --break-system-packages --no-cache-dir /tmp/ci-templates \
    && rm -rf /tmp/ci-templates

USER runner
ENTRYPOINT ["/home/runner/run.sh"]
