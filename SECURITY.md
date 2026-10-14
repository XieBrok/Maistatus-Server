# Security notes

- Do not commit `.env` or the `data/` directory.
- Use an HTTPS URL from the Android app. The app itself rejects non-HTTPS URLs.
- Keep the API bound to loopback and put a TLS-terminating reverse proxy in front of it.
- Keep `STATUS_API_TOKEN` private. Requests are authenticated with a constant-time comparison.
- Only the minimal liveness endpoint is unauthenticated; all personal status and event endpoints require a Bearer Token.
- Request bodies are intentionally not logged.
- Location, music, and activity data are sensitive personal information. Limit server access and backups to trusted administrators.
