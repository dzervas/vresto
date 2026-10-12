# syntax=docker/dockerfile:1
# Single final runtime. No credentials/build secrets, models, GPU or remote DB.
FROM rust:1.90.0-bookworm AS kagi
RUN git clone https://github.com/Microck/kagi-cli /source \
 && cd /source \
 && git checkout cb64cf514a99e527429468f92bc9f6b633a765e4 \
 && cargo install --locked --path . --root /cli \
 && mkdir -p /cli/licenses \
 && cp /source/LICENSE /cli/licenses/Kagi-LICENSE \
 && find /source -maxdepth 1 -type f -iname '*notice*' -exec cp '{}' /cli/licenses/ \; \
 && cd /usr/local/cargo/registry/src \
 && find . -type f \( -iname '*license*' -o -iname '*notice*' -o -iname '*copying*' \) -exec cp --parents '{}' /cli/licenses/ \; \
 && find /usr/local/rustup/toolchains -type f \( -name 'LICENSE-*' -o -name 'COPYRIGHT*' \) -exec cp --parents '{}' /cli/licenses/ \;

FROM ghcr.io/astral-sh/uv:0.12.17 AS uv
FROM python:3.12.13-slim-bookworm
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never \
    PLAYWRIGHT_BROWSERS_PATH=/opt/chromium \
    PATH=/app/.venv/bin:/usr/local/bin:/usr/bin:/bin \
    HOME=/home/crawler \
    KAGI_CRAWLER_HOST=0.0.0.0 \
    KAGI_CRAWLER_CACHE_DIRECTORY=/data/cache
COPY pyproject.toml uv.lock NOTICE ./
COPY licenses ./licenses
COPY src ./src
# Use Patchright's own exact browser manifest, not distro Chromium/Playwright's
# browser. Keep installed package/driver/browser LICENSE/ThirdPartyNotices files.
# --with-deps installs CPU system libraries at build time, never at runtime.
RUN uv sync --locked --no-dev --python 3.12 \
 && .venv/bin/python -m patchright install --with-deps chromium \
 && .venv/bin/python -c 'import json,pathlib,patchright; p=pathlib.Path(patchright.__file__).parent/"driver/package/browsers.json"; c=next(b for b in json.loads(p.read_text())["browsers"] if b["name"]=="chromium"); assert (c["revision"],c["browserVersion"])==("1243","153.0.8010.12")' \
 && .venv/bin/python -c 'import importlib.metadata as m; assert not {"torch","transformers","sentence-transformers","onnxruntime"} & {d.metadata["Name"].lower() for d in m.distributions()}' \
 && rm -rf /root/.cache /var/lib/apt/lists/* \
 && groupadd --gid 10001 crawler \
 && useradd --uid 10001 --gid crawler --create-home crawler \
 && mkdir -p /data/cache /usr/share/licenses/kagi-crawler \
 && cp NOTICE /usr/share/licenses/kagi-crawler/NOTICE \
 && cp -r licenses /usr/share/licenses/kagi-crawler/ \
 && chown -R crawler:crawler /data /home/crawler \
 && chmod -R a+rX /opt/chromium
COPY --from=kagi /cli/bin/kagi /usr/local/bin/kagi
COPY --from=kagi /cli/licenses /usr/share/licenses/kagi-cli
USER 10001:10001
EXPOSE 8000
VOLUME ["/data/cache"]
STOPSIGNAL SIGTERM
ENTRYPOINT ["kagi-crawler"]
