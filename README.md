# Docker homelab

Compose configuration for a media stack, FreshRSS, and Kavita.

## Access and networks

- Public through Traefik: Seerr (`request`), Plex (`plex`), FreshRSS (`rss`), Kavita (`books`). Each application must require its own authentication.
- Other routed services use the LAN allowlist: `127.0.0.1/32,192.168.86.0/24`.
- All media applications share the `media` network; qBittorrent and its forwarding helper share Gluetun's network namespace.
- FreshRSS and Kavita have separate networks, each shared with Traefik.
- Portainer has a separate management network. It retains full Docker management privileges.
- Traefik uses a restricted socket proxy on a separate internal network. Container inspection remains available and can expose environment variables; Docker write requests are blocked.
- Traefik logs at `INFO` to container stdout; Docker rotates its JSON logs at 10 MB per file with three files retained. View them with `docker compose logs traefik`.
- Traefik and the socket proxy are pinned by digest. Other image tags continue to follow the configured upstream versions during updates.

Direct published service ports remain available on the host. Router/firewall rules must restrict external access to the intended services. Traefik's allowlist only protects requests passing through those routers.

## Deploying an existing installation

Do not run this checkout against production until its paths and project name match the existing deployment:

1. Preserve the existing Compose project name (set `COMPOSE_PROJECT_NAME` in `.env` when needed). Moving the Compose file can change the inferred name.
2. Preserve the existing application directories. Relative bind mounts resolve beneath the Compose project directory; cloning into a new location does not migrate application state. Copy/migrate existing directories deliberately, or adjust the bind sources before deployment.
3. Bring your existing `.env` to the deployment directory. Never commit it or generated secrets.
4. Prepare the three wired secret files as described below.
5. Validate with `docker compose config --quiet`, deploy, and check application connectivity and certificate issuance. Recheck private hostnames from outside the LAN.

Application and credential backups live on the Synology; protect access to that share.

## Migrating credentials to secret files

From the deployment directory:

```sh
python3 scripts/migrate-compose-secrets.py
```

Alternatively, pass `--env-file /absolute/path/to/.env`. Secret files are created beside that `.env`, so use the `.env` next to the deployed Compose file.

The script copies present values for all 15 configured secret names into `secrets/`, using Docker Compose's dotenv parser. Each filename is the lowercase variable name. It prints no credential values, sets the directory to mode `0700` and files to `0600`, and refuses to overwrite different values. It leaves `.env` unchanged.

| Variables | Consumer migration status |
| --- | --- |
| `CLOUDFLARE_DNS_API_TOKEN` | Wired to Traefik via `CLOUDFLARE_DNS_API_TOKEN_FILE` |
| `CLOUDFLARE_DDNS_API_KEY` | Wired to Cloudflare DDNS via `API_KEY_FILE` |
| `WIREGUARD_PRIVATE_KEY` | Wired to Gluetun via `WIREGUARD_PRIVATE_KEY_SECRETFILE` |
| `PLEX_TOKEN`, `TRAKT_CLIENT_SECRET`, `MDB_API_KEY`, `TMDB_API_KEY`, `QBT_PASSWORD` | Copied when present; current Compose consumers still use `.env` |
| `TAILSCALE_AUTH_KEY`, `KOMODO_DB_PASSWORD`, `BW_PASSWD`, `YUBICO_SECRET_KEY`, `OPENVPN_PASSWORD`, `GLUETUN_PASS`, `GSP_GTN_API_KEY` | Copied when present; no consumers are configured in this Compose file |

After a successful deployment with this Compose version, only `CLOUDFLARE_DNS_API_TOKEN`, `CLOUDFLARE_DDNS_API_KEY`, and `WIREGUARD_PRIVATE_KEY` may be removed from `.env`, provided no other stack or script still uses them. Keep every other entry until all of its consumers read the corresponding secret file. Creating the file alone does not migrate an application.

Missing optional values are skipped; no empty placeholder files are created. The three wired credentials must exist either in `.env` or in their existing nonempty secret files. Rerunning works after removing previously migrated entries from `.env`. Both single dollar signs and literal `$$` are preserved. The corrected names are `TAILSCALE_AUTH_KEY` and `OPENVPN_PASSWORD`; the misspelled variants are not migrated.

This Compose file cannot start Traefik, Cloudflare DDNS, and Gluetun until their secret files exist. Keep `.env` private (`chmod 600 .env`). Compose secrets are mounted files, not an encrypted secret store.

## Scheduled backup and update

Run `scripts/backup-update.sh` on the Linux Docker host. Requirements: Bash, Docker Compose v2, Python 3, rsync, and standard Linux utilities including `findmnt`, `flock`, `timeout`, and `realpath`. The account must be able to control Docker, read all application data, and write to the NFS share while preserving permissions.

Defaults:

| Setting | Default |
| --- | --- |
| `STACK_DIR` | Parent directory of `scripts/` |
| `CONFIG_SOURCE` | `/docker-configs` |
| `SYNOLOGY_MOUNT` | `/docker` |
| `BACKUP_ROOT` | `/docker/backups` |
| `EXPECTED_SYNOLOGY_SOURCE` | Unset; optionally enforce the exact `findmnt` NFS source |
| `STOP_TIMEOUT` | 120 seconds |
| `WAIT_TIMEOUT` | 180 seconds |

The script:

1. Locks out overlapping runs, validates Compose configuration and required secret files, verifies that `/docker` is an actual NFS mount, and performs a temporary write probe.
2. Records immutable image IDs for the running services, then pulls configured images while the stack stays running. A pull failure aborts without stopping the stack.
3. Rechecks the mount and stops the stack for backup.
4. Mirrors `/docker-configs/` to `/docker/backups/docker-configs/`, retaining `--delete` and excluding Plex's cache.
5. Mirrors the project directory (including relative application state, `.env`, and secret files) to `/docker/backups/dockerhomelab/`, excluding `.git`, the lock file, and Plex's cache.
6. Saves the previous image mapping at `/docker/backups/dockerhomelab-previous-images.json` and starts the updated stack with `--remove-orphans --wait`.

These are mirrors, not historical snapshots: source deletion is propagated deliberately. Existing destination-only files will be deleted. Do not use either destination for unrelated data.

If shutdown or backup fails, previously running services are restarted using their captured image IDs, without pulling or building. The script still exits nonzero. If upgraded startup fails, it exits nonzero and requires inspection; automatic rollback could be unsafe after an application's database migration. `--wait` checks running/healthy state, but services without health checks still require application verification.

The script never automatically builds FreshRSS from its optional `edge` build context. It pulls its configured image; startup uses `--no-build --pull never`.

Example cron entry (replace the checkout path and choose a writable log path):

```cron
0 5 * * * /absolute/path/to/dockerhomelab/scripts/backup-update.sh >> /absolute/path/to/dockerhomelab-update.log 2>&1
```

Use an absolute path to Docker or set an appropriate cron `PATH` if it is not found. To enforce the exact Synology share, set `EXPECTED_SYNOLOGY_SOURCE` to the output of `findmnt -rn --mountpoint /docker -o SOURCE` in the job environment.

## Local checks

```sh
bash -n scripts/backup-update.sh
python3 -m unittest discover -s tests -v
docker compose config --no-interpolate --quiet
```

Backup tests use fake Docker/NFS/rsync commands and temporary directories; they do not stop containers or modify real backups. Secret migration tests use synthetic credentials and the installed Compose parser, without requiring a running Docker daemon.
