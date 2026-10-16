# MyStatus Device Admin

## First start and tokens

Run `./setup.sh`. The script keeps existing credentials and generates missing tokens into `.env` with mode `0600`.

- `STATUS_API_TOKEN`: Android event uploads and heartbeats.
- `STATUS_ADMIN_TOKEN`: web console login and admin API. It must be a different random value.

Do not put either token in a URL, commit it, paste it into issue reports, or include it in screenshots.

## HTTPS access

Use the HTTPS reverse proxy already in front of the receiver:

- Direct host/subdomain proxy: `https://YOUR_DOMAIN/admin/devices`
- If using the Nginx subpath in `nginx-location.conf`: `https://YOUR_DOMAIN/maistatus/admin/devices`

The proxy must set `X-Forwarded-Proto $scheme` and the public route must use HTTPS with a valid certificate. The application remains bound to `127.0.0.1:8090`; do not expose port 8090 to the public internet.

On the page, enter `STATUS_ADMIN_TOKEN`. The server validates it and issues a signed, HttpOnly, Secure, SameSite=Strict session cookie for eight hours. The plaintext token is not written to browser storage or sent in the URL. The protected API is `GET /admin/api/devices`.

## Device detection

Recent Android clients send a locally generated stable device ID plus device name and platform. Every upload refreshes the device's last-seen time, and the worker sends a lightweight heartbeat using `POST /api/v1/devices/heartbeat`, authenticated with `STATUS_API_TOKEN`.

A device is marked “最近活跃” when it has sent a heartbeat or upload in the last 45 minutes. This means recently reachable, not a persistent TCP/WebSocket connection. Device history is retained in SQLite.

Old clients that do not send `device_id` remain compatible. Their events are shown separately in the dashboard's unrecognized-legacy-event counter instead of inventing a device identity or treating IP addresses as IDs.

## Upgrade

The existing SQLite database is migrated automatically at startup by adding the nullable `events.device_id` column and creating a `devices` table. Existing event rows are preserved. After updating code, run:

```bash
cd /opt/maistatus-server
docker compose up -d --build
docker compose logs --tail=100 maistatus-api
curl http://127.0.0.1:8090/healthz
```

No additional public port or new Python dependency is needed.
