# Contributing

Thanks for your interest in WeQuota.

## Development setup

```powershell
git clone <repo-url> WeQuota
cd WeQuota
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

Frontend:

```powershell
cd web
npm ci
npm run build
```

## Before opening a pull request

- `python -m pytest -q` passes.
- `cd web && npm run build` passes.
- No `config.json`, `data/`, logs, model files, or real chat content are included.
- Screenshots use synthetic demo data only.
- Keep changes focused and update docs where behavior changes.

## Project conventions

- Backend: FastAPI + SQLite; keep storage and quota logic testable.
- Frontend: Vue 3 + WeUI + GSAP; avoid adding a second UI framework.
- User-facing privacy: local-first by default, no telemetry.
- Do not add WeChat database decryption code to this repository.

## Reporting issues

Please include:

- OS and Python version;
- whether `wechat-cli` is installed and logged in;
- the exact command you ran;
- sanitized logs (remove wxid, SMTP credentials, tokens, and message text).

For security issues, see [SECURITY.md](SECURITY.md).
