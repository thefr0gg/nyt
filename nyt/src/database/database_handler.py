import json
import sqlite3
from datetime import datetime
from pathlib import Path

from loguru import logger

from nyt.src.database.tables.channels_table import Channels
from nyt.src.database.tables.videos_table import Videos
from nyt.src.database.tables.watched_videos_table import WatchedVideos
from nyt.src.utils import date_in_gmt


class _ClosingConnection(sqlite3.Connection):
    """`with conn:` normally only commits/rolls back; this also closes, so handles aren't leaked."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


_SCHEMA = """\
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS channels (
    channel_uid                TEXT PRIMARY KEY NOT NULL,
    channel_handle             TEXT NOT NULL,
    channel_name               TEXT,
    video_starting_point_id    TEXT NOT NULL,
    watched_videos_uid         TEXT NOT NULL,
    channel_avatar_url_default TEXT NOT NULL DEFAULT '',
    channel_avatar_url_medium  TEXT NOT NULL DEFAULT '',
    channel_avatar_url_high    TEXT NOT NULL DEFAULT '',
    watch_delay_minutes        INTEGER,
    last_checked_at            TEXT,
    auto_download              INTEGER DEFAULT 1,
    added_at                   TEXT NOT NULL,
    channel_id                 TEXT NOT NULL DEFAULT '',
    max_height                 INTEGER NOT NULL DEFAULT 1080
);

