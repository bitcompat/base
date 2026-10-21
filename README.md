# Shared image build workflow

`build.yaml` passes `BUILD_VERSION` and `IMAGE_REVISION` to image builds.

- Published images: `IMAGE_REVISION=N`, using the revision output from
  `compute-ghcr-revision-action` that also generates the `-rN` tag.
- Native verification builds and `push: false`: `IMAGE_REVISION=0`.
- Dockerfiles that expose this metadata declare `ARG IMAGE_REVISION=0` and
  copy it to `ENV IMAGE_REVISION`, so direct local builds also default to zero.

The workflow supplies the authoritative argument after caller-provided build
arguments. The deprecated `revision` workflow input does not control this value.
This does not change how revisions are allocated or coordinate concurrent runs.

Regression check: `python3 tests/test_image_revision.py`.
