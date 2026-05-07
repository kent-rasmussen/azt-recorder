#!/usr/bin/env bash
# Print the path of the highest-version APK under bin/.
set -euo pipefail
cd "$(dirname "$0")"
ls -1 bin/*.apk 2>/dev/null | sort -V | tail -n 1
