# Security Policy

WeQuota is a **local-first** tool. It does not operate a cloud service and does not
send chat content to WeQuota servers.

## Sensitive data

Never commit or upload:

- `config.json` (may contain SMTP credentials and access tokens)
- `data/wequota.db`, `data/backups/`, `data/logs/`
- decrypted WeChat databases or exported chat files
- real wxid values, contact names, avatars, or message screenshots

These are excluded by `.gitignore`. If you accidentally expose a credential,
revoke or rotate it immediately (for example, regenerate your SMTP client
authorization code and access token).

## Threat model

- The app binds to `127.0.0.1` by default.
- If `app.auth_token` is configured, API requests require a bearer token.
- WeUI / Vue render local data; no third-party analytics or telemetry are bundled.
- Real-time collection depends on an external `wechat-cli`; review and trust that
  tool separately.

## Reporting a vulnerability

Please open a private security advisory or contact the maintainers instead of
publishing sensitive details in a public issue. Include a minimal reproduction
and affected version, but never include real chat content or credentials.
