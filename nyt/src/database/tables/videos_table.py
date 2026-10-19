from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Videos:
    video_id:       str | None = None
    channel_handle: str | None = None
    thumbnail_url:  str | None = None
    title:          str | None = None
    publish_date:   datetime | None = None
    download_path:  str | None = None
    is_downloaded:  bool = False
    size:           int = 0
    is_watched:     bool = False
    timestamp:      datetime | None = None
    updated_at:     datetime | None = None
    added_at:       datetime | None = None
    variants:       list = field(default_factory=list)
    subtitles:      list = field(default_factory=list)
    chapters:       list = field(default_factory=list)
    pinned:         bool = False   # 'watch later': sorted first, exempt from auto-delete
