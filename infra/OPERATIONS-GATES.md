# Local operational fixes and opt-in capacity artifacts

These files authorize no national generation. The capacity overlay now maps
PostgreSQL to the verified project volume and must only be applied after a clean,
verified cluster copy. It does not format storage or enable the scheduler. Keep
the existing deployment changes when packaging the reviewed release.

## Restoration acceptance target, confirmed November 4

Restore the result the client already accepted. The accepted V10 geometry,
subsequent accepted calculation rules and interface behavior are the reference,
with only the later explicit restoration changes added. Do not substitute a new
definition of forest, calculation methodology or redesigned product.

Before country work, repeat the complete workflow on a bounded area: generate,
load into the isolated preview, exercise the actual interface, capture screenshots,
inspect them and compare the same locations with accepted historical evidence.
Fix restoration regressions and repeat this cycle until the checks pass. A
geometry overlay alone is not a completed downstream/API/UI proof.

Record the accepted references, source/code identities, bounds, coordinates,
zoom, differences, fixes and remaining limitations for each iteration in the
private restoration evidence directory. Keep original evidence. Distinguish an
existing accepted limitation from a new restoration regression; if historical
evidence is missing, record that uncertainty rather than claiming exact parity.
Internal working requirements belong here or in private restoration notes, not
among verbatim client messages. This instruction does not authorize a national
run or public cutover before the bounded review is complete.

## Bounded replay checks completed on November 4

Use the complete raw source scope when checking cache geometry. The optional
`--check-source-cache-geometry` gate compares each parcel and source-layer union;
the default limits are 1,000 raw parcels and 10,000 cache pieces. It refuses larger
scopes rather than sampling. This is a bounded acceptance check, not a command
to apply those limits to the national source. Retain negative controls for changed
source geometry with the same ID, a missing cache subdivision and invalid pieces.

The current and preserved accepted implementations independently reproduced the
same 3,110 cells from the same 490 normalized source features. Their native masks,
stored masks and grid geometries also matched. This does not recover the lost
national database or establish identical historical OGM acquisition inputs.

Do not compare native EPSG:6933 mask geometry and geometry transformed back from
WGS84 as if they were the same numerical representation. In both implementations,
the measured maximum per-fragment discrete boundary Hausdorff distance was
0.000585238 metres, and the round-trip area differences were identical. Record
native and stored-representation measurements separately; do not change the
accepted geometry or loosen tolerances to conceal a failed comparison.

Celery dependency waits must survive more than three retries. Setting
`retry(max_retries=None)` alone retains Celery's task default; the task itself must
declare `max_retries=None`. The bounded cold-source run exercised this failure and
recovered only the waiting dependent batch, preserving the completed NDVI batch.
Operational deadlines and resource limits remain necessary for any long run.

Verify static releases as the actual unprivileged frontend user. Confirm directory
traversal, readable entry point and assets, and an authenticated HTML response
containing the application entry point. A running container, working API or HTTP
200 alone does not prove that the dashboard loaded. Preserve bundle checksums
when correcting directory permissions.

## Restore checks now require an isolated target

`scripts/check-backup-restore.sh --help` documents the required settings. The
default production target has been removed deliberately. Primary backup creation
and its scheduled jobs keep their current defaults.

Use a dedicated Compose project/service with **its own** PGDATA and the label
`io.map1.restore-isolated: "true"`. Do not put this label on the live database.
An optional `RESTORE_COMPOSE_OVERRIDE_FILE` is appended as a second `-f` file;
it must define the isolated target, not redirect the existing live service to
production storage. Inspect that service and record its full container ID first.

```bash
COMPOSE_FILE=/secure/recovery/compose.yml \
RESTORE_COMPOSE_OVERRIDE_FILE=/secure/recovery/isolated.override.yml \
ENV_FILE=/secure/recovery/runtime.env \
RESTORE_COMPOSE_PROJECT=map1-restore \
DB_SERVICE=restore-db \
RESTORE_EXPECTED_CONTAINER_ID=REPLACE_WITH_INSPECTED_FULL_CONTAINER_ID \
BACKUP_AGE_IDENTITY=/secure/recovery/identity.txt \
bash scripts/check-backup-restore.sh /secure/recovery/backup.dump.age
```

