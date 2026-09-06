# syntax=docker/dockerfile:1
ARG NODE_VERSION=22
ARG NGINX_VERSION=stable-alpine

FROM node:${NODE_VERSION}-alpine AS build
WORKDIR /app

COPY package.json package-lock.json .npmrc ./
RUN --mount=type=cache,target=/app/.cache/npm npm ci --ignore-scripts --workspaces=false

COPY index.html tsconfig.json vite.config.ts ./
COPY src/ ./src/
RUN npm run build

FROM nginxinc/nginx-unprivileged:${NGINX_VERSION} AS runtime
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist/ /usr/share/nginx/html/

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1

CMD ["nginx", "-g", "daemon off;"]
