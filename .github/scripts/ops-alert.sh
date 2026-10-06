#!/usr/bin/env bash
# Ops alerts as GitHub issues: one open issue per alert title, labeled ops-alert, that
# @-mentions the repo owner so GitHub emails them. Needs GH_TOKEN with issues: write.
#
#   ops-alert.sh open   "<title>" "<message>"   open the issue, or comment on it if already open
#   ops-alert.sh ensure "<title>" "<message>"   open the issue if it isn't open (no repeat comments)
#   ops-alert.sh close  "<title>" "<message>"   comment and close it, if open
set -euo pipefail

action=$1
title=$2
message=$3
label=ops-alert
owner=${GITHUB_REPOSITORY_OWNER:-}

gh label create "$label" --color B60205 --description "Automated alert: the live site needs attention" >/dev/null 2>&1 || true

number=$(TITLE="$title" gh issue list --label "$label" --state open --limit 50 --json number,title \
  --jq '.[] | select(.title == env.TITLE) | .number' | head -n 1)

case "$action" in
  open|ensure)
    if [ -z "$number" ]; then
      gh issue create --label "$label" --title "$title" \
        --body "@${owner} ${message}

_This issue was opened automatically and closes itself once things recover._"
      echo "Opened alert: $title"
    elif [ "$action" = open ]; then
      gh issue comment "$number" --body "$message"
      echo "Updated alert #$number: $title"
    else
      echo "Alert #$number already open: $title"
    fi
    ;;
  close)
    if [ -n "$number" ]; then
      gh issue close "$number" --comment "Recovered. $message"
      echo "Closed alert #$number: $title"
    fi
    ;;
  *)
    echo "usage: ops-alert.sh open|ensure|close <title> <message>" >&2
    exit 2
    ;;
esac