The checker verifies the resolved container ID, running state, project/service
labels and isolation label, then pins every operation to that ID. Database
commands specify the local Unix socket and port, preventing inherited PGHOST
from selecting another server. It creates/drops only its unique temporary DB.
The label is operator attestation, not proof that disk capacity is independent:
inspect the mounts and reserve expanded DB/index/temp/WAL space separately.

## Optional backup filesystem reserves

`BACKUP_RESERVE_PATHS` is a newline-separated list of existing absolute **host**
directories. Spaces within paths are preserved; newline-containing paths are
unsupported. An unset/empty value keeps the current archive-only behavior.
For a shell invocation, for example:

```bash
export BACKUP_RESERVE_PATHS=$'/srv/map1/storage\n/var/lib/docker\n/'
```

For a systemd EnvironmentFile use a quoted multiline value, not literal `\n`
escape characters. Select the actual host paths for PGDATA, external tablespaces,
external WAL and root/Docker storage. The same `BACKUP_MIN_FREE_GB` and
`BACKUP_MIN_FREE_INODES` floors apply to every path. The Python guard also accepts
repeated `--reserve-path /absolute/path` arguments. It checks before starting,
during the dump and before accepting success; disappearance or device change of
an additional path fails closed. It does not validate the initial expected UUID.

This guard still surrounds the dump phase, not the whole upload/configuration
workflow. It signals its owned local process group; it cannot guarantee stopping
a daemon-created PostgreSQL process. Check/cancel the owned container/session if
Docker disconnects. Do not regard sampled free-space floors as disk quotas.

## Production storage and aggregate memory overlay

`docker-compose.capacity.yml` is a separate, opt-in overlay for
`docker-compose.prod.yml`. It maps PGDATA and its tablespace directory to the
volume; never apply it to empty directories. Supply
measured `MAP1_DB_CPUS`, `MAP1_DB_MEMORY_LIMIT` and `MAP1_STORAGE_ROOT`; the latter
must be a dedicated directory under `/srv/map1/storage` with existing
`artifacts`, `cache`, `zonal`, `work`, `postgres` and `tablespace` subdirectories
and correct runtime UIDs.
All processing bind mounts use `create_host_path: false`.

Bootstrap sequence for the deployment owner, without starting country work:

1. Preserve the volume receipt outside the disk. The expected UUID is
   `<LOCAL_STORAGE_UUID>`. Keep the original PostgreSQL volume for
   rollback. Copy only after a clean shutdown, compare complete file checksums
   and the cluster system identifier, then verify the copied database. Do not
   rerun formatting. Once the new database accepts writes, the original volume
   is an old snapshot, not a lossless rollback of those subsequent writes.
2. Use the mount-only gate below before applying the overlay. It refuses wrong
   host, UUID, mount target/type, read-only mount, unsafe marker, misplaced data
   paths, or insufficient storage/root bytes/inodes. It creates nothing.
3. Install/start the supplied `map1-workers.slice` only after confirming a
   systemd cgroup-v2 Docker host. It imposes aggregate **4 CPU / 8 GiB RAM /
   zero swap / 256 tasks** across participating workers, beat, tools and migrate
   containers. Installing it starts no application work. Review these ceilings
   against the actual host; changes require corresponding gate/test updates.
4. Render/review the standalone production Compose plus the capacity overlay.
   It bounds per-service CPU/RAM/PIDs/logs and uses `restart: "no"` for added-disk
   consumers. It fixes Celery concurrency/prefetch at one and scheduler at false.
   Do not layer the development Compose file into this deployment.
5. After applying the reviewed limits, run the full gate before each worker
   admission, with `--requested-workers N`. The gate checks actual kernel slice
   limits, actual worker cgroup membership, finite project-container budgets,
   root/storage reserves, RAM headroom and worker count (default maximum two,
   excluding beat). Serialize admission/start operations in the parent launcher.

```bash
python3 scripts/check-production-storage.py --mount-only \
  --storage-root /srv/map1/storage/production \
  --expected-uuid <LOCAL_STORAGE_UUID>

python3 scripts/check-production-storage.py --project map1 \
  --storage-root /srv/map1/storage/production \
  --expected-uuid <LOCAL_STORAGE_UUID> \
  --requested-workers 1
```

