FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df AS builder
COPY --from=ghcr.io/astral-sh/uv:0.9.8@sha256:08f409e1d53e77dfb5b65c788491f8ca70fe1d2d459f41c89afa2fcbef998abe /uv /uvx /bin/
WORKDIR /workspace
COPY . /workspace
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy
RUN uv sync --locked --no-dev --no-editable --package mozaic-scrapy-collector

FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df AS runtime
ARG CATALOG_ASSEMBLY_ID
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    SCRAPY_COLLECTOR_CATALOG_ROOT=/opt/scrapy-collector \
    SCRAPY_COLLECTOR_ASSEMBLY_ID=${CATALOG_ASSEMBLY_ID} \
    SCRAPY_COLLECTOR_OUTPUT_ROOT=/out
LABEL org.data-mozaic.scrapy-collector.assembly-id=${CATALOG_ASSEMBLY_ID}
WORKDIR /opt/scrapy-collector
COPY --from=builder /opt/venv /opt/venv
COPY --from=botcatalog / /opt/scrapy-collector/
RUN mkdir -p /out && chown 65532:65532 /out
USER 65532:65532
ENTRYPOINT ["python", "-B", "-m", "scrapy_collector"]
