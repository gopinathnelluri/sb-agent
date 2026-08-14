# syntax=docker/dockerfile:1
FROM registry.access.redhat.com/ubi9/python-311

# OpenShift assigns a random UID at runtime, in the root group. Nothing may
# assume a writable $HOME or a fixed user, so HOME points at /tmp and every
# path the process writes to must be a tmpfs or a mounted volume.
ENV HOME=/tmp \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/app-root/venv \
    UV_COMPILE_BYTECODE=1 \
    PATH="/opt/app-root/venv/bin:$PATH"

WORKDIR /opt/app-root/src

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Dependencies first so a code change does not re-resolve the whole lock file.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY core/ ./core/
COPY adapters/ ./adapters/
COPY mcp_server/ ./mcp_server/
RUN uv sync --frozen --no-dev

# Fail the build, not the pod, if the rule catalog is malformed: a container
# that starts with fewer rules loaded reports a healthy cluster for the wrong
# reason.
RUN python -c "from core.rules.schema import RuleCatalog; \
    c = RuleCatalog.load(); \
    print(f'catalog ok: {len(c.rules)} rules'); \
    assert c.rules"

# Group-writable so the arbitrary runtime UID (member of root group) can read.
RUN chgrp -R 0 /opt/app-root && chmod -R g=u /opt/app-root

USER 1001

ENTRYPOINT ["python", "-m", "mcp_server"]
CMD ["--backend", "cos", "--transport", "streamable-http"]
