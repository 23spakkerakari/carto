# web service image (ADR 0052, draft for the orchestrator). Two stages: the build runs
# `npm ci` from the committed lockfile and `npm run build` (which also runs the build check:
# no mock API, strict CSP meta tag); the runtime is the same unprivileged nginx the push-route
# proxies use, serving the static files as uid 10001 on 8080 with deploy/nginx.conf.
#
# Build from the repository root:  docker build -f web/deploy/Dockerfile -t carto-web:dev web

FROM node:20.15.1-alpine3.20 AS build
WORKDIR /src
ENV CI=1 \
    NODE_ENV=development
COPY package.json package-lock.json .npmrc ./
RUN npm ci --no-audit --no-fund --ignore-scripts
COPY . .
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.30.4-alpine
COPY --chown=10001:10001 deploy/nginx.conf /etc/nginx/nginx.conf
COPY --chown=10001:10001 --from=build /src/dist /usr/share/nginx/html
USER 10001:10001
EXPOSE 8080 8443
HEALTHCHECK --interval=30s --timeout=3s CMD wget -qO- http://127.0.0.1:8080/healthz >/dev/null || exit 1
ENTRYPOINT ["nginx", "-g", "daemon off;"]
