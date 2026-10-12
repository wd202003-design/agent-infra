FROM python:3.12-slim
WORKDIR /app
COPY control-plane/requirements.txt /tmp/control-requirements.txt
COPY runner/requirements.txt /tmp/runner-requirements.txt
RUN pip install --no-cache-dir -r /tmp/control-requirements.txt -r /tmp/runner-requirements.txt
COPY contracts /app/contracts
COPY control-plane /app/control-plane
COPY runner /app/runner
COPY web /app/web
ENV PYTHONPATH=/app/control-plane:/app PYTHONUNBUFFERED=1
RUN useradd --uid 10001 --create-home agent && mkdir /data && chown agent:agent /data
USER agent
CMD ["python", "-m", "infra_control", "serve", "--host", "0.0.0.0"]
