FROM node:24.14.0-bookworm-slim
WORKDIR /app
COPY runtime/pi-agent/package.json runtime/pi-agent/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts && npm cache clean --force
COPY runtime/pi-agent/*.mjs ./
RUN mkdir /state && chown node:node /state
USER node
ENV AGENT_IMAGE_STATE=/state
CMD ["node", "worker.mjs"]
