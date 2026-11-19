# Backblaze B2 backup setup

Use a client-owned B2 Cloud Storage account in EU Central. The account region is
chosen at registration. Billing, MFA and account recovery remain under the
client's control. Do not keep owner-console credentials on the application host.

Create private project storage and obtain its actual S3 endpoint and region.
Use a standard application key for S3 requests: `keyID` is the access key ID and
`applicationKey` is the secret access key. The system master key is not an S3
credential.

In the private storage environment, select `BACKUP_S3_PROVIDER=backblaze-b2`
and `BACKUP_S3_OBJECT_LOCK_MODE=COMPLIANCE`. This adapter requires retained
versions. It is distinct from the default generic S3 publication protocol.

`infra/backblaze-backup-key.example.json` describes a bucket- and prefix-scoped
routine key using the B2 Native API. Replace the account and bucket identifiers.
The generic AWS IAM policy is not a B2 policy. Create a separate read-only
recovery key without `writeFiles`, and keep it outside the application server.
The routine key needs `readFileRetentions` and `writeFileRetentions`; the recovery
key needs `readFileRetentions` to verify retained versions.

The routine key has no key administration, bucket administration, governance
bypass or explicit file-version deletion capability. B2's `writeFiles` capability
can still hide a current object name or upload another version. This is not an
immutability guarantee by itself. The adapter applies COMPLIANCE retention to
both encrypted archives and completion markers, verifies retention using HEAD,
and records archive version IDs in each marker. Daily objects are protected for
seven days and weekly objects for 28 days. Keep the completion marker's returned
version ID with the backup receipt. Recovery can select that exact marker version
and then download the archive versions it identifies, even if current names have
been hidden or replaced. Retention protects those versions; it cannot prevent a
compromised writer from hiding current names or uploading additional versions.

If the original server or its receipts are unavailable, use the read-only recovery
environment to list retained completion marker versions:

```bash
python3 scripts/backup_offsite.py list-versions --max-pages 10 --page-size 1000
python3 scripts/backup_offsite.py download KEY DEST --manifest-version-id VERSION
```

Listing defaults to daily backups; `--group weekly` selects weekly backups.
Follow any returned continuation cursors before treating a truncated result as
complete. Select a known suitable snapshot and validate its data in a disposable
database before deployment.

B2 returned `501 NotImplemented` for conditional `PutObject(IfNoneMatch="*")`
during the account acceptance checks. The explicit B2 adapter publishes a unique
marker, pins its returned version ID, reads that version back and verifies its
COMPLIANCE retention. It does not silently retry a failed conditional request as
an unconditional write. Generic S3 retains conditional marker publication.

The B2 adapter also removes the SDK's `Expect: 100-continue` header and supplies
the transport checksum required for Object Lock uploads. These compatibility
controls do not replace complete SHA-256 readback or authenticated `age`
decryption.

Use `infra/backblaze-backup-lifecycle.example.json` with the supported S3 lifecycle
API. Each age-based expiration has B2's required paired delete-marker rule.
B2 can regenerate the paired rule IDs. Compare the returned retention controls
and prefixes rather than requiring those display IDs to remain identical.
Keep subsequent lifecycle changes on that same API. These rules hide daily
objects after seven days and weekly objects after 28 days, then delete noncurrent
versions after one additional day. Protected versions and processing delays can
increase stored volume. Review costs using all retained versions.

Before enabling the daily timer, test a small object and an object larger than
64 MiB against the actual account, including upload, full readback, download and
an isolated encrypted database restore. Verify prefix restrictions and denied
explicit version deletion using disposable test objects. Verify the selected
publication protocol, retention of the actual uploaded versions, and recovery by
version ID after hiding a diagnostic object's current name. A list-only preflight
does not establish these behaviors.

If testing identifies an SDK checksum compatibility issue, the environment
settings `AWS_REQUEST_CHECKSUM_CALCULATION=when_required` and
`AWS_RESPONSE_CHECKSUM_VALIDATION=when_required` restrict optional SDK checksum
features. They do not replace the transfer command's complete SHA-256 readback
or authenticated `age` decryption. Do not silently remove conditional writes or
integrity checks after a provider error.

Record the actual endpoint, key permissions, lifecycle response, measured backup
size, transfer usage and restore result. Account creation and configuration
templates are not proof of a working external backup.

References: [B2 S3 keys](https://www.backblaze.com/docs/cloud-storage-s3-compatible-app-keys),
[B2 lifecycle API](https://www.backblaze.com/apidocs/s3-put-lifecycle-configuration),
[B2 data regions](https://www.backblaze.com/docs/cloud-storage-data-regions).
