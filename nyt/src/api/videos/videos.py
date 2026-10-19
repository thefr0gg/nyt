import os
import mimetypes

from nyt.src.database.database_handler import DatabaseHandler
from nyt.src.nyt import NYT

_MIME_FALLBACK = "video/mp4"


def mime_for(path: str) -> str:
    guessed, _ = mimetypes.guess_type(path)
    return guessed or _MIME_FALLBACK


class VideosHandler:
    def __init__(self, database_handler: DatabaseHandler, nyt: NYT) -> None:
        self.database_handler = database_handler
        self.nyt = nyt

    def get_video_mime(self, video_id: str) -> str:
        video = self.database_handler.get_video_from_videos(video_id=video_id)
        if video and video.is_downloaded and video.download_path:
            return mime_for(video.download_path)
        return _MIME_FALLBACK

    def get_local_formats(self, video_id: str) -> list[dict]:
        video = self.database_handler.get_video_from_videos(video_id=video_id)
        if not video or not video.is_downloaded:
            return []
        formats = [{"format_id": "original", "label": "Original", "height": 0, "ext": "mp4", "size": video.size or 0}]
        for v in (video.variants or []):
            formats.append({
                "format_id": f"{v['height']}p",
                "label":     v["label"],
                "height":    v["height"],
                "ext":       "mp4",
                "size":      v.get("size", 0),
            })
        return formats

    def resolve_file(self, video_id: str, format_id: str | None = None) -> str | None:
        """
        Path of the local file to serve for a downloaded video: the requested quality version if it
        exists, otherwise the original. None if the video isn't downloaded or the file is gone.
        (Stream-only videos play from a YouTube iframe on the frontend and never reach this.)
        """
        video = self.database_handler.get_video_from_videos(video_id=video_id)
        if not video or not video.is_downloaded or not video.download_path or not os.path.exists(video.download_path):
            return None

        if format_id and format_id != "original":
            for v in (video.variants or []):
                if f"{v['height']}p" == format_id:
                    vpath = v.get("path", "")
                    if vpath and os.path.exists(vpath):
                        return vpath
                    break

        return video.download_path

    def list_videos(self) -> list[dict]:
        videos = self.database_handler.get_videos_list()
        res = []
        for video in videos:
            channel_info = self.database_handler.get_channel_row(channel_handle=video.channel_handle)
            res.append({
                "video_id":                  video.video_id,
                "video_title":               video.title,
                "channel_handle":            video.channel_handle,
                "channel_name":              channel_info.channel_name if channel_info else "",
                "channel_avatar_url_default": channel_info.channel_avatar_url_default if channel_info else "",
                "publish_date":              video.publish_date.isoformat() if video.publish_date else None,
                "thumbnail_url":             video.thumbnail_url,
                "watch_url":                 f"/videos/{video.video_id}",
                "is_watched":                video.is_watched,
                "is_downloaded":             video.is_downloaded,
                "timestamp":                 video.timestamp.isoformat() if video.timestamp else None,
                "is_pinned":                 bool(video.pinned),
            })
        return res

    def check_video_exists(self, video_id: str) -> bool:
        return self.database_handler.get_video_from_videos(video_id=video_id) is not None
