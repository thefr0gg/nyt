from loguru import logger

from nyt.src.nyt import NYT
from nyt.src.database.database_handler import DatabaseHandler


class TrackedChannels:
    def __init__(self, nyt: NYT, database_handler: DatabaseHandler) -> None:
        self.nyt = nyt
        self.database_handler = database_handler

    def list_tracked_channels(self) -> list[dict]:
        channels = self.database_handler.get_channels()
        res: list[dict] = []
        for ch in channels:
            res.append({
                "channel_handle":           ch.channel_handle,
                "channel_name":             ch.channel_name or ch.channel_handle,
                "channel_avatar_url_default": ch.channel_avatar_url_default,
                "channel_avatar_url_medium":  ch.channel_avatar_url_medium,
                "channel_avatar_url_high":    ch.channel_avatar_url_high,
                "video_count":              self.database_handler.count_channel_videos(ch.channel_handle),
                "watch_delay_minutes":      ch.watch_delay_minutes,
                "auto_download":            ch.auto_download if ch.auto_download is not None else True,
                "max_height":               ch.max_height if ch.max_height is not None else 1080,
                "last_checked_at":          ch.last_checked_at.isoformat() if ch.last_checked_at else None,
                "added_at":                 ch.added_at.isoformat() if ch.added_at else None,
            })
        return res

    def search_channel(self, channel_handle: str) -> dict | None:
        result = self.nyt.search_channel(channel_handle)
        if result is None:
            return None
        result["already_tracked"] = self.database_handler.check_channel_tracked(channel_handle)
        return result

    def add_channel_to_tracked_list(self, channel_handle: str) -> dict:
        if self.database_handler.check_channel_tracked(channel_handle):
            logger.info(f"Channel '@{channel_handle}' is already tracked — skipping")
            return {
                "status_code": 200,
                "message": f"The channel '{channel_handle}' is already being tracked.",
            }
        logger.info(f"Adding channel '@{channel_handle}' — fetching info from YouTube")
        self.nyt.add_channel(channel_handle=channel_handle)
        return {
            "status_code": 200,
            "message": f"The channel '{channel_handle}' has been added to the tracked list.",
        }

    def remove_channel_from_tracked_list(self, channel_handle: str) -> dict:
        if not self.database_handler.check_channel_tracked(channel_handle):
            return {
                "status_code": 404,
                "message": f"The channel '{channel_handle}' is not being tracked.",
            }
        logger.info(f"Removing channel '@{channel_handle}' and its videos")
        self.database_handler.delete_channel_row(channel_handle=channel_handle)
        return {
            "status_code": 200,
            "message": f"The channel '{channel_handle}' has been removed from tracking.",
        }
