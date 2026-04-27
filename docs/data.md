
## File system

### logical setup (what apps and users “see”)

- each user has a “disk” with folders etc like google drive.
- there’s some standard structure for the folders
- apps have permissions to subsets of this drive
- each app has a permanent and temporary storage area.
    - permanent is for stuff that needs backed up. it should be legible to users - standard file types. could be exported and imported into a different app relatively easily.
        - examples: sqlite files. markdown notes. source jpegs for a photo library. etc.
    - temporary is for app-specific stuff that the user doesn’t need to think about and can get recreated on-demand if needed. caches and whatnot.
        - examples: low-res thumbnails generated from source photos.
- there’s also a folder for router-level data, eg the sqlite database used by the router.
- where do app build artifacts go? probably in app temp data?
- folder structure (as seen inside containers, mounted at `/data/`)
    - /data/
        - app_data/
            - app_name/
        - app_temp_data/
            - app_name/
        - router_data/
            - router.db
- regardless of permissions, apps should see the same folder structure, just only with folders they have access to. that way the structure doesn't change if the permissions change. without any special permissions, apps will just have basically `/data/app_data/APP_NAME` and `/data/app_temp_data/APP_NAME`.

### API

- for now apps will have direct access to a POSIX file system
- this will be implemented by mounting the appropriate folders from the host into the app containers.

### Where data actually lives

- persistent data (app_data, router_data) lives in the configured data directory
- temp data (app_temp_data) lives in a separate subdirectory on the same disk, so that backups can target only the persistent data

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