The first result explicitly has `capacity_checked=false`. The second requires
already-running, bounded project services and a live bounded slice. Both refuse
missing evidence; neither starts containers. The gate defaults to 40 GiB free
on root and storage, 100000 free inodes, and 2 GiB host RAM reserve. It cannot
account for unlabelled jobs or predict another project's future allocations.
Worker-count checking is admission-time only; the slice enforces the aggregate
RAM/CPU/PID ceilings continuously, including extra containers using that slice.

`nofail` in fstab permits host boot without this disk. Every start/recreate must
pass the mount gate; do not bypass it with direct Docker starts. For unattended
deployment wire the consumer supervisor to `srv-map1-storage.mount` using
`RequiresMountsFor`, `BindsTo` and `After`, with the gate as `ExecStartPre`.
The overlay alone is not a mount-loss supervisor, and the preflight is not a
continuous disk watcher. Keep an owned-job reserve/deadline watcher during work.

`map1-production.service` supervises only db, Redis, API, frontend and Caddy.
Its UUID gate and mount dependency must pass before startup. Docker automatic
restart is disabled so boot cannot bypass that gate. The supervisor checks the
mount and application health continuously, and refuses unexpected processing
containers while no separate processing approval exists. The processing profile
is never started by this service. Its installation is not country-run approval.

For this deployment the inactive `stage1-tools` profile uses host networking,
the loopback PostgreSQL port and the project-only OGM proxy supplied through
`MAP1_OGM_PROXY_URL`. Its GDAL temporary directory is on the verified volume.
Do not use the older unbounded host-network helper for country processing.
Keep the tools profile inactive until the separate launch decision and capacity
admission. The API and public satellite workers do not use the OGM VPN route.

Negative fixtures can import `evaluate_storage()` / `evaluate_snapshot()` from
the gate; see `scripts/tests/test_production_storage.py`. The production CLI has
no fixture, skip-validation or country-approval option.

## Visual forest-mask acceptance

Source parity and zero overlap with stored exclusions do not prove forest cover.
Before a national rebuild, inspect the bounded grid on satellite imagery, record
the coordinates and source IDs of questionable cells, and check the original
exclusion input as well as the database. Review known roads, settlements, fields,
forest interiors and clipped edges. Keep numerical geometry checks separate
from this visual review.

OSM imports must use separate GDAL scratch directories on the processing volume.
The November 4 rehearsal reproduced an exit-zero `ogr2ogr` import that lost 40
of 68 exclusion polygons after its temporary geometry index failed. Treat GDAL
error diagnostics as a failed import even when the process exits successfully.
Before accepting a repaired import, compare the original PBF polygons with the
stored exclusions and grid; clean stderr alone does not prove completeness.
Rebuild affected geometry in an isolated scope before testing downstream results.
Do not reuse the earlier NDVI/carbon/restore proof as acceptance of changed cells.

The November 4 sample has agricultural-looking areas inside the accepted
OGM-plus-OSM mask. Compare them against the actual accepted V10 evidence before
classifying them as restoration regressions. They do not authorize replacing
the accepted union with an intersection or adding a land-cover classifier.
Preserve the observed source-quality limitation in the report; do not turn it
into a new methodology requirement. A green NDVI or verification status is not
evidence that an area is forest. National admission still requires the completed
bounded parity review and the existing operational gates.

The map must also retain visible, selectable cells above zoom 16. Source tiles
remain capped at zoom 16 and are overzoomed in the browser; increasing the view
zoom must not request new national tile levels.

## Remaining runtime checks

Stage6 browser tests now validate TLS without a bypass. Use a trusted certificate
before passing real review credentials. Caddy redacts `X-Stage5-Rules-Token` in
addition to the existing authentication headers. Validate the adapted Caddy
configuration and synthetic-token log output on the parent's isolated target.
The helper cleans credential files on normal exit, command failure and handled
signals, preserves the exit status and bounds Docker stop/CLI cleanup. SIGKILL,
host failure and an unavailable Docker daemon still require scoped recovery.
