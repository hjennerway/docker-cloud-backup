# Docker Cloud Backup

A single small container for backing up Docker **configuration and application state**, with a web UI, per-stack schedules, SMB uploads, Google Drive mirroring and push notifications. No image exports, Docker-in-Docker, NAS mounts, Node build step or external database.

## Screenshots
![Dashboard](/screenshots/cloud-backup3.png "Dashboard")
![Backup activity](/screenshots/cloud-backup2.png "Backup activity")
![7d/30d scheduled view](/screenshots/cloud-backup1.png "7d/30d scheduled view")


## Start

```sh
git clone https://github.com/hjennerway/docker-cloud-backup.git
cd docker-cloud-backup
cp .env.example .env
# Optionally set ADMIN_PASSWORD in .env; otherwise a random one is generated.
docker compose up -d --pull always
docker compose exec cloud-backup cat /data/admin-password.txt
```

Open `http://YOUR_DOCKER_HOST:8088` and sign in. The generated password file exists only when `ADMIN_PASSWORD` is unset. Keep `.env` out of version control. All schedules start **paused**; discovery never stops a container by itself.

After signing in, use **Change password** in the top bar. Enter the current password and confirm a new one of at least 12 characters. The app stores a salted password hash in the data volume, so the change survives restarts. `ADMIN_PASSWORD` supplies the initial password only; once credentials have been saved, changing that environment variable does not reset them. A successful password change removes the generated `admin-password.txt` file and signs out other browsers; your current browser stays signed in.

1. Review discovered stacks and choose automatic config mappings, selected volumes or manual config paths.
2. Configure and test the SMB destination, and optionally Google Drive.
3. Run a manual backup and check Activity. Download and test restoring a small archive.
4. Enable a cron schedule for each stack you want protected.

On **Stacks**, turn on **Include in manual backup** for each stack you want to run together, then click **Backup N selected** at the top. Selections start off and persist across refreshes and app restarts, independently of schedules and backup contents. The button includes selected stacks hidden by search and queues a separate backup for each, running one stack at a time. Review any selected stack with config issues before starting. If a container cannot restart, remaining selected backups do not start until recovery is resolved; run the selection again after recovery.

The app uses the local Docker socket. A second Dockge agent is a separate Docker host: deploy another instance there to protect its containers.

## Dockge

Create a stack named `cloud-backup` in Dockge and paste `compose.yaml`. It uses the published image `ghcr.io/hjennerway/docker-cloud-backup:latest`; no Git checkout or image build is needed on the Docker host. When migrating an existing stack, remove its `build` and `pull_policy: build` entries, replace `image` with that reference, and keep its existing ports, environment and volumes.

Deploy, open port 8088, and retrieve the generated password using the container's Bash terminal:

```sh
cat /data/admin-password.txt
```

Use Dockge's **Update** button to pull the current `latest` image and recreate the container. Publishing a new image does not automatically restart Dockge stacks. For a reproducible deployment or rollback, use `ghcr.io/hjennerway/docker-cloud-backup:sha-<full commit SHA>` instead of `latest`.

The `backup-data` named volume preserves schedules, mappings, encrypted destination credentials, job history and local archives. Keep that volume and its `secret.key` together. The app excludes its own stack from backup, so it cannot stop itself. Back up its data volume separately while the app is stopped if you want to preserve the app's own state.

## Container publishing

The **Checks** GitHub Actions workflow runs unit tests, validates Compose, builds the image and runs Docker integration tests. Successful pushes to `main` then call `publish.yml` to publish Linux AMD64 and ARM64 images to [GitHub Container Registry](https://github.com/hjennerway/docker-cloud-backup/pkgs/container/docker-cloud-backup), tagged `latest` and `sha-<full commit SHA>`. Pull requests run checks without publishing. You can also run **Checks** manually on `main` from the Actions page. Publishing uses the built-in `GITHUB_TOKEN` with `packages: write`; no registry password secret is needed.

New GHCR packages default to private, even for public repositories. After the first publish, make the container package public in its package settings to allow unauthenticated pulls. If keeping it private, authenticate to `ghcr.io` on the Docker host with a personal access token (classic) granting `read:packages`, and make those credentials available to Dockge's Docker client. See [GitHub's Container registry documentation](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).

