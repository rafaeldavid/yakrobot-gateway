#!/usr/bin/env bash
# Start the gateway for the Freenove car.
#
# PYTHONPATH is rebuilt from the venv's own .pth files rather than trusting
# them. On this machine (Python 3.14, uv 0.10.6) `site` silently declines to
# process editable-install .pth files: the file content and target directory
# are both valid, sys.path just never gets them. The package imports right
# after `uv sync --reinstall-package` and breaks again on the next run.
#
# That bit twice — first `yakrobot_cli` (gateway would not start), then
# `yakrobot_descriptor` (GET /{robot}/descriptor answered 501 "export is not
# installed" when it demonstrably was, which blocked registration at
# register.yakrobot.com). Reading the .pth files ourselves fixes the whole
# class rather than one path at a time.
#
# Also PYTHONUNBUFFERED, because the CLI print()s the tunnel URL and a
# redirected stdout block-buffers it into invisibility.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

SP="$(echo .venv/lib/python*/site-packages)"
EXTRA=""
for pth in "$SP"/*.pth; do
  [ -f "$pth" ] || continue
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      /*) [ -d "$line" ] && EXTRA="$EXTRA:$line" ;;   # absolute dir entries only;
    esac                                              # `import ...` lines are not paths
  done < "$pth"
done

export PYTHONPATH="$PWD/src${EXTRA}${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUNBUFFERED=1
exec .venv/bin/python -m yakrobot_cli "$@"
