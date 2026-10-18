# dailycheckin-plus

<p align="center">English ｜ <a href="README.md">简体中文</a></p>

**A web admin panel for [Sitoi/dailycheckin](https://github.com/Sitoi/dailycheckin). One `docker compose up -d`, then configure everything with a few clicks in the browser.**

[![build](https://github.com/BeacherZ/dailycheckin-plus/actions/workflows/build.yml/badge.svg)](https://github.com/BeacherZ/dailycheckin-plus/actions/workflows/build.yml)
[![ghcr](https://img.shields.io/badge/ghcr.io-dailycheckin--plus-2496ed?logo=docker&logoColor=white)](https://github.com/BeacherZ/dailycheckin-plus/pkgs/container/dailycheckin-plus)
![license](https://img.shields.io/badge/license-MIT-green)

The check-in engine is the upstream project unchanged (MIT, with the Bilibili API fix bundled in); **the web panel is what this project adds**.

![Panel screenshot](index.png)

*Card view: four sites per row, with a status summary in the top bar. The UI is in Chinese.*

## What it fixes

| Upstream | This project |
|---|---|
| Hand-edit `config.json`; one missing comma and nothing runs | Fill in a web form, hit save, and errors tell you exactly what is wrong |
| Cookies must be separated by `; ` (semicolon **plus space**) | Paste any format — normalized automatically into what the engine expects |
| Expired cookies go unnoticed until you dig through the logs | Cards show ✅ valid / ❌ invalid |
| No idea which fields a given site actually needs | Required fields are marked per site and highlighted when missing |
| Changing the schedule means editing files inside the container | Pick a schedule in the UI, saved and applied immediately |

## Quick start

Create a `docker-compose.yml` in any directory:

```yaml
services:
  dailycheckin:
    image: ghcr.io/beacherz/dailycheckin-plus:latest
    container_name: dailycheckin
    restart: unless-stopped
    ports:
      - 8080:8080
    volumes:
      - ./config:/dailycheckin/config
      - ./cron:/dailycheckin/cron
      - ./logs:/dailycheckin/logs
      - ./data:/dailycheckin/data
    environment:
      - TZ=Asia/Shanghai
```

```bash
docker compose up -d
```

Open `http://your-host:8080` — the first visit walks you through setting the panel account and password.

**Upgrading**: `docker compose pull && docker compose up -d`.
The "About" section at the bottom of the settings page shows the commit the running build was made from and compares it against the latest commit on GitHub `main` — new commits are flagged, and hovering shows the update command.

If you prefer not to use Compose, run the same image with `docker run` and mount the **four directories** below; on Windows replace `$PWD` with `${PWD}`; on Synology / QNAP paste the block above into a "Project" in Container Manager / Container Station.

### Where the data lives

| Directory | Contents |
|---|---|
| `config/` | `config.json` — sites and cookies (format identical to upstream) |
| `cron/` | Scheduled task definitions |
| `logs/` | Check-in logs |
| `data/` | Panel credentials, validation state, run history, config backups |

Deleting the container loses nothing; copy these four directories together to back everything up.

## The web panel

- **Site list**: 16 sites laid out as cards (four per row, switchable to a list view in the top-right), each with the site logo, a status color bar, and badges. Search filters by site name or `KEY` (shortcut `/`), with category filters and a "unconfigured only" toggle; sorting defaults to configured-first; density adapts as the window narrows; light/dark follows the system. Anyone who has logged in before goes **straight to the admin UI** instead of flashing a login screen.
- **Configuring a site**: click a card to open its config dialog, which includes a "how to get the cookie" explanation and the account form. The footer stays pinned with "＋ Add account / Check in now / Save". Form-based sites (AcFun, Aliyun Drive, …) show their fields directly; optional parameters for cookie-based sites are tucked into a collapsible section.
- **Paste cookies however you like**: semicolon strings copied from the browser, semicolon strings without spaces, one field per line, a whole DevTools table, a JSON export, a curl command with a `Cookie:` prefix — all accepted and normalized to `; `-separated form.
  > The upstream engine hardcodes `cookie.split("; ")` in almost every site module, so `;` without a space or newline-separated values are **silently mis-parsed without an error**. The panel inserts the separators for you.
- **Cookie validity**: three escalating levels — format check (instant on paste, names the missing required field) → online validation (queries the site's session endpoint, returns nickname / level / balance) → run validation (performs a real check-in). Results are stored on the card badge with the evidence noted, and **changing a cookie invalidates the previous verdict immediately**; when the network is unreachable it honestly reports "cannot determine" and never claims a cookie is invalid by mistake.
  > "Ran without errors" is not "valid": many upstream modules **return silently** on an invalid cookie (Bilibili's `main()` starts with `if is_login:` and simply skips). So a site judged ❌ by the API check stays ❌ even if the check-in run produced no error.
- **Running check-ins**: run a single site from its config dialog, or "Check in all" in the top-right. Output streams into the right-hand drawer (switchable between current task / history / logs).
- **Scheduled check-ins**: pick a preset or write your own cron expression in the settings page; saving writes it into the container's crontab. The panel only touches its own marked block, so your other cron jobs are untouched.
- **Push notifications**: every upstream channel is supported (PushPlus, WeCom, Telegram, Bark, DingTalk, Feishu, Gotify, ntfy, ServerChan, Qmsg) — fill in only the ones you use, one box per channel. **Exactly one message per check-in run**:

```text
【dailycheckin-plus 每日签到 · 2026-10-16】

「V2EX 论坛」
帐号信息: ABC
今日签到: 20261016 的每日登录奖励 13 铜币

————————————

「哔哩哔哩」
第 1 个账号: ✅✅✅✅✅

————————————

任务用时: 1 秒
项目地址: https://github.com/beacherz/dailycheckin-plus
```

> The sample output above is in Chinese, matching the upstream engine's message templates.

- **Settings page**: panel account and password / engine provenance (upstream repo, commit, local patch list) / raw `config.json` editing (syntax errors refuse to save; a timestamped backup is taken before every save, up to 30 kept and restorable in one click) / about.

## Supported sites

The list is **discovered automatically** from the engine — when upstream adds a site, it appears after a panel refresh with no code changes. Currently 16:

| Site | KEY | Credentials (required) |
|---|---|---|
| Bilibili | `BILIBILI` | Cookie `SESSDATA`, `bili_jct` |
| Baidu Tieba | `TIEBA` | Cookie `BDUSS` |
| Baidu Netdisk | `BAIDUWP` | Cookie `BDUSS` |
| iQIYI | `IQIYI` | Cookie `P00001` |
| Youdao Note | `YOUDAO` | Cookie `YNOTE_PERS` |
| V2EX | `V2EX` | Cookie `A2` |
| SMZDM | `SMZDM` | whole cookie block recommended |
| Aola Star | `AOLAXING` | Cookie `BT_LOGIN_tt_common` |
| Enshan Wireless Forum | `ENSHAN` | Cookie `TWcq_2132_auth` |
| fnNAS Club | `FNNASCLUB` | Cookie `pvRK_2132_auth` |
| Quanmin KGe | `KGQQ` | Cookie `uid`, `openid`, `openkey` |
| Aliyun Drive | `ALIYUN` | refresh_token |
| iMaotai | `IMAOTAI` | `token`, `userid`, `mobile`, etc. |
| AcFun | `ACFUN` | username / password |
| Mi Fit | `MIMOTION` | username / password |
| Baidu Site Submit | `BAIDU` | form fields |

New sites are **usable out of the box** (configurable, savable, checkable) with slightly reduced polish: no required-field enforcement, no online validation, and a fallback icon instead of a logo. To fill that in, add a metadata block to `panel/catalog.py` following the existing pattern.

**How to get a cookie**: log in to the target site → `F12` → **Application** → **Cookies** → select the domain → `Ctrl+A` `Ctrl+C` in the table. Or use the [Cookie-Editor](https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm) extension and paste the whole **Export → JSON** block.
A cookie is a password — **never share it**. It lives only in `config/config.json` on your own machine and is never uploaded anywhere.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `TZ` | `Asia/Shanghai` | Timezone, affects scheduled task times |
| `PANEL_PORT` | `8080` | Listen port inside the container (also update the Compose mapping if you change it) |
| `PANEL_TITLE` | `dailycheckin-plus` | Brand name for the browser tab and top bar |
| `PANEL_USERNAME` / `PANEL_PASSWORD` | empty | Set these to skip the first-run account setup; only effective before the panel is initialized |
| `PANEL_TOKEN` | empty | Optional script token for curl / automation (`X-Panel-Token` header or `?token=`). **Does not replace login** — do not expose it publicly |
| `PANEL_RUN_TIMEOUT` | `1800` | Timeout in seconds for a single check-in task |
| `PANEL_REPO` | `BeacherZ/dailycheckin-plus` | Target for links in "About" and for the update comparison; point it at your own fork |
| `PANEL_GH_TOKEN` | empty | Sent with GitHub API calls to avoid anonymous rate limits (60/hour/IP) |
| `DC_HOME` | `/dailycheckin` | Working directory, rarely changed |

Pure Python standard library, **zero framework dependencies**, runs on x86 / arm64 / armv7. Panel passwords are stored only as PBKDF2-derived values with a random salt, sessions are signed tickets (restarting the panel does not log you out), and failed logins are rate-limited per IP.

## Following upstream updates

The engine source is **vendored** in `dailycheckin/` (not installed from PyPI): builds are reproducible, compile offline, and never lose the patches — at the cost of syncing upstream explicitly.

```bash
python tools/sync_upstream.py --check     # check only, exit code 2 = updates available
python tools/sync_upstream.py             # sync to the ref recorded in UPSTREAM.json
python tools/sync_upstream.py --ref 25.12.9   # pin a tag / branch / commit
```

It downloads the upstream source, replaces `dailycheckin/`, **replays the local patches in `patches/`**, updates `UPSTREAM.json`, and reports which sites were added or removed. If a patch fails to apply the whole thing rolls back — never leaving a half-patched engine — so **do not copy the upstream directory over by hand**, that silently wipes the patches. You can also run **Actions → sync upstream → Run workflow** to sync and rebuild the image in one click.

**Syncing the repo does not change the engine in your container**: the container runs the image, so follow up with `docker compose pull && docker compose up -d`. To see which version you are running, check the build commit under "About" in the settings page.

Two local patches: `0001` fixes the Bilibili section API (the upstream endpoint went offline and caused an `AttributeError`; filed as [PR #592](https://github.com/Sitoi/dailycheckin/pull/592) — delete the patch once upstream merges it), and `0002` makes notifications exactly one message per run.

Offline self-tests (no network needed): `python tools/test_sync_upstream.py` (patch replay / conflict rollback), `tools/test_notice.py` (single notification), `tools/test_schedule.py` (crontab managed block), `tools/test_panel_static.py` (frontend contract).

## FAQ

**Can I expose it to the internet?**
① Set the panel account and password after logging in (login is required by default — do not disable it); ② put an HTTPS reverse proxy in front (Nginx / Caddy / Traefik) — session tickets are Bearer-style, so plain HTTP risks interception. Make sure the proxy forwards `X-Forwarded-Proto`; the panel uses it to add the `Secure` flag to cookies.

**What do I need to do to migrate from the official `sitoi/dailycheckin` image?**
Stop the old container first — this project also uses the container name `dailycheckin`, so leaving it running causes a name clash: `docker stop dailycheckin && docker rm dailycheckin`. Then copy your existing `config/config.json` into this project's `config/` and run `docker compose up -d`.

**Does "cannot determine" mean the cookie is broken?**
Not necessarily. Network failures, API changes, and risk-control blocks all produce it; the panel prefers uncertainty over a false "invalid". In that case click "Check in now": **an error definitely means a problem, but no error does not prove validity**.

## Local development

```bash
pip install -r requirements.txt    # only the engine needs these (requests / pycryptodome / rsa / urllib3)
python -m panel                    # listens on 0.0.0.0:8080 by default
```

Even if the engine fails to import (for example `requests` is missing), the panel still starts and shows a message instead of a blank page. To build the image from source, use `docker compose -f docker-compose.build.yml up -d --build`.

Layout: `panel/` the admin panel (stdlib only), `dailycheckin/` the engine (upstream source + `patches/`), `tools/` offline tools and self-tests, `docker/entrypoint.sh` the container entrypoint.

## Credits and license

The check-in engine comes from [Sitoi/dailycheckin](https://github.com/Sitoi/dailycheckin) by Sitoi, MIT License, copyright the original author. The web admin panel, Docker packaging, and deployment experience are additions in this project, also released under MIT.