## What is backed up

- Every Compose file listed in `com.docker.compose.project.config_files`, including override files.
- The stack `.env`, and files explicitly referenced by `env_file`, top-level `configs.file` and `secrets.file`.
- The container paths chosen for each stack: automatic image mappings, one or more complete volumes/bind mounts, or manually entered config files/folders. Data is read via Docker's archive API.
- A `manifest.json` containing image references, path mappings, original running states and restore information.

**Not included:** base image layers, full container exports, unselected volumes and media/download directories, host-wide files, external Docker secrets/configs, external databases or image build contexts. Images must remain pullable or be rebuilt from separately protected source. SQLite and similar app databases often live in config directories and are included as necessary application state.

The app reads raw Compose files rather than a substituted `docker compose config` dump. Compose input secrets are deliberately preserved for recovery; downloaded archives must therefore be treated as sensitive.

Docker Compose projects are grouped by labels. Saved inactive Dockge stacks are discovered from `STACKS_ROOT` and Dockge's own mounted stacks directory. Inactive stacks and standalone `docker run` containers appear with explanations; create Compose containers before enabling their config backups. Compose `include` / `extends` and unresolved environment-based file paths fail preflight rather than produce incomplete archives. Use a flattened standalone Compose file for these cases.

## Other Docker hosts

The **Stacks** page defaults to **All hosts**, showing stacks in one sorted list with a host badge beside each name. Edit each row's cron schedule and timezone, then choose **Save schedule**. Scheduled-backup and manual-backup switches save immediately to that row's host. Edits survive background refreshes; failed saves retain your input for correction or retry. Offline hosts show their connection error while reachable hosts remain visible. Search includes host, stack and container names. Backup and content-selection actions open the corresponding host's workspace, so identically named stacks on different machines remain separate.

The **Docker host** selector switches the workspace between hosts, including stacks, backups, restore targets, schedules, destinations and activity. Choosing a host sets the Stacks page to **Selected Docker host**. Each host runs its own Cloud Backup instance using the Compose setup above, with its own data volume and local Docker socket/host mounts. The host's worker performs backups, restores and restart recovery locally, including while the connection to your main instance is interrupted. Hosts are explicitly configured; the app does not scan the network.

Open **Remote hosts → Add remote host** in the main app. Enter a friendly name, the other machine's Cloud Backup address (for example `http://192.168.1.20:8088`) and its admin password. **Test connection** checks sign-in and Docker availability without saving. **Save host** adds it to the selector immediately, with no container restart or file editing. You can save an offline host and connect later.

Use **Edit or test** to change a connection or check it again. Leave the password blank to keep it; changing the address requires re-entering the password. Saved passwords are encrypted in the main instance's data volume and never returned to the browser. **Remove** deletes only the saved connection; the remote machine's containers, backups and schedules continue unchanged. Host management always applies to the main app, even when a remote workspace is selected.

Connection errors distinguish a rejected remote admin password from an unknown hostname, unreachable host, timeout, HTTPS certificate problem, redirect, rate limit or unexpected app response. Use the current password for the remote Cloud Backup sign-in page; an initial `ADMIN_PASSWORD` value no longer applies after changing it in the app. The address must be reachable and its hostname resolvable from the main Cloud Backup container. If the remote instance uses `COOKIE_SECURE=true`, connect through its HTTPS address; a correct password over HTTP cannot establish that secure session.

For file-based deployment, you can alternatively create a private `docker-hosts.json` file:

```json
[
  {
    "id": "nas",
    "name": "NAS",
    "url": "https://nas-backup.example.com",
    "password": "the-current-admin-password-on-the-nas"
  }
]
```

Add `./docker-hosts.json:/run/docker-hosts.json:ro` to its Compose volumes and set `DOCKER_HOSTS_FILE=/run/docker-hosts.json` in `.env`, then recreate the main container. Use the remote instance's reachable base URL without a subpath. Host IDs must be unique lowercase letters/digits/hyphens, up to 40 characters; `local` is reserved. Protect this file like an admin password, keep it out of source control, and use HTTPS on untrusted networks. File-managed hosts appear as read-only in the UI and take precedence over saved entries with the same ID. Changes to the file require restarting the main instance; update the file if a remote password changes.

