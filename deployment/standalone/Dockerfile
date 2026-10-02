# syntax=docker/dockerfile:1
# This image is explicitly opt-in; the Python wheel remains engine-independent.
FROM --platform=$BUILDPLATFORM node:22-bookworm-slim AS engine
ARG TARGETARCH
WORKDIR /build
RUN case "$TARGETARCH" in \
        amd64) package=cli-linux-x64-baseline ;; \
        arm64) package=cli-linux-arm64 ;; \
        *) exit 1 ;; \
    esac \
    && npm pack --ignore-scripts "@opencode/$package@2.0.19" \
    && tar -xzf *.tgz \
    && mv package/bin/opencode /opencode

FROM python:3.12-slim-bookworm AS wheel
RUN python -m pip install --no-cache-dir uv==0.9.5
WORKDIR /build
COPY pyproject.toml uv.lock README.md ./
COPY preprocessing/ preprocessing/
RUN uv build --wheel \
    && uv export --quiet --frozen --no-dev --no-editable --no-emit-project \
        --format requirements-txt --output-file requirements.txt

FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY --from=wheel /build/requirements.txt /tmp/requirements.txt
COPY --from=wheel /build/dist/ /tmp/wheels/
RUN python -m pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt \
    && python -m pip install --no-cache-dir --no-deps /tmp/wheels/*.whl \
    && rm -rf /tmp/wheels /tmp/requirements.txt \
    && mkdir -p /var/lib/preprocessing/data /var/lib/preprocessing/engine /run/preprocessing /workspace \
    && chown -R 10001:10001 /var/lib/preprocessing /run/preprocessing \
    && chmod 0700 /var/lib/preprocessing/data /var/lib/preprocessing/engine /run/preprocessing
COPY --from=engine --chmod=0755 /opencode /opt/opencode-2.0.19/bin/opencode
COPY deployment/standalone/supervise.py /opt/preprocessing/supervise.py
USER 10001:10001
WORKDIR /opt/preprocessing
EXPOSE 8765
HEALTHCHECK --interval=15s --timeout=10s --start-period=300s --retries=3 \
    CMD ["python", "/opt/preprocessing/supervise.py", "health"]
ENTRYPOINT ["python", "/opt/preprocessing/supervise.py"]
CMD ["--help"]
