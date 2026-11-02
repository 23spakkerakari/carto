# web service image (ADR 0052, draft for the orchestrator). Two stages: the build runs
# `npm ci` from the committed lockfile and `npm run build` (which also runs the build check:
# no mock API, strict CSP meta tag); the runtime is the same unprivileged nginx the push-route
# proxies use, serving the static files as uid 10001 on 8080 with deploy/nginx.conf.
#
# Build from the repository root:  docker build -f web/deploy/Dockerfile -t carto-web:dev web
#
# Both base images are pinned by digest (spec 14.8, ADR 0072); Renovate moves the digests with
# the patch releases. Node 22 is the active LTS line (`engines` in package.json allows it);
# web/.dockerignore keeps node_modules, build output, test output and env files out of the
# context.

FROM node:22.23.4-alpine3.24@sha256:fe4f6d74e20370acb04ad889077d14d0f64a9c8ea6a42bc60cddf1e844699914 AS build
WORKDIR /src
ENV CI=1 \
    NODE_ENV=development
COPY package.json package-lock.json .npmrc ./
RUN npm ci --no-audit --no-fund --ignore-scripts
COPY . .
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.30.4-alpine@sha256:adf5042a17f4ecdd200c595fa9ffd1be37efb18f89a830bd1a00e4ab4d59d42c
COPY --chown=10001:10001 deploy/nginx.conf /etc/nginx/nginx.conf
COPY --chown=10001:10001 --from=build /src/dist /usr/share/nginx/html
USER 10001:10001
EXPOSE 8080 8443
HEALTHCHECK --interval=30s --timeout=3s CMD wget -qO- http://127.0.0.1:8080/healthz >/dev/null || exit 1
ENTRYPOINT ["nginx", "-g", "daemon off;"]
