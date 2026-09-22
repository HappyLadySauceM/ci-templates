# Reuse the last published control image's verified OS/tooling layer for routine
# CLI releases. Bump this digest deliberately when the base toolchain changes.
ARG CONTROL_BASE_IMAGE=harbor.happyladysauce.local/knowledge-core/ci-templates:v1.1.12@sha256:7704f53047a23d036ea0edc93563e2af65c3b8c59f5cbbf2e697c2c2e3ce9227
FROM ${CONTROL_BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /workspace
RUN git config --system --add safe.directory /workspace
COPY pyproject.toml VERSION README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

ENTRYPOINT ["ci-templates"]