Choose a host in the sidebar to work with its containers. Stack names can repeat across hosts because each host keeps its own settings and history. Downloads and restore uploads pass through the main instance; allow disk space there for an uploaded archive and set `MAX_BACKUP_GB` appropriately on both instances. A remote outage leaves the selector available so you can switch back. Close open dialogs and finish pending actions before switching. Running backup jobs continue on their original host.

Google Drive's LAN token connection works through the selector. For browser-based Google OAuth, open the remote instance directly to complete sign-in. **Change password** always changes the main instance's password. Install this version on every connected host, and run only one instance per host/data volume.

## Image mappings

Mappings live in SQLite (`/data/backup.sqlite3`) and are edited in **Image mappings**. Seed rules cover common Sonarr, Radarr, Prowlarr, SABnzbd, qBittorrent, Seerr/Jellyseerr, Jellyfin, Uptime Kuma, Portainer, Mosquitto, n8n, go2rtc, NetAlertX, Dockge, Otter Wiki, Home Assistant, Frigate and Dashy images.

- Use a repository pattern without tag or digest, e.g. `lscr.io/linuxserver/sonarr` or `*/sonarr`.
- A bare image like `eclipse-mosquitto:2` normalizes to `library/eclipse-mosquitto`.
- More specific patterns take precedence. Rules match all tags; review them after major image upgrades.
- Enter exact **container** paths, one per line. A file such as `/config/go2rtc.yaml` is supported.
- Exclusions are relative to each selected directory, e.g. `cache/**` or `logs/**`. A file rule has no child paths to exclude.
- An empty path list explicitly marks an image as stateless (Compose only).
- Every mapped path must exist. Saving a mapping checks its paths in each current container where that rule wins the image match. If none exist yet, the mapping is saved as unverified until discovery. Auto-matched paths are checked during discovery, and missing paths appear as **Needs review**. Backups check again **before** stopping any containers.

NetAlertX seed paths use the current `/data/config` and `/data/db` layout. Older images used `/app/config` and `/app/db`: edit that rule for those versions. Paths differ between publishers; the UI shows actual mounted container paths to help review mappings. The app does not guess that every `/data` directory contains only config.

## Choosing volumes or config files

The **Stacks → Volumes** column lists each mounted named/anonymous volume and bind mount with its uncompressed file size. Shared mounts are listed once per stack. Sizes are cached for about a minute and do not follow child symlinks. An inaccessible host mount or a scan exceeding the time/file limit shows **Size unavailable**, not zero; it can still be selected and read through Docker. Narrowing `/host` may make sizes unavailable. Inactive stacks need their containers created before mounted volumes can be discovered.

The **Backup contents** column shows the saved choice and its approximate uncompressed size, for example **Config files — ≈5 MB** or **Selected volumes — ≈50 MB**. Totals include Compose inputs, honor config exclusions, and count shared selected volumes once. They estimate file contents before compression, rather than the final archive size. Host scans are bounded and cached for about a minute. For container-local config or inaccessible host storage, running containers are measured using a two-second metadata scan with `timeout`, `find` and `stat`, without a shell or downloading file contents. Images without those utilities, incomplete scans and unreadable paths show **Size unavailable**. Stopped containers remain stopped; individual file sizes can still be read through Docker metadata.

Dashy's mapping includes `/app/user-data`, covering its config, additional pages and assets even when that folder is stored inside the container. A Dashy Compose file with its `volumes:` section commented out has no mounted volume, so an empty volume list is expected; use automatic config mappings. The new Dashy mapping is added once to existing installations without overwriting a custom mapping.

Use **Choose contents** on a stack to select one of:

- **Automatically matched config paths** (the default for existing stacks): use image mappings and their exclusions.
- **One or more volumes**: select any subset of named volumes and bind mounts. Their complete contents are backed up without image mapping exclusions. Shared volumes are archived once, with all mount locations recorded in the manifest. Nested mounts are copied only when separately selected.
- **Manually specified config files or folders**: enter absolute container paths per Compose service, one per line. Blank services contribute no config paths. At least one path is required; files such as `/config/settings.yaml` are supported. Every replica of a service uses the same paths.

