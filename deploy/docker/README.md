# deploy/docker

One parameterized Dockerfile builds every carto service image (spec 6, 14.6):

```
docker build -f deploy/docker/base.Dockerfile --build-arg MEMBER=edge --build-arg ENTRY=carto-edge -t carto-edge:dev .
docker build -f deploy/docker/base.Dockerfile --build-arg MEMBER=core --build-arg ENTRY=carto-core -t carto-core:dev .
```

Images run as uid 10001 with a read-only root filesystem (writable volumes at `/var/lib/carto`
and, for the edge, the state directory), all capabilities dropped and `no-new-privileges` (see
`deploy/compose/compose.yaml`). The runtime stage keeps `/bin/sh` for the entrypoint shim; M6
replaces the base with a distroless image and adds cosign signatures and SLSA provenance to
`release.yml` (spec 14.8).

## Pins and the build context (spec 14.8, ADR 0072)

The Python base image and the `uv` binary image are pinned by digest, as are the Node and nginx
images of `web/deploy/Dockerfile`; Renovate (`renovate.json`, `docker:pinDigests`) moves the
digests with the patch releases and never crosses a major line on its own. The root
`.dockerignore` keeps `.git`, virtual environments, caches, every `tests/` directory, simulator
output, eval reports, `legacy/`, the console's `node_modules` and build output, the Compose
secrets and TLS files, bundles, databases and `.env*` files out of the build context, so none
of them can land in a layer; `web/.dockerignore` does the same for the console image. CI scans
every image it builds with Trivy (edge and core in the `images` job; ctl, simulator and web in
the `compose` job) and fails on high and critical findings with a fix available.
