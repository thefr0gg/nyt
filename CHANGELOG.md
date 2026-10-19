# Changelog

## v0.2.6 — 2026-10-19

### Added

- Per-channel download quality cap (default 1080p), set on the Channels page.
- Right-click menu on videos: open on YouTube, go to channel, mark all above as watched, pin to top (watch later), details, re-download at a chosen quality, show in folder, refresh metadata.
- Details dialog with on-demand local quality versions (1080p–240p) per video, independent of the global Transcoding setting.
- Channel filter dropdown on the Videos page, and themed dropdown menus across the app.
- Quality switcher in the player's control bar (works in fullscreen) that keeps your position and remembers your choice.
- Volume and mute are remembered globally across videos.
- Minimizable download progress panel with one combined bar and a merge step.
- Tracked channels follow handle changes automatically (permanent channel ID stored).

### Fixes

- Seeking and resume now work: the video endpoint serves proper HTTP range requests, and resume positions are saved while playing.
- Download progress no longer prints to the terminal; "finished" is reported only once the video is in the library.
- Failed downloads are retried on the next check instead of being skipped permanently; one failing channel no longer aborts the whole cycle.
- yt-dlp now merges separate video/audio streams, so downloads no longer fail with "Requested format is not available".
- The right-click menu was never visible (hidden by its own stylesheet).
- Update check: proper version comparison, no logging crash on Windows, and no self-update over a source checkout.

---

## v0.2.4 — 2026-09-06

### Changed

- Project moved to `thefr0gg/nyt` on GitHub — all repo URLs, the `nyt superuser`/update-check API, and asset/release links updated accordingly.
- Stream-only (not-yet-downloaded) YouTube videos now play via a native YouTube iframe embed instead of nyt proxying/merging YouTube's CDN streams itself. Quality selection for these videos is handled by YouTube's own player.
- The quality dropdown for downloaded, transcoded videos has a redesigned custom UI matching the app's visual style, replacing the native `<select>`.
- Video cards on the home grid now show a relative publish date (e.g. "3 days ago").

### Removed

- The "Quality selection" setting and its backend YouTube-format-resolution/merging code, made obsolete by the iframe change above.

### Fixes

- `yt-dlp` metadata-only fetches no longer fail with "Requested format is not available" — removed an unnecessary format constraint and wired in `deno` (auto-installed on first run) so YouTube's signature challenges resolve correctly instead of silently degrading to bot-check errors.

---

## v0.2.3 — 2026-05-01

### Fixes

- Serve `logo.svg` as favicon on all pages via `GET /favicon.ico`
- Fix favicon returning 404 — route must be registered before `api.mount("/static", ...)` to avoid Starlette shadowing it

---

## v0.2.2 — 2026-05-01

This release is a near-complete rewrite of the backend and a full replacement of the frontend. It is **not compatible** with v0.1.x — see the breaking changes section before upgrading.

### Breaking changes

- **`nyt api` removed** — replaced by `nyt serve`, which starts the web UI, REST API, and background watcher in a single command.
- **Frontend replaced** — the Next.js app (`nytweb/`) has been removed. The web UI is now a set of self-contained static HTML/CSS/JS pages served directly by the FastAPI backend. No Node.js or build step required.
- **Auth system replaced** — session tokens are now HS256 JWTs signed with the stored credentials. All existing sessions from v0.1.x are automatically invalidated on upgrade.
- **Database schema extended** — three new columns added to the `videos` table (`variants`, `subtitles`, `chapters`). Existing databases are migrated automatically on first start but cannot be used with v0.1.x afterwards.
- **`nyt.toml` gains new sections** — `[nyt.storage]`, `[nyt.cleanup]`, and `[nyt.player]` are written on first run or when settings are saved. Old config files are forward-compatible (missing sections use defaults) but the new format is not readable by v0.1.x.
- **`orjson` is now a required dependency** — add it with `pip install orjson` if upgrading manually without reinstalling.

---

### New features

#### Player
- **Creator subtitles** — yt-dlp downloads all creator-provided subtitle tracks (VTT format) alongside each video. The player injects them as native `<track>` elements with a language selector.
- **Chapter bar** — videos with chapter metadata get an interactive timeline bar below the player. Click any segment to jump to that chapter; the active chapter is highlighted in real time as the video plays.
- **Resume playback** — the player stores your position in `localStorage` and offers to pick up where you left off when you return to a video.

#### Downloading & storage
- **Quality variants via FFmpeg** — when transcoding is enabled, new downloads are transcoded to 720p, 480p, and 360p in background daemon threads. Progress is streamed to the UI in real time via SSE. Switch quality mid-playback from the player.
- **Storage cap** — set a maximum GB limit in Settings. The watcher stops auto-downloading when the cap is reached.
- **Storage usage meter** — Settings shows a live progress bar of disk usage, colour-coded by percentage (normal → orange at 70% → red at 90%).
- **Auto-delete watched videos** — configure a number of days after which watched video files are automatically removed. The video record stays in the library as "not downloaded".

#### Background watcher
- `nyt serve` now starts the watcher as a daemon thread alongside the API server — no need to run two processes.
- **Per-channel poll intervals** — set a custom check interval per channel from the Channels page. The global interval (set via `--delay` or Settings) is used as the fallback.

#### Authentication
- **JWT sessions** — login issues a 30-day HS256 JWT stored as an HttpOnly cookie. The token is refreshed on every page load and invalidated on logout or credential change.
- **`nyt superuser --disable`** — interactively prompts for the current username and password before clearing credentials, preventing accidental lockout.
- Auth can also be enabled and changed from the Settings page without restarting the server.

#### Web UI
- **Channels page** — search YouTube by handle, preview channel info (avatar, subscriber count, description), track/remove channels, set per-channel poll interval and stream-only mode, all without touching the CLI.
- **Settings page** — watcher interval, videos directory, storage limit, auto-delete, transcoding toggle, quality selection toggle, auth management, and version/update check in one place.
- **Sort and filter** on the home video grid — sort by date or channel, filter to unwatched only.
- **Mark watched** — clicking a video card marks it watched (fire-and-forget POST) before navigating to the player. Watched cards are visually dimmed with a checkmark overlay.
- **Watcher status dot** in the nav bar — green when the watcher ran within the last 10 minutes.
- **Download panel** — live SSE feed of active downloads and transcoding jobs, shown as an expandable panel in the nav.
- **Toast notifications** for user actions across all pages.

#### Docker
- Official `Dockerfile` and `docker-compose.yml` included. FFmpeg is pre-installed in the image. All data is stored in a named volume at `/root/.nyt`.

---

### Fixes

- Fixed session cookie never being set on login — FastAPI silently drops `set_cookie` calls on the injected `Response` parameter when the route returns a `Response` subclass directly. Cookies are now set on the returned response object.
- Fixed the watcher calling `sys.exit()` when no channels are tracked, killing the server process. It now returns early instead.
- Fixed TOML write errors on Windows — paths containing backslashes were written unescaped into double-quoted TOML strings, producing invalid escape sequences. All path values are now escaped via a `_qs()` helper before writing.

---

### Removed

- `nytweb/` — Next.js frontend removed entirely.
- `nyt api` CLI command — superseded by `nyt serve`.
- SQLAlchemy ORM — replaced with direct `sqlite3` calls for a simpler, dependency-free database layer.

---

## v0.1.2 — 2024

Initial public release. FastAPI backend, SQLAlchemy ORM, Next.js frontend, basic channel tracking and video downloading via yt-dlp.
