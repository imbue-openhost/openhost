
## File system

### logical setup (what apps and users “see”)

- each user has a “disk” with folders etc like google drive.
- there’s some standard structure for the folders
- apps have permissions to subsets of this drive
- each app has three storage areas with different durability + size + latency tradeoffs:
    - **permanent data (`app_data`)** — local disk, small, fast, backed up, legible to users.  Standard file types, exportable, importable.  This is where SQLite databases and other embedded-DB stores live (LMDB, RocksDB, BoltDB) — the latency profile is right and the strict POSIX consistency keeps WAL fsyncs safe.
        - examples: sqlite files. markdown notes. JSON config. small assets.
    - **archive data (`app_archive`)** — S3-backed (via JuiceFS) once the operator configures a bucket from the dashboard.  Disabled by default on fresh zones; apps that require it (`app_archive = true`) won't install until S3 is configured.  Large, elastic, durability tied to the operator's S3 provider.  Higher latency than `app_data` on uncached reads.  Intended for bulk content the app would otherwise outgrow local disk for.
        - examples: source jpegs / RAWs in a photo library, original video files, attachment uploads, large model weights.
    - **temporary data (`app_temp_data`)** — local disk scratch, not backed up, recreatable on demand.
        - examples: low-res thumbnails generated from source photos, transcoding work files, in-flight upload chunks.
- there’s also a folder for router-level data, eg the sqlite database used by the router.
- where do app build artifacts go? probably in app temp data?
- folder structure (as seen inside containers, mounted at `/data/`)
    - /data/
        - app_data/
            - app_name/
        - app_temp_data/
            - app_name/
        - app_archive/
            - app_name/
        - router_data/
            - router.db
- regardless of permissions, apps should see the same folder structure, just only with folders they have access to. that way the structure doesn't change if the permissions change. without any special permissions, apps will just have basically `/data/app_data/APP_NAME` and `/data/app_temp_data/APP_NAME` (and `/data/app_archive/APP_NAME` if requested).

### Why three tiers, not two

`app_data` and `app_archive` look similar from the in-container perspective — both are POSIX directories the app reads/writes — but their host backings have different access patterns and constraints.

`app_data` is local NVMe.  Microsecond random reads, fsync that means something, strict POSIX.  This is where SQLite WAL files have to live: a WAL needs shared-memory mappings the kernel propagates between processes on the same host, and it needs `fsync()` to actually durably commit.  A network FS that gives close-to-open consistency or that buffers writes in a daemon process would corrupt SQLite databases silently.

`app_archive` is S3-backed via JuiceFS (operator opt-in via the dashboard, one-shot configuration; disabled until configured).  It has tens-to-hundreds-of-ms first-touch reads, eventual durability, and no shared-memory mmap.  Apps that put the wrong data here — SQLite, anything using `fcntl` advisory locks for correctness — would hit data loss or corruption.  The manifest validator rejects `app_archive = true` without `app_data` (or `sqlite`, or `access_all_data`) so apps always have somewhere safe to put their working state.  Apps with `app_archive = true` are blocked from install/reload until the operator configures the S3 backend; if you want a local-disk archive, run minio (or equivalent) yourself and point the operator UI at it.

### API

- for now apps will have direct access to a POSIX file system
- this will be implemented by mounting the appropriate folders from the host into the app containers.

### Where data actually lives

- permanent data (`app_data`) lives on the host's local disk under `persistent_data_dir/app_data/<app>`
- temp data (`app_temp_data`) lives on a separate subdirectory under `temporary_data_dir`, so that backups can target only the persistent data
- archive data (`app_archive`) is disabled by default; once the operator configures the S3 backend from the dashboard, archive bytes route through a JuiceFS mount of the operator-supplied bucket.  The in-container path apps see is always `/data/app_archive/<app>/`.  Configuration is one-shot per zone — once configured, the choice is permanent.
- the JuiceFS metadata database is small and lives on the host's local disk under `persistent_data_dir/openhost/juicefs/state/meta.db`; the standard backup picks up that directory.  A planned but not-yet-implemented daily `juicefs dump` will write a JSON snapshot alongside the SQLite metadata file so a fresh VM restoring from backup has everything it needs to reattach to the existing S3 bucket via `juicefs format` + `juicefs load`.  Until that's wired up, recovery is "back up the SQLite metadata file directly" — see the JuiceFS upstream docs.

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

