FROM python:3.12-slim-bookworm

# Build once on the sandbox host; record the resulting image digest for a run.
RUN pip install --no-cache-dir playwright==1.55.0 \
    && python -m playwright install --with-deps chromium \
    && useradd --create-home --uid 10001 sandbox \
    && mkdir -p /opt/playwright \
    && cp -a /root/.cache/ms-playwright/. /opt/playwright/ \
    && chmod -R a+rX /opt/playwright
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
WORKDIR /home/sandbox
USER sandbox
CMD ["tail", "-f", "/dev/null"]