CREATE TABLE IF NOT EXISTS videos (
    video_id       TEXT PRIMARY KEY NOT NULL,
    channel_handle TEXT NOT NULL,
    thumbnail_url  TEXT NOT NULL DEFAULT '',
    title          TEXT NOT NULL DEFAULT '',
    publish_date   TEXT NOT NULL,
    download_path  TEXT NOT NULL DEFAULT '',
    is_downloaded  INTEGER NOT NULL DEFAULT 0,
    size           INTEGER NOT NULL DEFAULT 0,
    is_watched     INTEGER NOT NULL DEFAULT 0,
    timestamp      TEXT,
    updated_at     TEXT,
    added_at       TEXT NOT NULL,
    variants       TEXT NOT NULL DEFAULT '[]',
    subtitles      TEXT NOT NULL DEFAULT '[]',
    chapters       TEXT NOT NULL DEFAULT '[]',
    pinned         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS watched_videos (
    watched_videos_uid TEXT PRIMARY KEY NOT NULL,
    watched_videos     TEXT NOT NULL DEFAULT '{"ids":[]}',
    updated_at         TEXT,
    created_at         TEXT NOT NULL
);
"""

# Versioned migrations tracked via PRAGMA user_version.
# New migrations must be appended with a strictly increasing version number.
_MIGRATIONS: list[tuple[int, str]] = [
    (1, "ALTER TABLE channels ADD COLUMN channel_name TEXT"),
    (2, "ALTER TABLE channels ADD COLUMN watch_delay_minutes INTEGER"),
    (3, "ALTER TABLE channels ADD COLUMN last_checked_at TEXT"),
    (4, "ALTER TABLE channels ADD COLUMN auto_download INTEGER DEFAULT 1"),
    (5, "ALTER TABLE videos ADD COLUMN variants TEXT NOT NULL DEFAULT '[]'"),
    (6, "ALTER TABLE videos ADD COLUMN subtitles TEXT NOT NULL DEFAULT '[]'"),
    (7, "ALTER TABLE videos ADD COLUMN chapters TEXT NOT NULL DEFAULT '[]'"),
    (8, "ALTER TABLE channels ADD COLUMN channel_id TEXT NOT NULL DEFAULT ''"),
    (9, "ALTER TABLE channels ADD COLUMN max_height INTEGER NOT NULL DEFAULT 1080"),
    (10, "ALTER TABLE videos ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0"),
]

_SCHEMA_VERSION = _MIGRATIONS[-1][0] if _MIGRATIONS else 0


def _dt(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _parse_dt(s) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s))
    except (ValueError, TypeError):
        return None


class DatabaseHandler:
    def __init__(self, database_path: str) -> None:
        self._path = str(database_path)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        # WAL is persistent once set (see _SCHEMA), so it isn't re-issued per connection.
        conn = sqlite3.connect(
            self._path, timeout=30, check_same_thread=False, factory=_ClosingConnection,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            is_fresh = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='videos'"
            ).fetchone() is None

            conn.executescript(_SCHEMA)

            if is_fresh:
                conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
                logger.info(f"Database initialised at schema v{_SCHEMA_VERSION} — {self._path}")
            else:
                current_version = conn.execute("PRAGMA user_version").fetchone()[0]
                pending = [(v, s) for v, s in _MIGRATIONS if v > current_version]
                if pending:
                    logger.info(f"Running {len(pending)} database migration(s) (v{current_version} → v{_SCHEMA_VERSION})")
                for version, sql in pending:
                    try:
                        conn.execute(sql)
                        logger.debug(f"Migration v{version} applied")
                    except Exception as exc:
                        logger.debug(f"Migration v{version} already present: {exc}")
                if pending:
                    conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

            conn.commit()

    # ── Row converters ────────────────────────────────────────────────────────

    @staticmethod
    def _to_channel(row: sqlite3.Row) -> Channels:
        ad = row["auto_download"]
        return Channels(
            channel_uid                = row["channel_uid"],
            channel_handle             = row["channel_handle"],
            channel_name               = row["channel_name"],
            video_starting_point_id    = row["video_starting_point_id"],
            watched_videos_uid         = row["watched_videos_uid"],
            channel_avatar_url_default = row["channel_avatar_url_default"] or "",
            channel_avatar_url_medium  = row["channel_avatar_url_medium"]  or "",
            channel_avatar_url_high    = row["channel_avatar_url_high"]    or "",
            watch_delay_minutes        = row["watch_delay_minutes"],
            last_checked_at            = _parse_dt(row["last_checked_at"]),
            auto_download              = bool(ad) if ad is not None else True,
            added_at                   = _parse_dt(row["added_at"]),
            channel_id                 = (row["channel_id"] if "channel_id" in row.keys() else "") or "",
            max_height                 = row["max_height"] if "max_height" in row.keys() and row["max_height"] is not None else 1080,
        )

    @staticmethod
    def _json_col(row: sqlite3.Row, col: str) -> list:
        keys = row.keys()
        raw  = row[col] if col in keys else "[]"
        try:
            return json.loads(raw or "[]")
        except (ValueError, TypeError):
            return []

    def _to_video(self, row: sqlite3.Row) -> Videos:
        return Videos(
            video_id       = row["video_id"],
            channel_handle = row["channel_handle"],
            thumbnail_url  = row["thumbnail_url"] or "",
            title          = row["title"] or "",
            publish_date   = _parse_dt(row["publish_date"]),
            download_path  = row["download_path"] or "",
            is_downloaded  = bool(row["is_downloaded"]),
            size           = row["size"] or 0,
            is_watched     = bool(row["is_watched"]),
            timestamp      = _parse_dt(row["timestamp"]),
            updated_at     = _parse_dt(row["updated_at"]),
            added_at       = _parse_dt(row["added_at"]),
            variants       = self._json_col(row, "variants"),
            subtitles      = self._json_col(row, "subtitles"),
            chapters       = self._json_col(row, "chapters"),
            pinned         = bool(row["pinned"]) if "pinned" in row.keys() else False,
        )

    @staticmethod
    def _to_watched(row: sqlite3.Row) -> WatchedVideos:
        wv = row["watched_videos"]
        if isinstance(wv, str):
            wv = json.loads(wv)
        return WatchedVideos(
            watched_videos_uid = row["watched_videos_uid"],
            watched_videos     = wv,
            updated_at         = _parse_dt(row["updated_at"]),
            created_at         = _parse_dt(row["created_at"]),
        )

    # ── Channels ──────────────────────────────────────────────────────────────

    def add_channel_to_channels(self, channel: Channels) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO channels (
                    channel_uid, channel_handle, channel_name,
                    video_starting_point_id, watched_videos_uid,
                    channel_avatar_url_default, channel_avatar_url_medium, channel_avatar_url_high,
                    watch_delay_minutes, last_checked_at, auto_download, added_at, channel_id, max_height
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    channel.channel_uid, channel.channel_handle, channel.channel_name,
                    channel.video_starting_point_id, channel.watched_videos_uid,
                    channel.channel_avatar_url_default or "",
                    channel.channel_avatar_url_medium  or "",
                    channel.channel_avatar_url_high    or "",
                    channel.watch_delay_minutes,
                    _dt(channel.last_checked_at),
                    int(channel.auto_download) if channel.auto_download is not None else 1,
                    _dt(channel.added_at),
                    channel.channel_id or "",
                    channel.max_height if channel.max_height is not None else 1080,
                ),
            )
            conn.execute(
                "INSERT INTO watched_videos (watched_videos_uid, watched_videos, created_at) VALUES (?, ?, ?)",
                (channel.watched_videos_uid, json.dumps({"ids": []}), _dt(date_in_gmt())),
            )
            conn.commit()

    def delete_channel_row(self, channel_handle: str) -> None:
        channel = self.get_channel_row(channel_handle=channel_handle)
        for video in self.get_channel_videos(channel_handle=channel_handle):
            if video.is_downloaded and video.download_path:
                try:
                    Path(video.download_path).unlink()
                    logger.info(f"Removed video file '{video.download_path}'")
                except FileNotFoundError:
                    pass

        with self._connect() as conn:
            conn.execute("DELETE FROM videos WHERE channel_handle = ?", (channel_handle,))
            if channel:
                conn.execute(
                    "DELETE FROM watched_videos WHERE watched_videos_uid = ?",
                    (channel.watched_videos_uid,),
                )
            conn.execute("DELETE FROM channels WHERE channel_handle = ?", (channel_handle,))
            conn.commit()

    def get_channels(self) -> list[Channels]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM channels").fetchall()
        return [self._to_channel(r) for r in rows]

    def get_channel_row(
        self,
        channel_handle: str | None = None,
        channel_uid: str | None = None,
    ) -> Channels | None:
        with self._connect() as conn:
            if channel_handle is not None:
                row = conn.execute(
                    "SELECT * FROM channels WHERE channel_handle = ?", (channel_handle,)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM channels WHERE channel_uid = ?", (channel_uid,)
                ).fetchone()
        return self._to_channel(row) if row else None

    def get_channel_videos(self, channel_handle: str) -> list[Videos]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM videos WHERE channel_handle = ?", (channel_handle,)
            ).fetchall()
        return [self._to_video(r) for r in rows]

    def update_video_starting_point_id(self, channel_handle: str, video_starting_point_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE channels SET video_starting_point_id = ? WHERE channel_handle = ?",
                (video_starting_point_id, channel_handle),
            )
            conn.commit()

    def update_channel_delay(self, channel_handle: str, delay_minutes: int | None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE channels SET watch_delay_minutes = ? WHERE channel_handle = ?",
                (delay_minutes, channel_handle),
            )
            conn.commit()

    def update_channel_max_height(self, channel_handle: str, max_height: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE channels SET max_height = ? WHERE channel_handle = ?",
                (max_height, channel_handle),
            )
            conn.commit()

    def update_channel_auto_download(self, channel_handle: str, auto_download: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE channels SET auto_download = ? WHERE channel_handle = ?",
                (int(auto_download), channel_handle),
            )
            conn.commit()

    def set_channel_id(self, channel_handle: str, channel_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE channels SET channel_id = ? WHERE channel_handle = ?",
                (channel_id, channel_handle),
            )
            conn.commit()

    def rename_channel_handle(self, old_handle: str, new_handle: str, channel_name: str | None = None) -> None:
        """Re-key a channel and its videos to a new handle in a single transaction."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE channels SET channel_handle = ?, channel_name = COALESCE(?, channel_name) "
                "WHERE channel_handle = ?",
                (new_handle, channel_name or None, old_handle),
            )
            conn.execute(
                "UPDATE videos SET channel_handle = ? WHERE channel_handle = ?",
                (new_handle, old_handle),
            )
            conn.commit()

    def update_channel_last_checked(self, channel_handle: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE channels SET last_checked_at = ? WHERE channel_handle = ?",
                (_dt(datetime.utcnow()), channel_handle),
            )
            conn.commit()

    def check_channel_tracked(self, channel_handle: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM channels WHERE channel_handle = ?", (channel_handle,)
            ).fetchone()
        return row is not None

    def count_channel_videos(self, channel_handle: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM videos WHERE channel_handle = ?", (channel_handle,)
            ).fetchone()
        return row[0] if row else 0

    # ── Videos ───────────────────────────────────────────────────────────────

    def add_video_to_videos(self, video: Videos) -> None:
        if self.get_video_from_videos(video_id=video.video_id) is not None:
            return
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO videos (
                    video_id, channel_handle, thumbnail_url, title,
                    publish_date, download_path, is_downloaded, size,
                    is_watched, timestamp, updated_at, added_at,
                    variants, subtitles, chapters, pinned
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    video.video_id, video.channel_handle,
                    video.thumbnail_url or "", video.title or "",
                    _dt(video.publish_date), video.download_path or "",
                    int(video.is_downloaded), video.size or 0,
                    int(video.is_watched), _dt(video.timestamp),
                    _dt(video.updated_at), _dt(video.added_at),
                    json.dumps(video.variants  if video.variants  is not None else []),
                    json.dumps(video.subtitles if video.subtitles is not None else []),
                    json.dumps(video.chapters  if video.chapters  is not None else []),
                    int(bool(video.pinned)),
                ),
            )
            conn.commit()

    def get_storage_used_bytes(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(size), 0) FROM videos WHERE is_downloaded = 1"
            ).fetchone()
        return int(row[0]) if row else 0

    def get_stale_downloaded_videos(self, older_than_days: int) -> list[Videos]:
        from datetime import timedelta
        cutoff = (datetime.utcnow() - timedelta(days=older_than_days)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM videos
                   WHERE is_watched = 1 AND is_downloaded = 1 AND pinned = 0
                   AND timestamp IS NOT NULL AND timestamp <= ?""",
                (cutoff,),
            ).fetchall()
        return [self._to_video(r) for r in rows]

    def get_videos_list(self) -> list[Videos]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM videos").fetchall()
        return [self._to_video(r) for r in rows]

    def get_video_from_videos(self, video_id: str) -> Videos | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM videos WHERE video_id = ?", (video_id,)
            ).fetchone()
        return self._to_video(row) if row else None

    def update_videos_values(self, video_id: str, values: dict) -> None:
        normalised = {}
        for k, v in values.items():
            if isinstance(v, bool):
                normalised[k] = int(v)
            elif isinstance(v, datetime):
                normalised[k] = _dt(v)
            elif isinstance(v, (list, dict)):
                normalised[k] = json.dumps(v)
            else:
                normalised[k] = v

        set_clause = ", ".join(f"{k} = ?" for k in normalised)
        params     = list(normalised.values()) + [video_id]
        with self._connect() as conn:
            conn.execute(f"UPDATE videos SET {set_clause} WHERE video_id = ?", params)
            conn.commit()

    def set_videos_watched(self, video_ids: list[str], watched: bool) -> int:
        """Mark many videos watched/unwatched in one transaction. Returns the number of rows changed."""
        changed = 0
        stamp   = _dt(date_in_gmt())
        with self._connect() as conn:
            for i in range(0, len(video_ids), 500):
                chunk = video_ids[i:i + 500]
                marks = ",".join("?" * len(chunk))
                cur = conn.execute(
                    f"UPDATE videos SET is_watched = ?, timestamp = ? WHERE video_id IN ({marks})",
                    [int(watched), stamp, *chunk],
                )
                changed += cur.rowcount
            conn.commit()
        return changed

    def delete_video_row(self, video_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM videos WHERE video_id = ?", (video_id,))
            conn.commit()

    # ── Watched videos ────────────────────────────────────────────────────────

    def get_watched_videos_row(
        self,
        watched_videos_uid: str | None = None,
        channel_handle: str | None = None,
    ) -> WatchedVideos | None:
        if watched_videos_uid is None:
            channel = self.get_channel_row(channel_handle=channel_handle)
            if channel is None:
                return None
            watched_videos_uid = channel.watched_videos_uid

        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM watched_videos WHERE watched_videos_uid = ?",
                (watched_videos_uid,),
            ).fetchone()
        return self._to_watched(row) if row else None

    def add_video_id_to_watched_videos(
        self,
        watched_videos_uid: str,
        video_id: str,
        watched_videos: dict,
    ) -> None:
        watched_videos["ids"].append(video_id)
        with self._connect() as conn:
            conn.execute(
                "UPDATE watched_videos SET watched_videos = ?, updated_at = ? WHERE watched_videos_uid = ?",
                (json.dumps(watched_videos), _dt(date_in_gmt()), watched_videos_uid),
            )
            conn.commit()

    def delete_watched_videos_row(self, watched_videos_uid: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM watched_videos WHERE watched_videos_uid = ?",
                (watched_videos_uid,),
            )
            conn.commit()
