# The merged Client (upload-ingest-merge Phase 6): Vite dev server for the POC walkthrough.
# Node pinned to the patch release (Vite 8 needs ^20.19 or ^22.12); dependencies from the lockfile only.
FROM node:22.23.3-alpine
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
EXPOSE 5173
# 0.0.0.0 inside the container; compose publishes it on 127.0.0.1 only.
CMD ["npx", "vite", "--host", "0.0.0.0", "--port", "5173", "--strictPort"]
