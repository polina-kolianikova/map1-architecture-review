# External backup and recovery

Recovery bundles contain an encrypted PostgreSQL custom-format dump and an
encrypted configuration archive. The configuration archive contains the deployed
source archive, runtime environment and limited storage upload configuration.
Include the deployed source manifest, backup job environment and effective Compose
override when used. For Healthchecks, set `BACKUP_NOTIFICATION_ENV_DIR` to include
only the two per-check ping environments. Keep the monitoring management API key
off the application host; it is not part of these files. An optional project source-access configuration can be
included. It must not contain the backup decryption key or storage owner credentials.

The transfer service is separate from the application, has no Docker socket,
uses an explicit storage credential, and cannot discover credentials from a
personal AWS profile. The storage account must belong to the client and use a
provider independent of the application host.

## Required configuration

1. Create a private bucket in the client storage account. Disable public access.
   Enable versioning. Apply retention using the storage owner's credentials.
2. Adapt `infra/s3-backup-uploader-policy.example.json` to the selected provider
   and bucket. Use provider-specific permissions where AWS IAM is unavailable;
   [Backblaze B2 setup](backblaze-backups.md) describes its separate adapter and
   key capabilities. Restrict the routine identity to the project bucket and
   prefix, without administration or permanent version deletion. For B2, retained
   versions remain recoverable even though a writer can hide current names.
   Use a separate read-only recovery identity on a replacement host.
3. Adapt `infra/s3-backup-lifecycle.example.json`. Daily bundles expire after
   seven days. Monday bundles are also uploaded under `weekly/` and expire after
   28 days. Noncurrent versions are retained for one further day, so allow roughly
   eight days of daily bytes and 29 days of weekly bytes, plus lifecycle delays
   and any overwritten versions. Incomplete multipart uploads expire after one day. This is a time
   retention policy, not a guarantee of seven successful backups after missed jobs.
   Provider support and version cleanup must be verified before enabling jobs.
4. Generate an `age` recovery identity on a trusted client-controlled machine.
   Keep two secure copies outside the application server and the storage account.
   Only its public recipient goes on the application host.
5. Install `infra/backup.env.example` as
   `/srv/map1/secrets/backup-s3.env`, and `infra/backup-job.env.example` as
   `/srv/map1/secrets/backup-job.env`. Both files must be owned by `map1deploy`
   with mode `0600`. For email alerts, put the authorized recipient in
   `BACKUP_ALERT_EMAIL_TO` only in this private file. Set `BACKUP_ALERT_EMAIL_FROM`
   to a single sender mailbox verified with the client-provided relay, and set
   `BACKUP_SMTP_HOST`, `BACKUP_SMTP_PORT`, `BACKUP_SMTP_USERNAME` and
   `BACKUP_SMTP_PASSWORD` using dedicated relay credentials. Both addresses must
   be plain mailboxes without display names. Only authenticated TLS is supported:
   port 465 uses SSL and port 587 requires STARTTLS, with certificate and hostname
   verification. Do not use a personal mailbox password or grant mailbox access.
   Email takes priority when a recipient is configured; a configured
   `BACKUP_ALERT_WEBHOOK` (client-designated HTTPS) is the fallback if email setup
   or delivery fails. Without a usable relay or webhook, the notifier exits
   unsuccessfully; a journal entry is not delivery. Email success means the relay
   accepted the message, not confirmed inbox delivery. The notifier uses Python's
   standard library on the host, a 15-second socket timeout and no retries, and
   works even when Docker is unavailable. Live acceptance still requires a
   client-provided relay and an authorized delivery check.
6. Keep `/srv/map1/app/source.tar.gz` synchronized with the actual deployed
   release. Set `BACKUP_SOURCE_MANIFEST` to the deployed manifest: the job rejects
   an archive whose SHA-256 differs before starting the dump. Include the effective
   override through `BACKUP_DEPLOY_OVERRIDE_FILE` and the job configuration through
   `BACKUP_JOB_ENV_FILE`. Keep the accompanying source commit and archive checksum
   with the release records. Confirm that the runtime environment and override
   describe that same deployment.

