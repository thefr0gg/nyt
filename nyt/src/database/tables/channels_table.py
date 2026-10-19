from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Channels:
    channel_uid:                str | None = None
    channel_handle:             str | None = None
    channel_name:               str | None = None
    video_starting_point_id:    str | None = None
    watched_videos_uid:         str | None = None
    channel_avatar_url_default: str | None = None
    channel_avatar_url_medium:  str | None = None
    channel_avatar_url_high:    str | None = None
    watch_delay_minutes:        int | None = None
    last_checked_at:            datetime | None = None
    auto_download:              bool | None = True
    added_at:                   datetime | None = None
    max_height:                 int = 1080          # download quality cap in pixels; 0 = no cap
    channel_id:                 str | None = ""   # permanent YouTube id; survives handle changes
