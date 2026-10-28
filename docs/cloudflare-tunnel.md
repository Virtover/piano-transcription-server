# 🌐 HTTPS through a Cloudflare Tunnel

Optional. A [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) gives the server an HTTPS address such as `https://piano.example.com` without opening a port on your router or setting up certificates. The `cloudflared` service in `docker-compose.yml` connects out to Cloudflare and forwards requests to the API on the Compose network. It starts only when `.env` turns it on.

It needs a domain whose DNS is managed by Cloudflare. A free Cloudflare plan is enough, with one limit on uploads: see [Upload size](#-upload-size).

> [!IMPORTANT]
> The tunnel makes the server reachable from the whole internet. Without billing, set a [server password](setup.md#-password-protection) first, or anyone who finds the address can use your GPU. Cloudflare decrypts the traffic at its edge, so name it as a processor in your privacy policy if others use the server.

## 🧭 Setting it up

What Cloudflare must provide comes from the server itself, with values calculated from your configuration:

```shell
docker compose run --rm api python -m app.config requirements
```

1. **Create the tunnel and its public hostname** as `requirements` lists them, using [Cloudflare's guide to remotely managed tunnels](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/get-started/create-remote-tunnel/). Send the hostname to `http://api:8000`: `cloudflared` runs in the Compose network, where `localhost` is `cloudflared` itself.
2. **Save the tunnel token.** Cloudflare shows it inside an install command; copy only the token, the long text that starts with `eyJ`. The commands ask for it, so it stays out of your shell history:

   **Windows (PowerShell)**

   ```powershell
   New-Item -ItemType Directory -Force secrets\cloudflare | Out-Null
   $token = Read-Host "Tunnel token"
   Set-Content -Path secrets\cloudflare\tunnel_token -Value $token.Trim() -NoNewline
   Remove-Variable token
   ```

   **macOS and Linux**

   ```bash
   mkdir -p secrets/cloudflare
   read -rsp "Tunnel token: " token; echo
   printf '%s' "$token" > secrets/cloudflare/tunnel_token
   unset token
   ```

   The token is in a folder of its own so the workers, which process untrusted media, cannot see it. On Linux, `cloudflared` runs as user ID `65532`, so the file must be readable by it: files created this way are, unless your `umask` is strict.
3. **Turn it on in `.env`:**

   ```env
   COMPOSE_PROFILES=cloudflare
   CLOUDFLARE_TUNNEL_HOSTNAME=piano.example.com
   # Optional: only the tunnel can reach the API, not devices on your network.
   API_BIND_ADDRESS=127.0.0.1
   ```

   `COMPOSE_PROFILES` is read by Docker Compose and starts `cloudflared`; `.env` is the only switch. `CLOUDFLARE_TUNNEL_HOSTNAME` is only used to check the setup.
4. **Start it and verify:**

   ```shell
   docker compose run --rm api python -m app.config check
   docker compose up -d
   docker compose run --rm api python -m app.config check --online
   ```

   `check` fails if the token file is missing, and warns if the server has no password or sign-in, or if port `8000` is still open on every network interface. `check --online` confirms that `cloudflared` is connected to Cloudflare, that `https://<hostname>/health` reaches this server, and, with a password, that the API refuses requests without it.
5. **Use the address.** In Piano Weave or any other client, set the server address to `https://piano.example.com`.

`python scripts/server.py restart` and `stop` include the tunnel. While the server is stopped, Cloudflare answers the address with an error page.

The optional [admin web page](administration.md#-admin-web-page) uses the same tunnel, with a second hostname behind Cloudflare Access. Never give the API's hostname a sign-in: the app could not pass it.

## 📦 Upload size

Cloudflare limits the size of a request body, and the limit depends on the plan. An upload above it is refused by Cloudflare and never reaches the server, so the app can only show a generic error. Set `MAX_UPLOAD_SIZE_MB` to at most your plan's limit: apps read it from `GET /api/server-info` and refuse larger files themselves. Links to videos are not affected.

## 🔌 Turning it off

Remove `COMPOSE_PROFILES=cloudflare` from `.env`, then stop and remove the container:

```shell
docker compose --profile cloudflare rm -sf cloudflared
```

If you set `API_BIND_ADDRESS=127.0.0.1`, set it back to `0.0.0.0` and run `docker compose up -d` so devices on your network can reach the API again. Delete the tunnel at Cloudflare if you no longer need it.