Storage policies are templates, not an already provisioned account. Generic S3
compatibility does not guarantee support for every AWS IAM or lifecycle field.
Apply and test the selected provider's equivalent controls before acceptance.

## Run and verify

Run as `map1deploy` from `/srv/map1/app` after loading the private backup-job
environment, with `age` installed on the host:

```bash
scripts/backup-offsite.sh
```

The local dump is streamed through encryption. A failed pipeline does not publish
a final archive. The configuration is assembled in a private temporary directory,
encrypted, and removed. Storage transfer uses bounded multipart concurrency.
Archive sizes, metadata and the actual uploaded bytes are checked before
`COMPLETE.json` is uploaded. Generic S3 uses conditional marker publication.
The explicit B2 adapter requires COMPLIANCE retention and verifies a pinned marker
version instead. Retained archive versions are also pinned in the marker.
Reading each stored archive back is a full additional
download and must be included in the provider's transfer budget and backup window.
A completion marker is required for recovery and freshness checks. S3 metadata
alone does not authenticate the stored bytes: download checks the complete SHA-256,
and `age` authenticates the encrypted content when decrypting it.

Only the latest successfully uploaded local bundle is retained. Failed transfers
are retried before another dump is started; their original snapshot time is
preserved. An old snapshot does not become fresh merely because it is uploaded
again. Set `BACKUP_MAX_DUMP_GB` from the measured archive size with headroom. Before
starting, the job reserves that amount, space for configuration and the configured
database free-space reserve (40 GiB by default). A kernel file-size limit stops an
archive that exceeds its cap. An underestimated cap causes a failed backup and
cleanup rather than allowing the archive to consume the database reserve. Allow
space for the existing verified copy as well as the next archive. The supplied
1 GiB example is for initial small-database checks and must be reviewed after the
full data rebuild. Concurrent disk usage by other workloads still needs monitoring.

Install the `map1-offsite-backup*` units and `map1-backup-alert@.service` only after
the first successful real-provider upload, download and isolated restore. Do not
run the old local-only daily backup timer alongside the new job. The daily timer
runs at 02:30 UTC with up to 20 minutes of jitter. A separate hourly check fails
when the newest completed snapshot is older than 30 hours or any
required object is missing. Both failures invoke the configured notifier.

## Recovery rehearsal

Use a disposable database or a separate replacement host. Never restore a test
dump over the working database.

```bash
docker compose --project-name map1-backup --env-file runtime.env \
  -f infra/docker-compose.backup.yml run --rm --no-deps backup-transfer \
  download map1/daily/REPLACE_BUNDLE_ID/COMPLETE.json /restore/rehearsal
```

The destination must not already exist. Downloaded files become visible only
after all archive checksums pass. With the recovery identity available on the
isolated restore machine, run:

```bash
BACKUP_AGE_IDENTITY=/secure/client-recovery-identity.txt \
  scripts/check-backup-restore.sh /path/to/rehearsal/map1-turkey-TIMESTAMP.dump.age
```

The PostgreSQL client major version must match the database major version. The
existing production database image provides version 16 tools. Restored runtime
roles and grants must be recreated with `scripts/configure-db-runtime-role.sh`;
database dumps intentionally omit ownership and deployment-specific tablespaces.

Decrypt the configuration archive only into a protected recovery directory,
inspect it, and restore secrets with mode `0600`. Check the complete deployed
schema, partitioned cells, Layer B history, carbon assessments and audit guards,
then run authenticated API and browser acceptance checks. Record row counts,
scope IDs, release hash, elapsed restore time and the checked backup key.

An emulator test proves the transfer and failure handling code. It does not prove
an independent provider's retention, permissions, availability, or production data
recovery. Initial container startup and a complete database recovery have separate
timings; a large restore is not covered by a 15 to 20 minute startup target.

References: [S3 API permissions](https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-with-s3-policy-actions.html),
[S3 lifecycle](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lifecycle-mgmt.html),
[Boto3 managed uploads](https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/upload_file.html).
