# syntax=docker/dockerfile:1
# Single final runtime. No credentials/build secrets, models, GPU or remote DB.
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
 PLAYWRIGHT_BROWSERS_PATH=/opt/chromium \
 PATH=/app/.venv/bin:${PATH} \
 HOME=/home/crawler \
 KAGI_CRAWLER_HOST=0.0.0.0 \
 KAGI_CRAWLER_CACHE_DIRECTORY=/data/cache

RUN rm -rf /root/.cache /var/lib/apt/lists/* \
 && groupadd --gid 10001 crawler \
 && useradd --uid 10001 --gid crawler --create-home crawler \
 && mkdir -p /data/cache /etc/opt/chrome_for_testing/policies/managed \
 && chown -R crawler:crawler /data /home/crawler

# Use Patchright's own exact browser manifest, not distro Chromium/Playwright's
# browser.
# --with-deps installs CPU system libraries at build time, never at runtime.
RUN uv tool run --from patchright==1.63.0 patchright install --with-deps chromium \
 && echo '{"DnsOverHttpsMode":"secure","DnsOverHttpsTemplates":"https://1.1.1.1/dns-query"}' > /etc/opt/chrome_for_testing/policies/managed/native-doh.json \
 && chmod -R a+rX /opt/chromium

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --python 3.12 --no-install-project \
 && .venv/bin/python -c 'import json,pathlib,patchright; p=pathlib.Path(patchright.__file__).parent/"driver/package/browsers.json"; c=next(b for b in json.loads(p.read_text())["browsers"] if b["name"]=="chromium"); assert (c["revision"],c["browserVersion"])==("1243","153.0.8010.12")' \
 && .venv/bin/python -c 'import importlib.metadata as m; assert not {"torch","transformers","sentence-transformers","onnxruntime"} & {d.metadata["Name"].lower() for d in m.distributions()}'

COPY --from=kagi /cli/bin/kagi /usr/local/bin/kagi

COPY src ./src
RUN uv sync --locked --no-dev --python 3.12

USER 10001:10001
EXPOSE 8000
VOLUME ["/data/cache"]
STOPSIGNAL SIGTERM
ENTRYPOINT ["kagi-crawler"]
