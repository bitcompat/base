# General image comparison

`compare.py` uses Python 3's standard library and the Docker CLI. Both images
must already be available locally, on the host's native architecture. Pin the
Bitnami reference by registry digest; the candidate is resolved to its immutable
image ID before use. No images are published and no host ports are exposed.

```sh
python3 tests/compatibility/compare.py \
  --reference bitnamilegacy/redis@sha256:a7751bda1b64348a2d6322eee1f124fafd27ac0d404d051d27d24f781e64ace9 \
  --candidate public.ecr.aws/bitcompat/redis:8.0.3 \
  --platform linux/amd64 \
  --env-file /tmp/redis-test.env \
  --ready-command 'test "$(cat /proc/1/comm)" = redis-server' \
  --output /tmp/redis-comparison
```

Use identical startup fixtures on both sides. The environment file is passed to
Docker but is not copied into reports; do not put secrets into command arguments.
Choose a new output directory for every run. The runner will not overwrite an
existing report directory. Containers and anonymous volumes created by the run
are removed in `finally`; existing containers and volumes are untouched.

Outputs: `reference.json`, `candidate.json`, and `comparison.json`.

- Filesystem: paths, file types, modes, UID/GID and symlink/hardlink targets from
  `docker export` before startup. Timestamps and binary hashes are not compared.
  Export excludes mounted volume contents, including image-declared volumes;
  volume declarations are compared in image config. This is a tree comparison,
  not a comparison of file contents or the initialized data directory.
- Environment: all default OCI image variables and values, without silently
  ignoring version, distribution or architecture metadata. Runtime fixture
  overrides are deliberately not included. Review image-baked defaults before
  sharing reports if an image contains embedded secrets.
- Image config: user, entrypoint, command, working directory, volumes, exposed
  ports and stop signal.
- Lifecycle: default entrypoint/CMD, process survival for the startup timeout,
  or an explicit identical readiness probe. A live process is not reported as
  service readiness. Stop sends SIGTERM, waits, and rejects SIGKILL, OOM or an
  exit code other than 0/143. Short-lived utility images can exit normally but
  fail this service lifecycle check; their static differences remain available.
- Configure: optional `--configure-command`, executed in each running container.
  For PHP-FPM use `--configure-command 'php -i | sed -n "/^Configure Command/p"'`.
  An absent command or empty output does not establish matching compile flags.
  A failing supplied command fails the run. Reports retain the raw output.
- Size: Docker image inspect `Size` in bytes, delta and percentage. This is the
  local uncompressed image size, not compressed registry download size.

Exit 1 means execution/lifecycle failed. Exit 0 means collection and lifecycle
checks succeeded, with status `review-required`: differences still need review.
Running the same immutable image twice records `mode: self-check`; this does
not establish Bitcompat/Bitnami parity. Neither size nor filesystem differences
are automatically accepted or rejected. Review expected Trixie/provenance
changes separately from unexpected contract changes. Use the same software
version for comparisons intended to assess compatibility.

The database runner `run.py` is a subsequent specialization. Its prepared CI
gates are dormant unless `BITCOMPAT_DATABASE_COMPATIBILITY=true` is explicitly
set. The general comparator is currently a manual tool, without a publication
gate or automatic difference whitelist.

Small regression check: `python3 tests/compatibility/test_compare.py`.
