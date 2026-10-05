# Security & secrets

This repository is designed so that **no secret ever lives in the tree.**

## What is (and isn't) here

| Thing | In the repo? | Where it lives |
|---|---|---|
| Exchange API key/secret/passphrase | ❌ never | `~/.config/okx/credentials.json` **or** `OKX_API_KEY`/`OKX_SECRET`/`OKX_PASSPHRASE` env vars |
| Decision-model (Jev) API key | ❌ never | `~/.config/typesafe/credentials.json` **or** `TYPESAFE_API_KEY` env var |
| LLM relay key (desk commentary) | ❌ never | env var named by `TERMINAL_LLM_KEY_ENV` (default `TERMINAL_LLM_API_KEY`) |
| Trading telemetry / sample data | ✅ public, by design | `terminal/sample_data/` — no keys, only market + decision numbers |
| Strategy parameters | ✅ public, by design | constants at the top of `engine/okx_jev.py` |

The engine reads credentials at runtime from external files or the environment
and exits with a setup message if they're missing. The terminal generator
**never reads exchange credentials at all** — it only reads the engine's JSONL
log, public market endpoints, and (optionally) the LLM relay.

## Pre-publish audit we ran

Before publishing, every source file and the sample telemetry were scanned for:

- API keys / secrets / passphrases (`api_key`, `secret`, `passphrase`, `AK=`/`SK=`/`PP=`)
- Tokens (GitHub `gho_`/`ghp_`, OpenAI-style `sk-…`, JWTs `eyJ…`, `Bearer …`)
- Private keys (`BEGIN … PRIVATE KEY`)
- Personal paths (`/Users/<name>`), internal hosts, private repo URLs, cron/job IDs, wallet addresses

Run it yourself any time:

```bash
grep -rniE "api[_-]?key|secret|passphrase|BEGIN .*PRIVATE KEY|\bsk-[a-z0-9]{16,}|gh[pous]_[a-z0-9]|eyJ[a-z0-9_-]{20}" \
  --include=*.py --include=*.js --include=*.sh --include=*.html --include=*.json --include=*.md .
```

The only intended matches are documentation placeholders (`YOUR_TYPESAFE_KEY`,
`OKX_API_KEY`, etc.) and code that *names* a field. If you see a real value,
**rotate that key immediately** and remove it from history (`git filter-repo`).

## If you fork or deploy this

1. **Rotate and scope your own keys.** Exchange key: `read` + `trade` only,
   **no withdrawal**, and IP-whitelist it to your runner.
2. **Never commit** `credentials.json`, `data.js`, `dossiers/`, or any engine
   runtime state — the included `.gitignore` already excludes them.
3. **Prefer env vars over files** in CI/containers so nothing lands on disk.
4. The terminal is static — serve it over HTTPS, and don't put it behind auth
   that you then hardcode into the page.
5. The sample telemetry intentionally shows real fills and balances from a small
   account. If that's more than you want public, regenerate `sample_data/` from
   your own (or blank it).

## Reporting an issue

If you believe you've found exposed credentials or a security bug, please do not
open a public issue — contact the maintainer via the profile on the repo, and
rotate any affected key first.
