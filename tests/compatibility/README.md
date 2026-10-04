# Specialized database checks (deferred)

Start with [general image comparison](GENERAL.md); this runner is the next step.

The runner uses Python 3 (standard library) and the Docker CLI. It runs on the
native AMD64 or ARM64 architecture and uses isolated containers and volumes;
it does not publish ports or modify existing containers. Temporary resources
are removed in `finally`, including on failure. Passwords are generated for the
test and removed from failure logs.

```sh
python3 tests/compatibility/run.py \
  --service postgresql \
  --candidate local/postgresql-under-test:17.6 \
  --version 17.6 \
  --platform linux/amd64 \
  --report compatibility-report.json
```

The same command supports `mysql` and `redis`. Image versions are checked against
the running server, not just labels. Candidate image IDs and registry digests
are included in the report; after inspection the test starts that exact image ID.

## Contract covered

- Default UID 1001, Bitnami entrypoint/command paths and exposed port.
- Rejection of missing credentials; rejection of an incorrect TCP password.
- Successful startup with custom user/database where applicable.
- Password variables and their `*_FILE` forms.
- Configurable service port through the documented environment variable.
- Effective write permission on `/bitnami/<service>/data` as UID 1001.
- SQL initialization scripts for PostgreSQL/MySQL run once; they are not rerun
  on restart or replacement with an existing volume.
- Write/read of a persistent fixture, same-container restart, and a replacement
  container with the populated volume.
- SIGTERM shutdown without Docker falling back to SIGKILL or an OOM exit.

The readiness check waits for the server to become PID 1, so a temporary server
during initialization cannot make the test proceed before init scripts finish.
Ownership and permissions are reported without requiring directories to be owned
by UID 1001: Bitnami also uses group-writable directories owned by root.

## Differential mode and references

`references.json` contains explicitly selected Bitnami Legacy manifest digests,
with their original tags and AMD64/ARM64 child digests. These are historical
test fixtures. The runner never selects a moving `latest` reference.

For a locked exact version, both images run the same contract checks; the candidate
also receives a volume initialized and written by Bitnami. No reverse handoff is
performed: this would potentially test an unsupported database downgrade.

When no exact reference is locked, the runner emits a CI warning and records
`mode: contract-only`. Passing means the candidate passed the contract checks;
it does not mean differential equivalence was demonstrated. Use
`--require-reference` to reject that mode, or supply an exact-version immutable
reference via `--reference repository@sha256:<digest>`.

Current exact references: PostgreSQL 17.6; Redis 8.0.3 and 8.2.1; MySQL 8.4.5.
The active MySQL matrix has newer/different versions, so those CI jobs currently
run contract checks without claiming an exact Bitnami comparison. Other missing
PostgreSQL references are also reported as contract-only.

## CI integration

This local integration is dormant: it runs only when the repository variable
`BITCOMPAT_DATABASE_COMPATIBILITY` is explicitly set to `true`. General image
comparison in `compare.py` is the current first step.

The common build workflow accepts `compatibility-service`. MySQL and Redis set
it for every build job. Each native build loads its image and runs these checks;
the final publication job depends on both native jobs succeeding. PostgreSQL's
dedicated workflow runs the same suite after each native build. JSON reports are
uploaded even when tests fail.

The tests are checked out from `bitcompat/base@main`; publish the base changes
before updating consumer workflows. Other images keep the default empty input
and do not run this database-specific suite.

## Limits

These checks cover a defined subset of the Bitnami runtime contract, not every
file, configuration or feature. Replication, TLS, extension inventories, custom
UIDs, arbitrary mounted configurations, upgrades across database majors,
collation changes across Debian releases and recovery after an abrupt crash are
not covered. Upstream unit tests are independent of this suite.

See the workspace document `docs/runtime-compatibility-2026-10-04.md` for actual
runs and validation limits. Adding a digest to the reference lock does not prove
that its runtime or that architecture has been exercised.
