# syntax=docker/dockerfile:1
# Single final runtime. Public pinned CPU model; no credentials/build secrets or remote DB.
FROM rust:1.90.0-bookworm AS kagi
RUN git clone https://github.com/Microck/kagi-cli /source \
 && cd /source \
 && git checkout cb64cf514a99e527429468f92bc9f6b633a765e4 \
 && cargo install --locked --path . --root /cli

FROM ghcr.io/astral-sh/uv:0.12.17 AS uv

FROM python:3.12.13-slim-bookworm
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app

ENV UV_PYTHON_DOWNLOADS=never \
 KAGI_CRAWLER_HEADFUL=1 \
 KAGI_CRAWLER_BROWSER_CHANNEL=chrome \
 PATH=/app/.venv/bin:${PATH} \
 HOME=/home/crawler \
 KAGI_CRAWLER_HOST=0.0.0.0 \
 KAGI_CRAWLER_CACHE_DIRECTORY=/data/cache

RUN rm -rf /root/.cache /var/lib/apt/lists/* \
 && groupadd --gid 10001 crawler \
 && useradd --uid 10001 --gid crawler --create-home crawler \
 && mkdir -p /data/cache /etc/opt/chrome/policies/managed /tmp/.X11-unix \
 && chmod 1777 /tmp/.X11-unix \
 && chown -R crawler:crawler /data /home/crawler

# Stable Google Chrome is a moving build target; record its version/image digest.
# --with-deps supplies CPU libraries and Xvfb at build time, never at runtime.
RUN uv tool run --from patchright==1.63.0 patchright install --with-deps chrome \
 && command -v Xvfb >/dev/null \
 && echo '{"DnsOverHttpsMode":"secure","DnsOverHttpsTemplates":"https://1.1.1.1/dns-query"}' > /etc/opt/chrome/policies/managed/native-doh.json

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --python 3.12 --no-install-project \
 && .venv/bin/python -c 'import importlib.metadata as m; assert not {"torch","transformers","sentence-transformers"} & {d.metadata["Name"].lower() for d in m.distributions()}'

COPY --from=kagi /cli/bin/kagi /usr/local/bin/kagi

COPY scripts/install_embedding_model.py /app/scripts/install_embedding_model.py
# Anonymous, revision-pinned FP32 graph/data/tokenizer; no runtime downloads.
# INT8 is a runtime option after provisioning its pinned pair with --int8.
RUN python scripts/install_embedding_model.py /opt/embeddinggemma2

COPY src ./src
RUN uv sync --locked --no-dev --python 3.12

USER 10001:10001
EXPOSE 8000
VOLUME ["/data/cache"]
STOPSIGNAL SIGTERM
ENTRYPOINT ["python", "-m", "kagi_crawler.headful"]
