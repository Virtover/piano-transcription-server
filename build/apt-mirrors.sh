#!/bin/sh
# Points apt at the mirror lists in build/apt, so that a package index or a
# package comes from the next mirror when one fails (apt-transport-mirror).
# apt checks everything against the archive's signature, whichever mirror
# gave it. Run as root in an image build, before apt-get update.
set -eu

here=$(dirname "$0")
. /etc/os-release
mkdir -p /etc/apt/mirrors
cp "$here/apt/$ID.list" "$here/apt/$ID-security.list" /etc/apt/mirrors/

case "$ID" in
ubuntu)
	archive='http://archive\.ubuntu\.com/ubuntu/\{0,1\}'
	security='http://security\.ubuntu\.com/ubuntu/\{0,1\}'
	# The CUDA images also list NVIDIA's repository, which the images install
	# nothing from: without it, its downtime cannot fail a build.
	rm -f /etc/apt/sources.list.d/cuda*.list
	# apt 2.4 of Ubuntu 22.04 hangs taking an index by its hash from the next
	# mirror; by its name, it moves on in seconds. apt 3 does not need this.
	echo 'Acquire::By-Hash "no";' > /etc/apt/apt.conf.d/99mirror-fallback
	;;
debian)
	archive='http://deb\.debian\.org/debian/\{0,1\}'
	security='http://deb\.debian\.org/debian-security/\{0,1\}'
	;;
*)
	echo "build/apt-mirrors.sh: no mirror list for $ID" >&2
	exit 1
	;;
esac

# Security first: its address contains the archive's on Debian.
for file in /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
	[ -f "$file" ] || continue
	sed -i \
		-e "s#$security\([[:space:]]\|\$\)#mirror+file:/etc/apt/mirrors/$ID-security.list\1#g" \
		-e "s#$archive\([[:space:]]\|\$\)#mirror+file:/etc/apt/mirrors/$ID.list\1#g" \
		"$file"
done

# Every source the image uses must now be a mirror list.
if grep -h -v '^[[:space:]]*#' /etc/apt/sources.list /etc/apt/sources.list.d/* 2>/dev/null \
	| grep -E '(^deb[[:space:]]|^URIs:)' | grep -v 'mirror+file:'; then
	echo "build/apt-mirrors.sh: the sources above have no mirror list" >&2
	exit 1
fi
