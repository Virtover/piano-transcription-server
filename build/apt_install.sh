#!/bin/sh
# apt-get install with a time limit, tried up to three times: Ubuntu 22.04's
# apt (2.4) can hang for good while it moves a download to the next mirror of
# build/apt (seen in an image build: one install stopped for 50 minutes on a
# package another run fetched in a second). A hung attempt is ended and the
# install starts again; packages already fetched are kept in apt's cache.
# Usage, after build/apt-mirrors.sh and apt-get update:
#   sh /build/apt_install.sh python3 ffmpeg ...
# APT_INSTALL_TIMEOUT (seconds, default 900) is for tests.
set -u
limit="${APT_INSTALL_TIMEOUT:-900}"
for attempt in 1 2 3; do
	if timeout "$limit" apt-get install -y --no-install-recommends "$@"; then
		exit 0
	fi
	echo "build/apt_install.sh: attempt $attempt failed or took over $limit s" >&2
	[ "$attempt" -lt 3 ] && sleep 5
done
echo "build/apt_install.sh: apt-get install failed three times" >&2
exit 1
