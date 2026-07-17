
## File system

### logical setup (what apps and users “see”)

- each user has a “disk” with folders etc like google drive.
- there’s some standard structure for the folders
- apps have permissions to subsets of this drive
- each app has three storage areas with different durability + size + latency tradeoffs:
    - **permanent data (`app_data`)** — local disk, small, fast, backed up, legible to users.  Standard file types, exportable, importable.  This is where SQLite databases and other embedded-DB stores live (LMDB, RocksDB, BoltDB) — the latency profile is right and the strict POSIX consistency keeps WAL fsyncs safe.
        - examples: sqlite files. markdown notes. JSON config. small assets.
    - **archive data (`app_archive`)** — bulk storage for content the app would otherwise outgrow local disk for.  ALWAYS available: by default it is backed by **local disk** on the instance (`backend = 'local'`), so apps that request it install immediately on any zone.  The operator can later upgrade the zone to **S3** (via JuiceFS) from the dashboard for durable, elastic object storage; existing local archive data is migrated into the bucket at that point.  Higher latency than `app_data` on uncached reads once on S3.  Intended for bulk content: source jpegs / RAWs in a photo library, original video files, attachment uploads, large model weights.
        - Durability: on the `local` backend the archive lives under `persistent_data_dir` and IS included in backups, but has no off-instance copy.  On the `s3` backend durability is tied to the operator's S3 provider (and the archive is deliberately excluded from restic backups because the bytes already live in the bucket).
    - **temporary data (`app_temp_data`)** — local disk scratch, not backed up, recreatable on demand.
        - examples: low-res thumbnails generated from source photos, transcoding work files, in-flight upload chunks.
- there’s also VM-level / router data (eg the sqlite database used by the router). apps see this in-container as `/data/vm_data/` (only with the `access_vm_data` permission); on the host the router db lives at `persistent_data/openhost/router.db`.
- where do app build artifacts go? probably in app temp data?
- folder structure (as seen inside containers, mounted at `/data/`)
    - /data/
        - app_data/
            - app_name/
        - app_temp_data/
            - app_name/
        - app_archive/
            - app_name/
        - vm_data/          # VM-level / router shared data (only with access_vm_data)
- regardless of permissions, apps should see the same folder structure, just only with folders they have access to. that way the structure doesn't change if the permissions change. without any special permissions, apps will just have basically `/data/app_data/APP_NAME` and `/data/app_temp_data/APP_NAME` (and `/data/app_archive/APP_NAME` if requested).

### Why three tiers, not two

`app_data` and `app_archive` look similar from the in-container perspective — both are POSIX directories the app reads/writes — but their host backings have different access patterns and constraints.

`app_data` is local NVMe.  Microsecond random reads, fsync that means something, strict POSIX.  This is where SQLite WAL files have to live: a WAL needs shared-memory mappings the kernel propagates between processes on the same host, and it needs `fsync()` to actually durably commit.  A network FS that gives close-to-open consistency or that buffers writes in a daemon process would corrupt SQLite databases silently.

`app_archive` has two backings the operator chooses between per zone:

* **`local`** (the default) — a plain directory on the instance's local disk (under `persistent_data_dir`, so it survives rebuilds and is backed up).  Always available; no configuration needed.
* **`s3`** — S3-backed via JuiceFS (operator opt-in via the dashboard).  Tens-to-hundreds-of-ms first-touch reads, elastic capacity, durability tied to the S3 provider, and no shared-memory mmap.

Either way, apps that put the wrong data on the archive — SQLite, anything using `fcntl` advisory locks for correctness — would hit data loss or corruption, so app authors should pair `app_archive` with `app_data` (or `sqlite`) to keep working state on local disk.  Because the archive tier is always available, apps with `app_archive = true` install on any zone; on a `local`-backed zone the operator is shown a notice at install time that the app's bulk data will live on non-durable local disk until they configure S3.

Upgrading `local` → `s3` is a one-shot, one-way operation: the platform formats + mounts the bucket, **migrates the local archive data into it (copied and size-verified before the switch; fail-open — if anything fails the local data is left intact and the backend stays `local`)**, flips the backend to `s3`, then removes the migrated local copy.  Once a zone is on `s3` it cannot be reconfigured.

### API

- for now apps will have direct access to a POSIX file system
- this will be implemented by mounting the appropriate folders from the host into the app containers.

### Where data actually lives

- permanent data (`app_data`) lives on the host's local disk under `persistent_data_dir/app_data/<app>`
- temp data (`app_temp_data`) lives on a separate subdirectory under `temporary_data_dir`, so that backups can target only the persistent data
- archive data (`app_archive`) defaults to the `local` backend: a directory at `persistent_data_dir/app_archive_local/<app>` on the host.  (It lives at a DIFFERENT host path from the S3 mountpoint, `data_root_dir/app_archive`, so a local directory can never silently shadow a later JuiceFS mount.)  Once the operator upgrades the zone to S3, archive bytes route through a JuiceFS mount of the operator-supplied bucket at `data_root_dir/app_archive/`.  The in-container path apps see is always `/data/app_archive/<app>/`, regardless of backing.  The `local` → `s3` upgrade migrates existing local data into the bucket; going to S3 is one-way and permanent.
- the JuiceFS metadata database is small and lives on the host's local disk under `persistent_data_dir/openhost/juicefs/state/meta.db`; the standard backup picks up that directory.  A planned but not-yet-implemented daily `juicefs dump` will write a JSON snapshot alongside the SQLite metadata file so a freshly-installed zone restoring from backup has everything it needs to reattach to the existing S3 bucket via `juicefs format` + `juicefs load`.  Until that's wired up, recovery is "back up the SQLite metadata file directly" — see the JuiceFS upstream docs.

### permissions

- apps can request access to the entire data dir, or to specific apps, and/or to the router’s data.
- there should also be a permission explicitly requesting access to the app's own POSIX file system - some apps won't need this at all.
  - separate permissions for permanent and temp data dirs, too.
- this probably gives access just to the “permanent data”. idk that we need cross-app access to temp data.
- for specific app access, the app will specify like “i want access to the user’s emails”, and the user will probably have to select the app name that they use for email. and we’ll eventually need some protocols for interoperability of data formats, eg between different email apps.

## relational DB

- we'll offer access to a sqlite db as an explicit permissioned resource, so that later this can be swapped to a distributed db or whatever without changing the app code.
- the actual sqlite file will be stored in the app's permanent data dir, so it will be backed up and legible to users, but apps generally shouldn't access this directly.


## Router-level data

the router stores its own state (database, TLS certs, etc.) in the configured data directory alongside app data.

