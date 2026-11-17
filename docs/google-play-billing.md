# 💳 Google Play billing

This guide covers the additional setup required when `BILLING_PROVIDER=google_play`. Set up [Google sign-in](google-sign-in.md) first: billing needs it. Uncomment the billing section of [.env.example](../.env.example) in your `.env`, and see [Configuration](configuration.md#billing) for every setting.

## 🧭 Setting it up

The server lists products and verifies and consumes purchases through the Google Play Developer API, as a service account. What you must create, with the reasons, comes from the server itself:

```shell
docker compose run --rm api python -m app.config requirements
```

See the **Google Play** section of its output: the API, the service account and its key, its access in Play Console, and the products from `BILLING_PRODUCTS`.

> [!TIP]
> With an AI coding agent such as Claude Code, run `/setup-google`. It turns each requirement into Google's current settings, guides you through them, and verifies the result.

1. **Create what `requirements` lists**, using Google's documentation: [Play Developer API access](https://developers.google.com/android-publisher/getting_started), [one-time products](https://support.google.com/googleplay/android-developer/answer/1153481), and [testing billing](https://developer.android.com/google/play/billing/test).
2. **Save the service account's JSON key** under the exact name the server expects:

   **Windows (PowerShell)**

   ```powershell
   Move-Item "$env:USERPROFILE\Downloads\<downloaded-key>.json" secrets\google_play_service_account.json
   ```

   **macOS and Linux**

   ```bash
   mv ~/Downloads/<downloaded-key>.json secrets/google_play_service_account.json
   ```

   The key gives access to your app's orders: never commit, email, or paste it. Compose mounts `./secrets` at `/run/secrets`, so the containers read `/run/secrets/google_play_service_account.json`. Older setups that use `GOOGLE_PLAY_SERVICE_ACCOUNT_FILE` keep working, with a deprecation warning.

3. **Configure `server.env`.** Map every product ID to the transcription minutes it buys:

   ```env
   GOOGLE_PLAY_PACKAGE_NAME=com.example.pianoweave
   BILLING_PRODUCTS={"transcription_25":25,"transcription_60":60,"transcription_300":300}
   FREE_MINUTES=10
   FREE_MINUTES_INTERVAL=30d
   ```

   Only products that exist in Play Console **and** are listed in `BILLING_PRODUCTS` are offered through `/api/server-info`. The API reads the products at startup, so restart it after changes; there is no on-demand synchronization endpoint:

   ```shell
   docker compose restart api
   ```

4. **Verify.** `check --online` lists the products with the service account and compares them with `BILLING_PRODUCTS`. A failure names the likely cause: a rejected key, missing permissions, or a wrong package name. Products in only one of the two places are reported as warnings.

   ```shell
   docker compose run --rm api python -m app.config check --online
   ```

   A 401 or 403 right after giving the service account access is usually Google's delay in applying new permissions, which can take up to a day.

## 🗓️ Monthly and paid minutes

Each user has two balances:

* **Monthly minutes**, from `FREE_MINUTES`; "monthly" is their name in the API, but they are renewed every `FREE_MINUTES_INTERVAL`, which need not be a month. Every `FREE_MINUTES_INTERVAL`, on a fixed schedule from the user's sign-up, the ones left unused expire and the balance is renewed to `FREE_MINUTES`. Paying users get them too. The renewal is made at the user's first request in a new period, but the schedule stays: a user who comes back late still gets the next renewal when the current period ends, not a full period later.
* **Paid minutes**, bought or added by hand. They never expire.

A job is paid from the monthly minutes first and from the paid ones for the rest. A job that is running when the period ends keeps the monthly minutes it holds. When it ends, the ones it did not use expire; they never carry over into the new period. The renewal, each reservation, and each settlement are single database transactions, so a crash or power cut leaves either the state before or the state after, never a mix. The database refuses a negative monthly balance.

On upgrading, existing balances become paid minutes, and the current period counts as already given. Each user's first monthly minutes come when that period ends. The server did not record sign-up times before, so an existing user's schedule runs from their last free grant.

## 🔁 Subscriptions

`BILLING_SUBSCRIPTIONS` lists the Google Play subscriptions users can buy, each with a name, the minutes each paid period gives, or `"unlimited"`, and the base plan the app buys:

```shell
BILLING_SUBSCRIPTIONS={"sub_pro":{"name":"Pro","minutes":100,"base_plan":"pro-monthly"},"sub_unlimited":{"name":"Unlimited","minutes":"unlimited","base_plan":"unlimited-monthly"}}
```

Plans have levels by their minutes, unlimited highest. Two plans may share neither a name nor minutes; the server refuses to start otherwise.

* **A plan replaces the free minutes.** Each paid order starts a period that lasts until Google's expiry time, that is, the base plan's billing period set in Play Console. `FREE_MINUTES_INTERVAL` plays no part in it, and the period need not be a month: a weekly or yearly base plan gives its minutes each week or year. Each paid order starts its period: the monthly minutes left unused expire, and the plan's minutes become the monthly balance, spent before paid minutes. A subscriber gets no `FREE_MINUTES` while a period runs. When a subscription ends without renewal, the free schedule starts again from the end of its period.
* **Renewals** are read from Google Play: the API asks for each subscription from 10 minutes before its period ends, every minute, then more rarely, for up to 90 days. The app also sends its subscriptions at start. A renewal's order starts the next period once, however often it is reported.
* **A grace period**, while Google retries a failed payment, keeps the plan but gives no new minutes until the payment comes. An account hold, a pause, or an expiry ends the period at once.
* **Upgrades and downgrades** replace the subscription: the new one's first order starts its period, and the replaced one is never renewed again.
* **Unlimited** plans charge nothing, but each account may start at most `BILLING_UNLIMITED_DAILY_MINUTES` minutes of jobs in any 24 hours; beyond that, a submission is refused with HTTP 429, `error_code` `fair_use_limit`, and `Retry-After`. The per-user job limit applies as for everyone.
* **One account per subscription.** The app gives Google Play the SHA-256 of the user ID as the obfuscated account ID. A subscription bought for, or first sent by, another account is refused with HTTP 409, `error_code` `subscription_owned`. An account deleted and made again by the same person gets it back.
* **Acknowledgement.** The server acknowledges each new subscription, which Google refunds within three days otherwise. If that fails, the answer has `acknowledged: false`, and the renewal reads try again.

The app sends a subscription to `POST /api/billing/google-play/subscriptions/verify` with `product_id` and `purchase_token`, as for products. The answer has `status` (`started` when an order's period started now, `recorded` otherwise), `granted_minutes`, `acknowledged`, and the new balance. Each account may ask to verify products and subscriptions 60 times in 10 minutes.

`GET /api/billing/server-info` lists the plans in `subscriptions`, lowest level first, and `GET /api/billing/balance` gives the running plan in `subscription`, with `unlimited` and `unlimited_minutes_left`.

## 🎁 Welcome minutes

`WELCOME_MINUTES` gives each new account that many paid minutes, once. They never expire. A hash of the account's ID is kept, also after the account is deleted, so that deleting and making an account again does not give them twice. Accounts made before the setting was turned on are not given them; `python -m app.admin users credit-all` gives every account minutes; see [Administration](administration.md#-users).

## 🔄 Purchase flow

The Android client sends the completed purchase's product ID and purchase token to `POST /api/billing/google-play/verify` with the user's Google ID token:

**Windows (PowerShell)**

```powershell
$purchase = @{
    product_id = 'transcription_60'
    purchase_token = '<token-returned-by-google-play>'
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri http://localhost:8000/api/billing/google-play/verify `
  -Headers @{ Authorization = "Bearer $googleIdToken" } `
  -ContentType 'application/json' `
  -Body $purchase
```

**macOS and Linux**

```bash
curl -X POST http://localhost:8000/api/billing/google-play/verify \
  -H "Authorization: Bearer $GOOGLE_ID_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"product_id":"transcription_60","purchase_token":"<token-returned-by-google-play>"}'
```

The server verifies the purchase with Google Play, credits the configured minutes once per purchase token, and consumes the purchase. Repeating the same token returns `credited_minutes: 0`. Google refuses to consume a purchase while another change to it is under way, which happens right after the purchase and when the app sends the same token twice at once; the server then tries again for about 7 seconds, and counts the purchase as consumed if another request consumed it meanwhile. If it still fails, the answer has `consumed: false` and the log says why. The minutes are credited anyway, and the app should send the token again later, for example at its next start, when the purchase is still unconsumed.

## ↩️ Refunds and chargebacks

When a purchase is refunded, charged back, or cancelled, Google Play reports it as voided. The API reads voided purchases when it starts and then every `GOOGLE_PLAY_VOIDED_PURCHASES_INTERVAL` (default `1h`), and takes each one's minutes back once.

* **The balance can go below zero** when the minutes were already used. The user then cannot start transcriptions until a purchase brings the balance back above zero, and `GET /api/billing/balance` returns the negative `minutes`.
* **Monthly minutes do not pay off a negative balance.** Their renewal waits until the paid balance is zero or more, so "buy, use, refund" cannot be repeated for free. Monthly minutes left from before the refund still count against what is owed.
* Each voided purchase is logged as a warning with the order ID, the minutes taken back, and the user ID.
* A voided purchase stays recorded, so its token can never be credited again.
* Google keeps voided purchases for 30 days. If the API was stopped for longer, older ones are missed, and the API logs a warning.
* Purchases of deleted accounts are marked voided without changing any balance.
* **A refunded subscription order** takes back what its period gave: while the period runs, its unused minutes from the monthly balance and those already used from the paid one; for an ended period, the minutes it used. A refunded unlimited order charges the minutes of the jobs it ran. With the latest order, the subscription ends at once.
* `python -m app.admin billing` lists the users with a negative balance and when voided purchases were last read; see [Administration](administration.md#-billing).

Reading voided purchases needs the service account's access to orders and financial data, which `requirements` lists and `check --online` verifies.

## ⏱️ Transcription charges

For a video lasting $d$ seconds, the full cost in minutes is:

$$
m =
\begin{cases}
1, & d / 60 < 2 \\
\lfloor d / 60 \rfloor, & d / 60 \ge 2
\end{cases}
$$

The full amount is reserved before a job is queued. Completed jobs charge the reservation; failed jobs release it. Cancelled jobs charge based on progress $p$:

$$
c =
\begin{cases}
0, & p < 0.05 \\
\lceil 0.6 \times m \times p \rceil, & p \ge 0.05
\end{cases}
$$

The cancellation charge cannot exceed the reserved cost. Production billing
records are stored in PostgreSQL; `DATABASE_PATH` is retained only for
tests and the one-time SQLite migration source.

## 🗑️ Account deletion

Google Play requires apps that let users create an account to also let them delete it. The client can call `DELETE /api/account` with the user's Google ID token. See **Account deletion** in the [API guide](api.md) for what is deleted and what is kept.

Google Play also requires a web link where users can request deletion without the app. Handle those requests with `python -m app.admin user <email> delete`, described in [Administration](administration.md#-users).
