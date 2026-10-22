#!/usr/bin/env bash
# Linux host: pull images, verify Synology, stop, mirror configs, then start.
set -Eeuo pipefail
umask 077

STACK_DIR=${STACK_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}
CONFIG_SOURCE=${CONFIG_SOURCE:-/docker-configs}
SYNOLOGY_MOUNT=${SYNOLOGY_MOUNT:-/docker}
BACKUP_ROOT=${BACKUP_ROOT:-$SYNOLOGY_MOUNT/backups}
EXPECTED_SYNOLOGY_SOURCE=${EXPECTED_SYNOLOGY_SOURCE:-}
WAIT_TIMEOUT=${WAIT_TIMEOUT:-180}
STOP_TIMEOUT=${STOP_TIMEOUT:-120}

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }
for cmd in docker rsync findmnt flock realpath timeout mktemp python3; do
  command -v "$cmd" >/dev/null || die "Required command missing: $cmd"
done
STACK_DIR=$(realpath -e -- "$STACK_DIR")
CONFIG_SOURCE=$(realpath -e -- "$CONFIG_SOURCE")
SYNOLOGY_MOUNT=$(realpath -e -- "$SYNOLOGY_MOUNT")
BACKUP_ROOT=$(realpath -m -- "$BACKUP_ROOT")
[[ $STACK_DIR != / && $CONFIG_SOURCE != / && $SYNOLOGY_MOUNT != / ]] || die 'Refusing filesystem root paths.'
[[ -f $STACK_DIR/docker-compose.yaml ]] || die 'Compose file missing.'
[[ -d $CONFIG_SOURCE ]] || die 'Configuration source missing.'
[[ -n $(find "$CONFIG_SOURCE" -mindepth 1 -maxdepth 1 -print -quit) ]] || die 'Configuration source is empty; refusing a destructive mirror.'
[[ $BACKUP_ROOT == "$SYNOLOGY_MOUNT/"* ]] || die 'Backup root must be beneath the Synology mount.'
for source_dir in "$CONFIG_SOURCE" "$STACK_DIR"; do
  [[ $BACKUP_ROOT != "$source_dir" && $BACKUP_ROOT != "$source_dir/"* ]] || die 'Backup destination must not be inside a backup source.'
done
CONFIG_DEST=$BACKUP_ROOT/docker-configs
PROJECT_DEST=$BACKUP_ROOT/dockerhomelab
[[ $CONFIG_SOURCE != "$CONFIG_DEST" && $STACK_DIR != "$PROJECT_DEST" ]] || die 'Source and destination must differ.'

exec 9>"$STACK_DIR/.backup-update.lock"
flock -n 9 || die 'Another backup/update is already running.'
cd -- "$STACK_DIR"
compose=(docker compose --project-directory "$STACK_DIR" -f "$STACK_DIR/docker-compose.yaml")
"${compose[@]}" version >/dev/null
"${compose[@]}" config --quiet
# Fail before shutdown if the new secret-file migration has not been completed.
python3 - "${compose[@]}" <<'PYSECRETS'
import json, os, subprocess, sys
result = subprocess.run(sys.argv[1:] + ['config', '--format', 'json'],
                        capture_output=True, text=True)
if result.returncode:
    raise SystemExit('Could not validate Compose secret files.')
for secret in json.loads(result.stdout).get('secrets', {}).values():
    path = secret.get('file')
    if path and (not os.path.isfile(path) or not os.access(path, os.R_OK)):
        raise SystemExit('A required secret file is missing or unreadable; run the secret migration first.')
PYSECRETS
state=$(mktemp -d)
needs_restart=0
upgrade_started=0
previous_services=()

finish() {
  status=$?
  trap - EXIT
  if (( status != 0 && needs_restart && ! upgrade_started )); then
    log 'Backup or shutdown failed; restarting previously running images.'
    if ! "${compose[@]}" -f "$state/previous-images.json" up -d --no-build --pull never "${previous_services[@]}"; then
      log 'ERROR: Recovery failed; manual intervention is required.' >&2
    fi
  elif (( status != 0 && upgrade_started )); then
    log 'ERROR: Upgrade/startup failed. Inspect the stack before retrying; no automatic database rollback was attempted.' >&2
  fi
  rm -rf -- "$state"
  exit "$status"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

verify_synology() {
  local fs remote probe
  fs=$(findmnt -rn --mountpoint "$SYNOLOGY_MOUNT" -o FSTYPE) || die 'Synology mount is absent.'
  remote=$(findmnt -rn --mountpoint "$SYNOLOGY_MOUNT" -o SOURCE) || die 'Cannot determine Synology mount source.'
  case "$fs" in nfs|nfs4) ;; *) die "Expected an NFS mount at $SYNOLOGY_MOUNT; found $fs." ;; esac
  [[ -z $EXPECTED_SYNOLOGY_SOURCE || $remote == "$EXPECTED_SYNOLOGY_SOURCE" ]] || die 'NFS source does not match EXPECTED_SYNOLOGY_SOURCE.'
  # Exercise the mounted filesystem, rather than trusting a directory's existence.
  probe=$(timeout 15 mktemp "$SYNOLOGY_MOUNT/.dockerhomelab-backup-probe.XXXXXX") || die 'Synology write probe failed or timed out.'
  timeout 15 rm -- "$probe" || die 'Synology write-probe cleanup failed.'
}
verify_synology

# Save only service names and immutable image IDs, never inspected credentials.
"${compose[@]}" ps --quiet > "$state/container-ids"
while IFS= read -r id; do
  [[ -n $id ]] || continue
  docker inspect --format '{{index .Config.Labels "com.docker.compose.service"}} {{.Image}}' "$id"
done < "$state/container-ids" > "$state/previous-images.txt"
python3 - "$state" <<'PY'
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
services = {}
for line in (root / 'previous-images.txt').read_text().splitlines():
    name, image = line.split()
    if not image.startswith('sha256:'):
        raise SystemExit('Could not capture immutable running image IDs.')
    services[name] = {'image': image}
(root / 'previous-images.json').write_text(json.dumps({'services': services}, indent=2))
PY
while read -r name _; do
  [[ -n $name ]] && previous_services+=("$name")
done < "$state/previous-images.txt"

log 'Pulling images while the stack remains running.'
"${compose[@]}" pull
verify_synology
if (( ${#previous_services[@]} )); then needs_restart=1; fi
log 'Stopping the stack for a consistent configuration backup.'
"${compose[@]}" down --timeout "$STOP_TIMEOUT"
verify_synology
mkdir -p -- "$CONFIG_DEST" "$PROJECT_DEST"
log 'Mirroring /docker-configs data to Synology.'
rsync -aPhiv --delete --exclude='plex/Library/Application Support/Plex Media Server/Cache/*' -- "$CONFIG_SOURCE/" "$CONFIG_DEST/"
log 'Mirroring project-relative configurations, .env, and secret files to Synology.'
rsync -aPhiv --delete --exclude='.git/' --exclude='.backup-update.lock' --exclude='plex/Library/Application Support/Plex Media Server/Cache/*' -- "$STACK_DIR/" "$PROJECT_DEST/"
cp -- "$state/previous-images.json" "$BACKUP_ROOT/dockerhomelab-previous-images.json"

log 'Backup completed; starting the stack with the pulled images.'
upgrade_started=1
"${compose[@]}" up -d --remove-orphans --no-build --pull never --wait --wait-timeout "$WAIT_TIMEOUT"
needs_restart=0
log 'Backup and update completed.'
