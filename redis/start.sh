#!/bin/sh
# The redis service's entrypoint: start Redis with the users in
# /redis/users.acl. Each #{name} there becomes the SHA-256 hash of the
# password in /run/secrets/redis/<name>, so no password is in a file Redis
# reads, a command line, or `docker inspect`. Stops if a password is missing.
#
# `start.sh reload`, run in the running container, writes the users again and
# makes Redis read them without restarting: scripts/server.py does it after
# every start, so a user or password added by an update takes effect.
set -eu

template=/redis/users.acl
acl=/tmp/users.acl
umask 077

render() {
  next="$acl.next"
  # Redis's user files have no comments.
  tr -d '\r' < "$template" | grep -v -e '^[[:space:]]*#' -e '^[[:space:]]*$' > "$next"
  for name in $(grep -o '#{[a-z_-]*}' "$next" | tr -d '#{}' | sort -u); do
    file="/run/secrets/redis/$name"
    if [ ! -f "$file" ] || [ ! -s "$file" ]; then
      echo "redis: secrets/redis/$name is missing or empty; python scripts/server.py start creates it" >&2
      rm -f "$next"
      exit 1
    fi
    hash=$(tr -d '[:space:]' < "$file" | sha256sum | cut -d ' ' -f 1)
    sed -i "s/#{$name}/#$hash/g" "$next"
  done
  chown redis:redis "$next"
  mv "$next" "$acl"
}

render
if [ "${1:-}" = "reload" ]; then
  REDISCLI_AUTH="$(cat /run/secrets/redis/operator)" redis-cli --user operator --no-auth-warning ACL LOAD
  exit 0
fi

# The image's own entrypoint starts Redis as the redis user.
exec docker-entrypoint.sh "$@" --aclfile "$acl"
