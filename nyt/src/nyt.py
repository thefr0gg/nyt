import json
import os
import subprocess
import tempfile
import threading
import time
import yt_dlp
import requests

from dataclasses import dataclass
from pathlib import Path
from loguru import logger
from datetime import datetime

from nyt import constant


class _YdlLogger:
    """Routes yt-dlp's own messages into loguru so nothing (progress bars included) is written to the console."""

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        logger.debug(f"yt-dlp: {msg}")

    def error(self, msg: str) -> None:
        logger.debug(f"yt-dlp: {msg}")


@dataclass
class _VideoEntry:
    video_id: str
    title:    str
from nyt.src.database.database_handler import DatabaseHandler
from nyt.src.database.tables.channels_table import Channels
from nyt.src.database.tables.videos_table import Videos
from nyt.src.utils import date_in_gmt, generate_uid, send_notification, find_deno
from nyt.src.config import ConfigManager


class NYT:
    YOUTUBE_BASE     = "https://www.youtube.com"
    DOWNLOAD_RETRIES = 3
    show_notification = True

    _deno_warned = False

    def __init__(self) -> None:
        self.config           = ConfigManager().load_config()
        self.database_handler = DatabaseHandler(database_path=self.config.DATABASE_PATH)

    # ── yt-dlp option builders ──────────────────────────────────────────────────

    def _ydl_common_opts(self) -> dict:
        """
        Base options shared by every yt-dlp call. Points yt-dlp at deno (needed to
        solve YouTube's "n" signature challenge) and fetches its solver script —
        without both, extraction fails with misleading errors like "Requested
        format is not available" or "Sign in to confirm you're not a bot", even
        for public videos with no real access restriction.
        """
        deno_path = find_deno()
        if not deno_path and not NYT._deno_warned:
            NYT._deno_warned = True
            logger.warning(
                "deno (JS runtime) not found — YouTube extraction may intermittently fail "
                "with 'Sign in to confirm you're not a bot' or 'Requested format is not "
                "available', even for normal public videos. nyt tries to auto-install deno "
                "on startup; if that failed, install it manually from "
                "https://docs.deno.com/runtime/getting_started/installation/ and restart nyt."
            )

        opts: dict = {
            "quiet":             True,
            "noprogress":        True,
            "logger":            _YdlLogger(),
            "remote_components": {"ejs:github"},
            "socket_timeout":    30,
            "retries":           3,
            "extractor_retries": 3,
        }
        if deno_path:
            opts["js_runtimes"] = {"deno": {"path": deno_path}}
        return opts

    DEFAULT_MAX_HEIGHT = 1080

    @staticmethod
    def _format_selector(max_height: int | None) -> str:
        """
        yt-dlp format string capped at max_height pixels (0/None = no cap). YouTube now mostly
        serves separate video/audio streams, so merge them; keep single-file fallbacks, and as a
        last resort take the smallest format rather than fail when nothing fits under the cap.
        """
        cap = f"[height<={int(max_height)}]" if max_height else ""
        return (
            f"bv*[ext=mp4]{cap}+ba[ext=m4a]/bv*{cap}+ba/b[ext=mp4]{cap}/b{cap}/wv*+wa/w"
        )

    def _ydl_base_opts(self, max_height: int | None = DEFAULT_MAX_HEIGHT) -> dict:
        return {
            **self._ydl_common_opts(),
            "format":              self._format_selector(max_height),
            "merge_output_format": "mp4",
        }

    @staticmethod
    def _explain_ydl_error(exc: Exception) -> str:
        """
        Translate a raw yt-dlp exception into a plain-language reason, so failures
        read as "YouTube blocked/restricted this" rather than looking like a nyt bug.
        """
        msg = str(exc).lower()

        if "sign in to confirm" in msg and "bot" in msg:
            return (
                "YouTube flagged this request as a bot and is demanding sign-in. "
                "This is usually caused by too many requests from this IP in a short "
                "window, or a missing/broken JS runtime (deno) needed to solve YouTube's "
                "challenge. It should clear up on its own after a cooldown; if it keeps "
                "happening, verify 'deno --version' works and consider raising the watch "
                "delay so fewer videos are checked back-to-back."
            )
        if "requested format is not available" in msg:
            return (
                "yt-dlp couldn't resolve any downloadable format for this video. This is "
                "almost always YouTube serving a degraded response because of bot-detection "
                "or rate-limiting (see the 'Sign in to confirm' case) rather than the video "
                "genuinely having no formats — a missing/broken deno install is the most "
                "common cause."
            )
        if "members-only" in msg or "join this channel" in msg or "level:" in msg:
            return (
                "This video is restricted to paying channel members. nyt has no way to "
                "access member-only content — this is expected and not an error to fix."
            )
        if "private video" in msg:
            return "This video is private and cannot be accessed."
        if "video unavailable" in msg:
            return "YouTube reports this video as unavailable (deleted, region-blocked, or removed by the uploader)."
        if "429" in msg or "too many requests" in msg:
            return (
                "YouTube is rate-limiting this IP (HTTP 429). This is usually transient — "
                "it should resolve after a cooldown. If it happens often, consider raising "
                "the watch delay so fewer requests are made back-to-back."
            )
        if "http error 404" in msg or "requested entity was not found" in msg:
            return (
                "YouTube couldn't find this channel or video (HTTP 404). For a channel, the "
                "handle was probably changed, or the channel was deleted or terminated. Remove it "
                "from tracking, or track it again under its new handle."
            )
        if "http error 403" in msg or "forbidden" in msg:
            return "YouTube returned 403 Forbidden — likely a temporary block or an outdated yt-dlp version."

        return str(exc)

    @staticmethod
    def _is_permanent_error(exc: Exception) -> bool:
        """True for errors that retrying can never fix (private, members-only, removed)."""
        msg = str(exc).lower()
        return any(k in msg for k in (
            "members-only", "join this channel", "private video", "video unavailable",
            "has been removed", "this video is not available",
            "http error 404", "requested entity was not found",
        ))

    def _with_retries(self, fn, what: str, attempts: int = 3, backoff: float = 5.0):
        """Run fn(), retrying transient yt-dlp failures with exponential backoff."""
        for attempt in range(1, attempts + 1):
            try:
                return fn()
            except Exception as exc:
                if attempt == attempts or self._is_permanent_error(exc):
                    raise
                wait = backoff * 2 ** (attempt - 1)
                logger.warning(
                    f"{what} failed (attempt {attempt}/{attempts}): {self._explain_ydl_error(exc)} "
                    f"Retrying in {wait:.0f}s…"
                )
                time.sleep(wait)

    @staticmethod
    def _extract_info(opts: dict, url: str) -> dict | None:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    # ── Public API ────────────────────────────────────────────────────────────

    def add_channel(self, channel_handle: str) -> int | None:
        if self.database_handler.check_channel_tracked(channel_handle):
            return constant.CHANNEL_ALREADY_TRACKED

        videos = self.get_channel_last_videos(channel_handle)
        video_starting_point_id = videos[-1].video_id

        # One API call (forHandle=) covers name + avatar — no second page scrape needed.
        info = self.search_channel(channel_handle) or {}

        channel = Channels(
            channel_uid                = generate_uid(),
            channel_handle             = channel_handle,
            channel_name               = info.get("channel_name", ""),
            video_starting_point_id    = video_starting_point_id,
            watched_videos_uid         = generate_uid(),
            channel_avatar_url_default = info.get("avatar_url_default", ""),
            channel_avatar_url_medium  = info.get("avatar_url_medium",  ""),
            channel_avatar_url_high    = info.get("avatar_url_high",    ""),
            added_at                   = date_in_gmt(),
            channel_id                 = info.get("channel_id", ""),
        )
        self.database_handler.add_channel_to_channels(channel=channel)
        display = info.get("channel_name") or channel_handle
        logger.info(f"Now tracking '{display}' (@{channel_handle}) — starting from {video_starting_point_id}")

        avatar_url = (
            channel.channel_avatar_url_high
            or channel.channel_avatar_url_medium
            or channel.channel_avatar_url_default
        )
        if avatar_url:
            self._download_avatar(channel_handle, avatar_url)

    def remove_channel(self, channel_handle: str) -> int | None:
        if not self.database_handler.check_channel_tracked(channel_handle):
            return constant.CHANNEL_NOT_TRACKED
        self.database_handler.delete_channel_row(channel_handle=channel_handle)
        logger.info(f"Stopped tracking '@{channel_handle}' — videos and files removed")

    def check_channel_tracked(self, channel_handle: str) -> bool:
        return self.database_handler.check_channel_tracked(channel_handle)

    def watch(self) -> None:
        channels = self.database_handler.get_channels()

        if not channels:
            logger.info("No channels in the track list.")
            return

        logger.info(f"Tracking: {', '.join(c.channel_handle for c in channels)}")

        for channel in channels:
            try:
                self._process_channel(channel)
            except Exception:
                logger.exception(f"Unexpected error while processing '{channel.channel_handle}' — continuing with the next channel")

    def _process_channel(self, channel: Channels) -> None:
        if channel.last_checked_at is not None:
            delay = channel.watch_delay_minutes or self.config.WATCH_DELAY_MINUTES
            last  = channel.last_checked_at
            if hasattr(last, "tzinfo") and last.tzinfo is not None:
                last = last.replace(tzinfo=None)
            elapsed = (datetime.utcnow() - last).total_seconds()
            if elapsed < delay * 60:
                logger.debug(
                    f"Skipping '{channel.channel_handle}' — "
                    f"not due for {int((delay * 60 - elapsed) // 60)}m"
                )
                return

        logger.info(f"Checking '{channel.channel_handle}' for new uploads")

        try:
            meta, channel_last_videos = self._fetch_channel_listing(channel)
        except RuntimeError as exc:
            logger.warning(str(exc))
            self.database_handler.update_channel_last_checked(channel.channel_handle)
            return

        self._sync_channel_identity(channel, meta)

        logger.debug(f"Fetched {len(channel_last_videos)} videos")

        start_idx = next(
            (i for i, v in enumerate(channel_last_videos) if v.video_id == channel.video_starting_point_id),
            None,
        )

        if start_idx is None:
            logger.warning(
                f"Starting-point video not found for '{channel.channel_handle}'. "
                "Treating all fetched videos as new."
            )
            new_videos = channel_last_videos
        elif start_idx == len(channel_last_videos) - 1:
            logger.info(f"No new videos from '{channel.channel_handle}'.")
            self.database_handler.update_channel_last_checked(channel.channel_handle)
            return
        else:
            new_videos = channel_last_videos[start_idx + 1:]

        logger.info(f"{len(new_videos)} new video(s) from '{channel.channel_handle}'")

        if self.show_notification:
            icon = str(Path(self.config.ASSETS_PREFIX) / "nyt-high-resolution-logo.png")
            send_notification(
                app_name     = constant.PACKAGE,
                summary_text = "New YouTube Videos",
                message      = f"{len(new_videos)} video(s) uploaded by '{channel.channel_handle}'",
                icon_path    = icon,
            )

        auto_dl = channel.auto_download if channel.auto_download is not None else True

        # Only advance the starting point past videos that were handled, so a
        # transient failure (429, bot-check) is retried on the next cycle.
        new_start = channel.video_starting_point_id

        for video in new_videos:
            _transcode_args = None

            if self.database_handler.get_video_from_videos(video_id=video.video_id) is not None:
                new_start = video.video_id
                continue

            if auto_dl:
                logger.info(f"Downloading '{video.title}' to '{self.config.VIDEOS_PREFIX_DIRECTORY}'")

                downloaded = False
                permanent  = False
                for attempt in range(1, self.DOWNLOAD_RETRIES + 1):
                    try:
                        output_path, publish_date, title, thumbnail_url, size, height, subtitles, chapters = self.download_video(
                            video_id         = video.video_id,
                            prefix_directory = self.config.VIDEOS_PREFIX_DIRECTORY,
                            max_height       = channel.max_height,
                        )
                        downloaded = True
                        break
                    except Exception as error:
                        reason = self._explain_ydl_error(error)
                        if self._is_permanent_error(error):
                            permanent = True
                            logger.warning(f"Skipping '{video.title}' — {reason}")
                            break
                        if attempt == self.DOWNLOAD_RETRIES:
                            logger.warning(
                                f"Giving up on '{video.title}' for now after {self.DOWNLOAD_RETRIES} failed attempts — {reason}"
                            )
                        else:
                            logger.warning(
                                f"Download attempt {attempt}/{self.DOWNLOAD_RETRIES} failed for '{video.title}': "
                                f"{reason} Retrying…"
                            )

                if not downloaded:
                    if permanent:
                        new_start = video.video_id
                        continue
                    logger.warning(
                        f"Stopping '{channel.channel_handle}' for this cycle; '{video.title}' will be retried next check"
                    )
                    break

                if ConfigManager().load_config().TRANSCODING_ENABLED and self.wants_transcode(height):
                    _transcode_args = (video.video_id, output_path)

                record = Videos(
                    video_id       = video.video_id,
                    channel_handle = channel.channel_handle,
                    download_path  = output_path,
                    is_downloaded  = True,
                    is_watched     = False,
                    publish_date   = publish_date,
                    added_at       = date_in_gmt(),
                    thumbnail_url  = thumbnail_url,
                    title          = title,
                    size           = size,
                    variants       = [],
                    subtitles      = subtitles,
                    chapters       = chapters,
                )
            else:
                logger.info(f"Listing '{video.title}' (stream-only)")
                try:
                    title, thumbnail_url, publish_date = self.fetch_video_metadata(video.video_id)
                except Exception as exc:
                    logger.warning(
                        f"Could not fetch metadata for '{video.title}' ({video.video_id}): "
                        f"{self._explain_ydl_error(exc)}"
                    )
                    if self._is_permanent_error(exc):
                        new_start = video.video_id
                        continue
                    break

                record = Videos(
                    video_id       = video.video_id,
                    channel_handle = channel.channel_handle,
                    download_path  = "",
                    is_downloaded  = False,
                    is_watched     = False,
                    publish_date   = publish_date,
                    added_at       = date_in_gmt(),
                    thumbnail_url  = thumbnail_url,
                    title          = title,
                    size           = 0,
                )

            self.database_handler.add_video_to_videos(video=record)
            if record.is_downloaded:
                self._report_done(video.video_id, record.title)
            self._flag_video_watched(
                video_id          = video.video_id,
                watched_videos_uid = channel.watched_videos_uid,
            )
            new_start = video.video_id

            if _transcode_args:
                t = threading.Thread(
                    target=self._transcode_video,
                    args=_transcode_args,
                    daemon=True,
                    name=f"transcode-{_transcode_args[0]}",
                )
                t.start()

        if new_start != channel.video_starting_point_id:
            self.database_handler.update_video_starting_point_id(
                channel_handle          = channel.channel_handle,
                video_starting_point_id = new_start,
            )

        self.database_handler.update_channel_last_checked(channel.channel_handle)

    def search_channel(self, channel_handle: str) -> dict | None:
        url  = f"{self.YOUTUBE_BASE}/@{channel_handle}"
        opts = {**self._ydl_common_opts(), "extract_flat": True, "playlist_items": "0"}
        try:
            info = self._with_retries(
                lambda: self._extract_info(opts, url), f"Channel lookup for '@{channel_handle}'"
            )
        except Exception as exc:
            logger.warning(
                f"Channel lookup failed for '@{channel_handle}': {self._explain_ydl_error(exc)}"
            )
            return None

        if not info:
            return None

        thumbnails = info.get("thumbnails") or []
        avatar     = next((t["url"] for t in reversed(thumbnails) if t.get("url")), "")

        return {
            "channel_id":         info.get("channel_id", ""),
            "channel_name":       info.get("channel", ""),
            "channel_handle":     channel_handle,
            "avatar_url_default": avatar,
            "avatar_url_medium":  avatar,
            "avatar_url_high":    avatar,
            "subscriber_count":   str(info.get("channel_follower_count", 0)),
            "description":        info.get("description", ""),
        }

    def cleanup_watched_videos(self, days: int) -> int:
        videos = self.database_handler.get_stale_downloaded_videos(older_than_days=days)
        deleted = 0
        for video in videos:
            if video.download_path:
                try:
                    Path(video.download_path).unlink()
                    deleted += 1
                    logger.debug(f"Auto-deleted '{video.download_path}'")
                except FileNotFoundError:
                    pass
            self.database_handler.update_videos_values(
                video_id=video.video_id,
                values={"is_downloaded": False, "download_path": "", "size": 0},
            )
        if deleted:
            logger.info(f"Auto-deleted {deleted} watched video file(s) older than {days} day(s)")
        return deleted

    def get_channel_last_videos(self, channel_handle: str) -> list[_VideoEntry]:
        return self._fetch_channel_videos(channel_handle)[1]

    @staticmethod
    def _is_not_found(exc: BaseException | None) -> bool:
        msg = str(exc).lower()
        return "http error 404" in msg or "requested entity was not found" in msg

    def _fetch_channel_listing(self, channel: Channels) -> tuple[dict, list[_VideoEntry]]:
        """
        Fetch a tracked channel's recent uploads, surviving handle changes.
        Channels with a stored id are fetched by id (stable). Older rows without one fall back
        to the handle, and if it 404s (renamed) the id is recovered from the last-seen video.
        """
        try:
            return self._fetch_channel_videos(channel.channel_handle, channel.channel_id or "")
        except RuntimeError as exc:
            if channel.channel_id or not self._is_not_found(exc.__cause__):
                raise

        channel_id = self._channel_id_from_video(channel.video_starting_point_id)
        if not channel_id:
            raise RuntimeError(
                f"'@{channel.channel_handle}' no longer resolves and its new handle couldn't be "
                "determined automatically. If the channel changed its handle, remove it and track "
                "it again under the new one."
            )
        logger.info(f"'@{channel.channel_handle}' no longer resolves; recovered its channel id ({channel_id}) from its last video")
        return self._fetch_channel_videos(channel.channel_handle, channel_id)

    def _channel_id_from_video(self, video_id: str | None) -> str:
        if not video_id:
            return ""
        url = f"{self.YOUTUBE_BASE}/watch?v={video_id}"
        try:
            info = self._with_retries(
                lambda: self._extract_info({**self._ydl_common_opts(), "skip_download": True}, url),
                f"Channel id lookup via {video_id}",
            )
        except Exception as exc:
            logger.warning(f"Could not look up the channel of video {video_id}: {self._explain_ydl_error(exc)}")
            return ""
        return (info or {}).get("channel_id") or ""

    def _sync_channel_identity(self, channel: Channels, meta: dict) -> None:
        """Persist the channel id, and follow the channel if it changed its handle."""
        channel_id = meta.get("channel_id") or ""
        if channel_id and channel_id != (channel.channel_id or ""):
            self.database_handler.set_channel_id(channel.channel_handle, channel_id)
            channel.channel_id = channel_id

        new_handle = meta.get("handle") or ""
        old_handle = channel.channel_handle
        if not new_handle or new_handle.lower() == old_handle.lower():
            return
        if self.database_handler.check_channel_tracked(new_handle):
            logger.warning(f"'@{old_handle}' is now '@{new_handle}', which is already tracked — leaving both as they are")
            return

        self.database_handler.rename_channel_handle(old_handle, new_handle, channel_name=meta.get("name"))
        avatar = Path(self.config.AVATARS_DIRECTORY) / f"{old_handle}.jpg"
        if avatar.exists():
            os.replace(avatar, avatar.with_name(f"{new_handle}.jpg"))
        channel.channel_handle = new_handle
        logger.info(f"Channel '@{old_handle}' changed its handle to '@{new_handle}' — updated automatically")

    def _fetch_channel_videos(self, channel_handle: str, channel_id: str = "") -> tuple[dict, list[_VideoEntry]]:
        if channel_id:
            url = f"{self.YOUTUBE_BASE}/channel/{channel_id}/videos"
        else:
            url = f"{self.YOUTUBE_BASE}/@{channel_handle}/videos"
        opts = {
            **self._ydl_common_opts(),
            "extract_flat": True,
            "playlistend":  50,
        }
        try:
            info = self._with_retries(
                lambda: self._extract_info(opts, url), f"Video list for '@{channel_handle}'"
            )
        except Exception as exc:
            raise RuntimeError(
                f"Could not fetch video list for '@{channel_handle}': {self._explain_ydl_error(exc)}"
            ) from exc

        if not info:
            raise RuntimeError(f"Could not fetch video list for '@{channel_handle}': empty response from YouTube")

        entries = [e for e in (info.get("entries") or []) if e and e.get("id")]
        if not entries:
            raise RuntimeError(f"No videos found for '@{channel_handle}'")

        # yt-dlp returns newest-first; the watcher expects oldest-first
        entries.reverse()

        videos = [_VideoEntry(video_id=e["id"], title=e.get("title") or e["id"]) for e in entries]
        logger.debug(f"Fetched {len(videos)} videos for '@{channel_handle}'")
        meta = {
            "channel_id": info.get("channel_id") or info.get("id") or "",
            # uploader_id is "@handle" for channels that have one, otherwise the bare channel id
            "handle":     (info.get("uploader_id") or "")[1:] if (info.get("uploader_id") or "").startswith("@") else "",
            "name":       info.get("channel") or "",
        }
        return meta, videos

    @staticmethod
    def _report_done(video_id: str, title: str | None) -> None:
        """Mark a download finished. Called only once the library row exists, so the UI's refresh finds it."""
        from nyt.src.api.download_state import update_progress
        update_progress(video_id, {"title": title or video_id, "percent": 100, "status": "finished"})

    @staticmethod
    def _report_failed(video_id: str, title: str | None) -> None:
        from nyt.src.api.download_state import update_progress
        update_progress(video_id, {"title": title or video_id, "status": "error"})

    def download_video(
        self, video_id: str, prefix_directory: str,
        max_height: int | None = DEFAULT_MAX_HEIGHT, overwrite: bool = False,
    ) -> tuple:
        from nyt.src.api.download_state import update_progress

        state = {"title": video_id, "plan_total": 0, "streams": {}, "filepath": None}

        def _progress(d: dict) -> None:
            fname = d.get("filename") or ""
            if d.get("status") not in ("downloading", "finished") or fname.endswith(".vtt"):
                return
            done = d.get("downloaded_bytes") or 0
            if d["status"] == "finished":
                done = d.get("total_bytes") or done
            state["streams"][fname] = done

            # One combined bar across the video and audio streams, not one 0-100% per stream.
            total      = state["plan_total"] or d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = sum(state["streams"].values())
            speed      = d.get("speed") or 0
            update_progress(video_id, {
                "title":   state["title"],
                "percent": min(round(downloaded / total * 100, 1), 99) if total else 0,
                "speed":   speed,
                "eta":     int((total - downloaded) / speed) if total and speed else None,
                "status":  "downloading",
            })

        def _postprocess(d: dict) -> None:
            if d.get("postprocessor") == "Merger" and d.get("status") == "started":
                update_progress(video_id, {"title": state["title"], "percent": 99, "status": "merging"})
            elif d.get("postprocessor") == "MoveFiles" and d.get("status") == "finished":
                state["filepath"] = (d.get("info_dict") or {}).get("filepath")

        opts = {
            **self._ydl_base_opts(max_height),
            "outtmpl":             str(Path(prefix_directory) / "%(uploader)s" / "%(title)s.%(ext)s"),
            "writesubtitles":      True,
            "subtitleslangs":      ["all"],
            "subtitlesformat":     "vtt",
            "writeautomaticsub":   False,
            "progress_hooks":      [_progress],
            "postprocessor_hooks": [_postprocess],
            "overwrites":          overwrite,   # re-download over an existing file
        }
        video_url = f"{self.YOUTUBE_BASE}/watch?v={video_id}"

        _t0 = time.monotonic()
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                # Extract once, then download from that result (no second page/player request).
                info = ydl.extract_info(video_url, download=False)
                state["title"] = info.get("title") or video_id
                streams = info.get("requested_formats") or [info]
                state["plan_total"] = sum(f.get("filesize") or f.get("filesize_approx") or 0 for f in streams)
                update_progress(video_id, {"title": state["title"], "percent": 0, "status": "downloading"})

                ydl.process_info(info)

                file_name     = state["filepath"] or ydl.prepare_filename(info)
                title         = info.get("title")
                thumbnail_url = info.get("thumbnail")
                publish_date  = datetime.strptime(info["upload_date"], "%Y%m%d")
                size          = Path(file_name).stat().st_size
                height        = info.get("height") or 0
                elapsed       = time.monotonic() - _t0
                size_mb       = size / 1024 ** 2
                logger.info(f"Downloaded '{title}' - {size_mb:.1f} MB, {height}p, {elapsed:.0f}s")
        except Exception:
            self._report_failed(video_id, state["title"])
            raise

        # Collect subtitle VTT files written alongside the video
        subtitles = self._collect_subtitles(file_name)

        # Extract chapter metadata from yt-dlp info
        chapters = [
            {"start_time": c["start_time"], "end_time": c["end_time"], "title": c["title"]}
            for c in (info.get("chapters") or [])
        ]
        if chapters:
            logger.debug(f"Chapters: {len(chapters)} for '{title}'")

        return file_name, publish_date, title, thumbnail_url, size, height, subtitles, chapters

    def fetch_video_metadata(self, video_id: str) -> tuple[str, str, datetime]:
        url = f"{self.YOUTUBE_BASE}/watch?v={video_id}"
        info = self._with_retries(
            lambda: self._extract_info(self._ydl_common_opts(), url), f"Metadata fetch for {video_id}"
        )
        return (
            info.get("title", video_id),
            info.get("thumbnail", ""),
            datetime.strptime(info["upload_date"], "%Y%m%d"),
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _collect_subtitles(self, video_path: str) -> list[dict]:
        """Return a list of {lang, path, label} for VTT subtitle files next to the video."""
        p    = Path(video_path)
        stem = p.stem
        subs = []
        try:
            for entry in p.parent.iterdir():
                name = entry.name
                if (
                    name != p.name
                    and name.startswith(stem + ".")
                    and name.endswith(".vtt")
                ):
                    lang = name[len(stem) + 1 : -4]  # strip "{stem}." prefix and ".vtt"
                    if lang:
                        subs.append({"lang": lang, "path": str(entry), "label": lang})
        except OSError:
            pass
        if subs:
            logger.debug(f"Found {len(subs)} subtitle(s) for '{stem}': {[s['lang'] for s in subs]}")
        return subs

    def _download_avatar(self, channel_handle: str, url: str) -> None:
        dest = Path(self.config.AVATARS_DIRECTORY) / f"{channel_handle}.jpg"
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            res = requests.get(url, timeout=15)
            res.raise_for_status()
            dest.write_bytes(res.content)
            logger.debug(f"Cached avatar for '{channel_handle}' → {dest}")
        except Exception as exc:
            logger.warning(f"Could not cache avatar for '{channel_handle}': {exc}")

    def _flag_video_watched(self, video_id: str, watched_videos_uid: str) -> None:
        row = self.database_handler.get_watched_videos_row(watched_videos_uid=watched_videos_uid)
        if row is None or video_id in row.watched_videos["ids"]:
            return
        self.database_handler.add_video_id_to_watched_videos(
            video_id           = video_id,
            watched_videos_uid = watched_videos_uid,
            watched_videos     = row.watched_videos,
        )

    # ── Local quality variants (for bandwidth-friendly self-hosted streaming) ───

    TRANSCODE_HEIGHTS  = (720, 480, 360, 240)          # built automatically when transcoding is enabled
    ON_DEMAND_HEIGHTS  = (1080, 720, 480, 360, 240)    # can be requested per video from the UI

    _transcode_lock     = threading.Lock()   # one ffmpeg at a time
    _backfill_running   = False
    _transcode_checked: set[str] = set()     # videos already considered by the backfill this run

    @classmethod
    def wants_transcode(cls, source_height: int | None) -> bool:
        return bool(source_height) and any(h < source_height for h in cls.TRANSCODE_HEIGHTS)

    @staticmethod
    def _probe_video(path: str) -> tuple[float, int]:
        """Return (duration_seconds, height) of the file's first video stream."""
        out = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=height:format=duration",
                "-of", "json", path,
            ],
            capture_output=True, text=True, timeout=30, check=True,
        )
        data = json.loads(out.stdout)
        return float(data["format"]["duration"]), int(data["streams"][0]["height"])

    def start_transcode_backfill(self) -> None:
        """
        Queue local quality variants for already-downloaded videos that have none.
        Cheap to call every watcher cycle: it's a no-op while one is running, and
        each video is only considered once per process.
        """
        if NYT._backfill_running:
            return
        NYT._backfill_running = True
        threading.Thread(target=self._transcode_backfill, daemon=True, name="transcode-backfill").start()

    def _transcode_backfill(self) -> None:
        try:
            for video in self.database_handler.get_videos_list():
                if (
                    not video.is_downloaded or not video.download_path or video.variants
                    or video.video_id in NYT._transcode_checked
                    or not Path(video.download_path).exists()
                ):
                    continue
                NYT._transcode_checked.add(video.video_id)
                self._transcode_video(video.video_id, video.download_path)
        except Exception:
            logger.exception("Quality-variant backfill failed")
        finally:
            NYT._backfill_running = False

    def _transcode_video(self, video_id: str, source_path: str, heights: list[int] | None = None) -> None:
        from nyt.src.api.download_state import update_progress

        with NYT._transcode_lock:
            video = self.database_handler.get_video_from_videos(video_id)
            if not video or not video.is_downloaded or not Path(source_path).exists():
                return

            try:
                total_seconds, source_height = self._probe_video(source_path)
            except Exception as exc:
                logger.warning(f"ffprobe failed for '{video_id}': {exc}")
                return

            existing = {
                v.get("height") for v in (video.variants or [])
                if v.get("path") and Path(v["path"]).exists()
            }
            wanted  = heights or self.TRANSCODE_HEIGHTS
            targets = [h for h in wanted if h < source_height and h not in existing]
            if not targets:
                return

            logger.info(
                f"Transcoding '{video_id}' -> {', '.join(f'{h}p' for h in targets)} (source: {source_height}p)"
            )
            source = Path(source_path)
            for height in targets:
                self._transcode_one(video_id, source, height, total_seconds, update_progress)

    def _transcode_one(self, video_id: str, source: Path, height: int, total_seconds: float, update_progress) -> None:
        out_path     = source.parent / f"{source.stem}_{height}p.mp4"
        part_path    = source.parent / f"{source.stem}_{height}p.part.mp4"
        progress_key = f"{video_id}_{height}p"
        title        = f"{height}p — {source.stem[:40]}"

        def _progress(status: str, **extra) -> None:
            update_progress(progress_key, {"title": title, "status": status, "video_id": video_id, **extra})

        _progress("transcoding", percent=0)

        try:
            with tempfile.TemporaryFile(mode="w+") as err_file:
                proc = subprocess.Popen(
                    [
                        "ffmpeg", "-y", "-loglevel", "error",
                        "-i",        str(source),
                        "-vf",       f"scale=-2:{height}",
                        "-c:v",      "libx264",
                        "-crf",      "23",
                        "-preset",   "fast",
                        "-c:a",      "aac",
                        "-b:a",      "128k",
                        "-movflags", "+faststart",
                        "-progress", "pipe:1",
                        "-nostats",
                        str(part_path),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=err_file,
                    text=True,
                    # Keep the machine responsive while a long transcode runs.
                    creationflags=0x00004000 if os.name == "nt" else 0,   # BELOW_NORMAL_PRIORITY_CLASS
                    preexec_fn=(lambda: os.nice(10)) if os.name != "nt" else None,
                )

                for line in proc.stdout:
                    line = line.strip()
                    if line.startswith("out_time_us="):
                        try:
                            elapsed = int(line.split("=", 1)[1]) / 1_000_000
                            _progress("transcoding", percent=min(round(elapsed / total_seconds * 100, 1), 99))
                        except (ValueError, ZeroDivisionError):
                            pass

                proc.wait()
                err_file.seek(0)
                err_tail = err_file.read()[-500:].strip()

            if proc.returncode != 0 or not part_path.exists():
                _progress("error")
                logger.warning(f"FFmpeg failed for '{video_id}' at {height}p: {err_tail or 'no error output'}")
                return

            # Re-read the row now: the video may have been deleted or re-downloaded while we encoded.
            current = self.database_handler.get_video_from_videos(video_id)
            if not current or not current.is_downloaded:
                logger.info(f"'{video_id}' was removed during transcoding; discarding {height}p")
                return

            os.replace(part_path, out_path)
            variants = [v for v in (current.variants or []) if v.get("height") != height]
            variants.append({
                "height": height,
                "label":  f"{height}p",
                "path":   str(out_path),
                "size":   out_path.stat().st_size,
            })
            variants.sort(key=lambda v: v["height"], reverse=True)
            self.database_handler.update_videos_values(video_id=video_id, values={"variants": variants})
            _progress("finished", percent=100)
            logger.info(f"Transcoded '{video_id}' -> {height}p at '{out_path}'")

        except Exception as exc:
            _progress("error")
            logger.warning(f"Transcoding error for '{video_id}' at {height}p: {exc}")
        finally:
            part_path.unlink(missing_ok=True)
