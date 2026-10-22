#!/usr/bin/env bash
# Install the saved crontab for the current user only when none exists.
set -euo pipefail
command -v crontab >/dev/null || { printf '%s\n' 'crontab is not installed.' >&2; exit 1; }
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source_file=$root/crontab.txt
[[ -s $source_file ]] || { printf '%s\n' 'Saved crontab.txt is missing or empty.' >&2; exit 1; }
state=$(mktemp -d)
trap 'rm -rf -- "$state"' EXIT
if LC_ALL=C crontab -l > "$state/current" 2> "$state/error"; then
  printf '%s\n' 'A crontab already exists for this user; left unchanged.'
  exit 0
fi
if ! grep -Eq '(^|: )no crontab for ' "$state/error"; then
  printf '%s\n' 'Cannot read the current crontab; nothing was installed.' >&2
  exit 1
fi
crontab "$source_file"
printf '%s\n' 'Installed crontab.txt for the current user.'
