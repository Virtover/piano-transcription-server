#!/bin/sh
# The redis service's entrypoint: start Redis with the users in
# /redis/users.acl. Each #{name} there becomes the SHA-256 hash of the
# password in /run/secrets/redis/<name>, so no password is in a file Redis
# reads, a command line, or `docker inspect`. Stops if a password is missing.
#
# A home cluster's nodes are users too: one line of /redis/node.acl for each
# password file in /run/secrets/redis/nodes, named after the node. A file
# whose name is not a node's name is skipped, with a message.
#
# Redis replays its append-only file at start as the default user, which
# refuses everything: every transaction in it (MULTI/EXEC) would be dropped,
# and jobs would come back as they were before them. So Redis starts with an
# open default user but no network port, only a socket inside this
# container; once the file is loaded, the real users are loaded and port 6379
# opens. Should anything fail, the port stays closed.
#
# `start.sh reload`, run in the running container, writes the users again and
# makes Redis read them without restarting: scripts/server.py does it after
# every start, and scripts/admin.py after it invites or revokes a node.
set -eu

template=/redis/users.acl
node_template=/redis/node.acl
nodes=/run/secrets/redis/nodes
acl=/tmp/users.acl
socket=/tmp/redis-start.sock
port=6379
umask 077

password_hash() {
  tr -d '[:space:]' < "$1" | sha256sum | cut -d ' ' -f 1
}

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
    hash=$(password_hash "$file")
    sed -i "s/#{$name}/#$hash/g" "$next"
  done
  if [ -d "$nodes" ] && [ -f "$node_template" ]; then
    line=$(tr -d '\r' < "$node_template" | grep -v -e '^[[:space:]]*#' -e '^[[:space:]]*$')
    for file in "$nodes"/*; do
      [ -f "$file" ] || continue
      name=$(basename "$file")
      # The name goes into the user's key patterns: only a node's name.
      if ! printf '%s' "$name" | grep -Eq '^[a-z0-9][a-z0-9-]{0,30}$'; then
        echo "redis: skipping secrets/redis/nodes/$name: not a node's name" >&2
        continue
      fi
      if [ ! -s "$file" ]; then
        echo "redis: skipping secrets/redis/nodes/$name: the password file is empty" >&2
        continue
      fi
      hash=$(password_hash "$file")
      printf '%s\n' "$line" | sed -e "s/{name}/$name/g" -e "s/{hash}/$hash/g" >> "$next"
    done
  fi
  chown redis:redis "$next"
  mv "$next" "$acl"
}

if [ "${1:-}" = "reload" ]; then
  render
  REDISCLI_AUTH="$(cat /run/secrets/redis/operator)" redis-cli --user operator --no-auth-warning ACL LOAD
  exit 0
fi

# Checked now, so a missing password stops the container before Redis starts.
render
users="$acl.users"
mv "$acl" "$users"

# While the append-only file loads: an open default user, reachable only
# through the socket, which only Redis's own user and root can open.
printf 'user default on nopass ~* &* +@all\n' > "$acl"
chown redis:redis "$acl"

open_port() {
  # PING answers LOADING until the file is loaded.
  until [ "$(redis-cli -s "$socket" ping 2>/dev/null)" = "PONG" ]; do
    sleep 0.2
  done
  mv "$users" "$acl"
  if [ "$(redis-cli -s "$socket" ACL LOAD 2>&1)" != "OK" ]; then
    echo "redis: could not load the users; port $port stays closed" >&2
    exit 1
  fi
  REDISCLI_AUTH="$(cat /run/secrets/redis/operator)" redis-cli -s "$socket" --user operator --no-auth-warning \
    CONFIG SET port "$port" > /dev/null
  echo "redis: users loaded; listening on port $port" >&2
}
open_port &

# The image's own entrypoint starts Redis as the redis user.
exec docker-entrypoint.sh "$@" --aclfile "$acl" --port 0 --unixsocket "$socket" --unixsocketperm 700