Selections persist across restarts and apply to both manual and scheduled backups. Volume/manual mode does not require image mappings. Missing paths, removed selected volumes or removed selected services prevent backup until the selection is corrected. Compose inputs are always included, and all running containers in the stack still stop together for consistency. Whole volumes count toward `MAX_BACKUP_GB` (10 GiB by default).

## Backup lifecycle and recovery

1. Acquire the single backup worker and validate mappings, files, container states and minimum free space.
2. Record previously running containers in a durable SQLite restart journal **before** stopping them.
3. Stop the stack in reverse `depends_on` order, allowing 30 seconds for each graceful shutdown. Docker may kill a process that exceeds this timeout.
4. Stream selected paths into one compressed tar archive. No extraction, shell execution, image save/export or nested compressed archives. File modes, ownership IDs and symlinks are preserved.
5. In a `finally` block, restart previously running containers in dependency order, with retries. Containers that were already stopped remain stopped. Running state is checked; application-specific readiness/health is not guaranteed.
6. Once containers are back, compute SHA-256 and upload copies. Each provider reports its own result; size is checked after upload. A failed upload retains the local archive and exposes **Retry upload** without stopping containers again.

After an app restart, the journal is replayed before new backups can run. Unresolved restarts are shown prominently and retried every ten seconds. This cannot restart containers while the Docker host itself is unavailable; it resumes recovery when Docker returns. Only run **one app instance per data volume / Docker host**. Do not run multiple ASGI workers or replicas.

The default uncompressed archive limit is 10 GiB (`MAX_BACKUP_GB`). Config should be much smaller. Keep enough disk space in `backup-data`; failures caused by space exhaustion retain the restart journal. Local partial archives are not treated as successful backups.

## Schedules and retention

Five-field cron: `minute hour day-of-month month day-of-week`. The UI offers hourly, daily, weekly and monthly presets, editable fields and the next five run times. Each stack has an IANA timezone such as `Europe/London`. Standard cron OR semantics apply when both day fields are restricted. Preview around daylight-saving changes if wall-clock timing matters.

Open **Scheduled** to see every upcoming backup run in chronological order. Toggle **7d** or **30d** to view the next seven or thirty days, with each run displayed in its stack's timezone. Frequent schedules use Previous/Next pages of 100 jobs. Paused schedules are hidden; a missed schedule appears once as overdue, waiting for the worker. Edit a schedule from its row or use **Refresh jobs** to update the window.

Schedules and their next due time survive restart. A missed schedule is run once after recovery, without replaying every missed interval. A busy worker defers other due stacks. Default retention is seven days; it runs after a successful backup and only deletes older successful backups for that stack. The newest successful archive, failed/partial jobs and unrelated remote files are retained. If destinations change, old remote files are retained for manual review. Retention only acts on remote file receipts recorded by this app; there is no broad remote sync/delete.

## SMB

In Destinations, enter `//NAS/backups/docker`, username, password and optional domain. UNC (`\\NAS\backups\docker`) and `smb://` paths are accepted. **Test connection** is a read-only listing test; upload permissions are checked by the actual backup.

rclone speaks SMB directly, so the container does not need `privileged`, `SYS_ADMIN`, `mount.cifs` or a mounted NAS share. Files are stored beneath `<chosen path>/cloud-backup/<stack-and-id>/`. SMB and Google Drive are independent destinations; if one fails the other is still attempted.

## Google Drive

### LAN connection (no public hostname required)

Install [rclone](https://rclone.org/install/) on a computer with a browser, then run:

```sh
rclone authorize "drive"
```

Complete Google sign-in and consent yourself. Paste the complete resulting token JSON into **Destinations → Google Drive → Connect or reconnect**, then connect. The app verifies that it can list My Drive before saving the connection; a failed reconnect keeps your previous settings. Browse to the desired Drive folder, choose **Use this folder**, enable mirroring and save. Refresh tokens are encrypted in SQLite and updated after rclone refreshes them. If Google rejects an access token before its recorded expiry, the app forces one refresh and retries the operation.

If you authorize with a custom rclone client, set that same client ID and secret in the app before connecting the token. Do not mix tokens from rclone's built-in client with a different client ID.

Drive API requests are paced to avoid bursts. Request rate limits are retried with increasing delays and jitter, within the operation's original timeout; an upload verification retry does not re-upload the archive. Activity distinguishes API request limits from daily API quotas and full account storage. Daily quotas, storage limits and authorization errors need corrective action rather than repeated upload retries. Persistent request limits can come from rclone's shared OAuth project even when your own Drive has plenty of free space.

[rclone's shared Google client is being retired during 2026](https://rclone.org/drive/#making-your-own-client-id), so your own client is recommended. If the default connection fails, create a Google **Desktop app** OAuth client with Drive API enabled using that guide, then authorize with the matching credentials:

```sh
rclone authorize "drive" "CLIENT_ID" "CLIENT_SECRET"
```

Enter that same client ID and secret in the app before connecting the new token. Grant full Drive access so existing destination folders can be listed. The connection check reports rejected tokens, client mismatches, missing permissions, disabled APIs and quota limits without exposing credentials. It checks read access; the first backup checks upload permission.

### Browser OAuth

Register your own Google **Web application** OAuth client with the Drive API enabled. Set its client ID, secret and exact redirect URI ending in `/oauth/google/callback`. Google requires an HTTPS hostname (or localhost), not a private LAN IP. Access and sign in to the app through that hostname, then click **Save & sign in with Google**. OAuth state is short-lived, single-use and bound to the signed-in browser session. Enable `COOKIE_SECURE=true` when using HTTPS. A Google OAuth project in testing mode may require periodic reauthorization; configure its publishing status appropriately.

The folder browser uses full Drive scope to list existing folders. Files are added to a `cloud-backup` subfolder within your selection. Selecting a folder does not make that folder public. Disconnect clears the local token; revoke the app separately in your Google account if you want to revoke the grant itself. Shared Drive management and service-account setup are not included in this first version.

## Notifications

Activity always provides status with no external setup. Optional providers:

- **ntfy**: select a server and topic, then subscribe to that topic in the mobile app. Use an authenticated/self-hosted server for private alerts, or an unguessable topic for a simple setup.
- **Telegram**: create a bot through BotFather, send it a message, then set its bot token and your numeric chat ID.
- **Pushbullet**: provide an access token; the app sends a note.

Each notification type (successful backup, partial upload, failed backup and test) has a title and message editor. Available placeholders are `{stack}` (stack name), `{status}` (event status), `{detail}` (result or error description), and `{job_id}` (job ID). Messages support multiple lines. Use `{{` and `}}` for literal braces. Templates apply to every provider.

Save settings before using **Send a test**. Failure alerts are sent when enabled; successful-run alerts can be disabled. Delivery failures appear on the job without changing backup success to failure. WhatsApp is not included because its business API setup is disproportionate for a small home-server tool.

## Restore

Open **Restore** to restore configuration or volume data into an existing container on the local Docker host:

1. Select a local container. Running and stopped containers, including standalone containers, are listed. The backup app and excluded stacks are omitted.
2. Upload a backup and click **Upload & review backup**. Accepted formats are an existing Cloud Backup `.tar.gz`/`.tgz`, a ZIP containing exactly one such backup, or a ZIP containing `manifest.json` and the original `containers/...` directory layout at its root. An arbitrary ZIP of config files has no destination mappings and is rejected.
3. Select the original container inside the backup if it contains several. The image repository must match the target; tags may differ. Review the destination paths, confirm that the listed files can be overwritten, and start the restore.
4. Follow progress and the result in **Activity**. The app stops a running target, writes the backed-up files through Docker, and restarts it, including when a write fails. A target that was already stopped stays stopped. Failed restarts use the same durable restart journal and automatic recovery as backups.

Restoration overwrites files present in the backup and keeps other files, including excluded cache/log files. It does not recreate containers, change images or overwrite host Compose files. Restore destinations must already exist (a directory backup needs its directory; a file backup needs its parent). Read-only destinations and storage shared with another running container fail preflight; stop the other consumer first. Use a separate test container when checking a backup for the first time.

Uploads and unpacked data are limited by `MAX_BACKUP_GB`. Uploaded files are stored privately in the data volume, bound to the signed-in session, and expire after one hour. The scheduler cleans up expired uploads. The upload is removed after a restore attempt; restart recovery retains its journal separately. Only one backup, restore or recovery operation can run at a time. Archives are checked for path traversal, duplicate entries, unsafe links and unsupported file types before writing container data. ZIPs containing a `.tar.gz` preserve its file modes and ownership IDs; direct ZIPs use their file modes and the destination directory's ownership IDs. A failed write can leave some files restored; review Activity before retrying.

For a complete stack recovery onto a new host, restore the Compose inputs manually first:

1. Download an archive and compare its SHA-256 against Activity (`sha256sum archive.tar.gz`).
2. Inspect `manifest.json` and the archive listing before extracting a trusted archive into a new, empty directory. Do not blindly extract unknown archives as root.
3. Restore the files under `compose/<original absolute path>` to the stack directory, including `.env` and referenced files. Adjust host paths if restoring to a different machine.
4. Pull the images referenced in Compose (or rebuild from separately protected source), then `docker compose create` to create **stopped** containers and volumes.
5. For each manifest entry, copy the corresponding archived file/folder into the recorded container `source` path. `docker cp -a` works with stopped containers and preserves ownership IDs. When restoring a directory's contents, use its `/.` suffix, e.g. `docker cp -a extracted/containers/<name-id>/0/config/. container-name:/config/`.
6. Check ownership/permissions, secrets, external networks and database dependencies; then start the stack and check application health.

The archive excludes image layers by design. A config-only backup cannot recover separately stored media or an external database. For databases shared across stacks, coordinate downtime and use a database-native backup strategy.

## Security and deployment boundary

This is a single-administrator tool for a trusted LAN/VPN. The Docker socket grants host-level control, and the read-only `/host` mount allows Compose-file access at paths recorded by Docker. Login is required for every config, backup and download API. Signed HTTP-only sessions, request-origin checks, login throttling and a restrictive Content Security Policy are included. Never publish this port directly to the Internet; use a trusted HTTPS reverse proxy/VPN if remote access is needed.

The host mount can be narrowed if all relevant Compose files live in known directories, e.g. replace `/:/host:ro` with `/opt/stacks:/host/opt/stacks:ro` and add any other directories explicitly. `DAC_READ_SEARCH` allows reading Compose and `.env` files owned by other UIDs; the host mount remains read-only. Container data is read through Docker's archive API; it does not require exposing all Docker volume paths to the app.

Destination secrets are encrypted at rest, with the encryption key stored separately in the same protected data volume. This protects against accidental database disclosure, not an attacker who can read the entire volume or Docker host. Archives themselves are **not encrypted**. No NAS password, Google token or notification token is built into the image or committed in this repository.

## Development and checks

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
docker build -t docker-cloud-backup:local .
RUN_DOCKER_TESTS=1 pytest -q
```

On PowerShell, activate with `.venv\Scripts\Activate.ps1` and set `$env:RUN_DOCKER_TESTS='1'`. Integration tests create and clean up only uniquely named disposable test containers and volumes. They verify named-volume content, media/cache exclusion, stopped-container preservation, restart and restoration into a clean container. Unit tests cover restart failures, lost stop responses, recovery locking, path preflight, archive traversal/size limits, encrypted settings, cron, authentication, CSRF and retention isolation.

The runtime is Python/FastAPI + SQLite + a single rclone binary, with vanilla HTML/CSS/JavaScript. See the [Docker archive API](https://docs.docker.com/reference/api/engine/), [rclone SMB](https://rclone.org/smb/), [rclone Drive](https://rclone.org/drive/) and [Google web OAuth](https://developers.google.com/identity/protocols/oauth2/web-server) documentation for the integration contracts.

## License

Docker Cloud Backup is licensed under the [GNU General Public License, version 3](LICENSE) (`GPL-3.0-only`).
