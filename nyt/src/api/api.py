import asyncio
import hashlib
import json
import os
import pathlib
import re
import secrets
import subprocess
import sys
import threading
import time
import requests as _requests
from loguru import logger

from fastapi import FastAPI, Request, Depends
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    StreamingResponse,
    RedirectResponse,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from nyt import constant
from nyt.src.api.classes import classes
from nyt.src.api import constant as api_constant
from nyt.src.api.auth.auth import auth_manager, require_auth
from nyt.src.api.watcher_state import watcher_state
from nyt.src.config import ConfigManager
from nyt.src.utils import date_in_gmt
from nyt.src.api.videos.videos import mime_for

STATIC_DIR = pathlib.Path(__file__).parent.parent.parent / "static"
ROOT = api_constant.ROOT_API_ROUTE

# ── App ───────────────────────────────────────────────────────────────────────

from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(app: FastAPI):
    config = ConfigManager().load_config()
    if config.AUTO_UPDATE_ENABLED:
        threading.Thread(target=_startup_update, daemon=True, name="nyt-startup-update").start()
    yield


api = FastAPI(lifespan=_lifespan)

api.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Favicon ───────────────────────────────────────────────────────────────────

@api.get("/favicon.ico", include_in_schema=False)
async def favicon_route():
    return FileResponse(STATIC_DIR / "logo.svg", media_type="image/svg+xml")


api.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _auth_redirect(request: Request) -> RedirectResponse | None:
    if not auth_manager.is_auth_enabled():
        return None
    token = request.cookies.get("nyt_session")
    if not token or not auth_manager.validate_session(token):
        return RedirectResponse(url="/login")
    return None


# ── Frontend HTML routes ──────────────────────────────────────────────────────

@api.get("/", include_in_schema=False)
async def home_route(request: Request):
    redir = _auth_redirect(request)
    return redir or FileResponse(STATIC_DIR / "index.html")


@api.get("/watch/{video_id}", include_in_schema=False)
async def watch_route(video_id: str, request: Request):
    redir = _auth_redirect(request)
    return redir or FileResponse(STATIC_DIR / "watch.html")


@api.get("/channels", include_in_schema=False)
async def channels_page_route(request: Request):
    redir = _auth_redirect(request)
    return redir or FileResponse(STATIC_DIR / "channels.html")


@api.get("/login", include_in_schema=False)
async def login_route():
    if not auth_manager.is_auth_enabled():
        return RedirectResponse(url="/")
    return FileResponse(STATIC_DIR / "login.html")


@api.get("/settings", include_in_schema=False)
async def settings_page_route(request: Request):
    redir = _auth_redirect(request)
    return redir or FileResponse(STATIC_DIR / "settings.html")


# ── Auth API routes ───────────────────────────────────────────────────────────

class LoginBody(BaseModel):
    username: str
    password: str


@api.post(f"{ROOT}/auth/login", response_class=JSONResponse)
async def auth_login_route(body: LoginBody):
    config = ConfigManager().load_config()
    if not config.ADMIN_USERNAME:
        token = auth_manager.create_session()
        resp  = JSONResponse({"ok": True})
        resp.set_cookie("nyt_session", token, httponly=True, samesite="strict", max_age=86400 * 30)
        return resp

    pw_hash = hashlib.pbkdf2_hmac(
        "sha256",
        body.password.encode(),
        config.ADMIN_SALT.encode(),
        200_000,
    ).hex()

    if body.username != config.ADMIN_USERNAME or pw_hash != config.ADMIN_PASSWORD_HASH:
        logger.warning(f"Failed login attempt for user '{body.username}'")
        return JSONResponse({"ok": False, "message": "Invalid credentials"}, status_code=401)

    logger.info(f"User '{body.username}' logged in")
    token = auth_manager.create_session()
    resp  = JSONResponse({"ok": True})
    resp.set_cookie("nyt_session", token, httponly=True, samesite="strict", max_age=86400 * 30)
    return resp


@api.post(f"{ROOT}/auth/refresh", response_class=JSONResponse)
async def auth_refresh_route(request: Request):
    if not auth_manager.is_auth_enabled():
        return JSONResponse({"ok": True, "refreshed": False})
    token = request.cookies.get("nyt_session")
    if not token or not auth_manager.validate_session(token):
        return JSONResponse({"ok": False}, status_code=401)
    new_token = auth_manager.create_session()
    resp = JSONResponse({"ok": True, "refreshed": True})
    resp.set_cookie("nyt_session", new_token, httponly=True, samesite="strict", max_age=86400 * 30)
    return resp


@api.post(f"{ROOT}/auth/logout", response_class=JSONResponse)
async def auth_logout_route(request: Request):
    token = request.cookies.get("nyt_session")
    if token:
        auth_manager.revoke_session(token)
        logger.info("User logged out")
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("nyt_session")
    return resp


@api.get(f"{ROOT}/auth/status", response_class=JSONResponse)
async def auth_status_route(request: Request):
    enabled = auth_manager.is_auth_enabled()
    token   = request.cookies.get("nyt_session")
    logged_in = bool(token and auth_manager.validate_session(token))
    return JSONResponse({"auth_enabled": enabled, "logged_in": logged_in})


# ── Watcher status ────────────────────────────────────────────────────────────

@api.get(f"{ROOT}/watcher/status", response_class=JSONResponse,
         dependencies=[Depends(require_auth)])
async def watcher_status_route():
    return JSONResponse({
        "status_code": 200,
        "watcher": {
            "running":  watcher_state.running,
            "last_run": watcher_state.last_run.isoformat() if watcher_state.last_run else None,
            "next_run": watcher_state.next_run.isoformat() if watcher_state.next_run else None,
            "error":    watcher_state.error,
        },
    })


# ── Download progress (SSE) ───────────────────────────────────────────────────

@api.get(f"{ROOT}/downloads/progress")
async def download_progress_sse(request: Request):
    from nyt.src.api.download_state import get_all_progress

    async def _stream():
        while True:
            if await request.is_disconnected():
                break
            yield f"data: {json.dumps(get_all_progress())}\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":       "keep-alive",
        },
    )


# ── API root ──────────────────────────────────────────────────────────────────

@api.get(ROOT, status_code=200, response_class=JSONResponse)
async def root_route():
    return JSONResponse([{"code": 200}])


# ── Channel routes ────────────────────────────────────────────────────────────

@api.get(f"{ROOT}/channels", status_code=200, response_class=JSONResponse)
async def root_channels_route():
    return JSONResponse({"code": 200, "message": "root channels route"})


@api.get(f"{ROOT}/channels/list", status_code=200, response_class=JSONResponse,
         dependencies=[Depends(require_auth)])
async def list_channels_route():
    return JSONResponse({
        "status_code": 200,
        "tracked_channels": classes.tracked_channels.list_tracked_channels(),
    })


@api.get(f"{ROOT}/channels/search", status_code=200, response_class=JSONResponse,
         dependencies=[Depends(require_auth)])
async def search_channels_route(handle: str):
    result = classes.tracked_channels.search_channel(handle)
    if result is None:
        return JSONResponse({"status_code": 404, "result": None}, status_code=404)
    return JSONResponse({"status_code": 200, "result": result})


@api.post(f"{ROOT}/channels/add", status_code=200, response_class=JSONResponse,
          dependencies=[Depends(require_auth)])
async def add_channels_route(channel_handle: str):
    return JSONResponse(
        classes.tracked_channels.add_channel_to_tracked_list(channel_handle=channel_handle)
    )


@api.put(f"{ROOT}/channels/{{channel_handle}}/delay", status_code=200, response_class=JSONResponse,
         dependencies=[Depends(require_auth)])
async def set_channel_delay_route(channel_handle: str, body: dict):
    delay = body.get("delay_minutes")
    classes.database_handler.update_channel_delay(
        channel_handle=channel_handle,
        delay_minutes=int(delay) if delay is not None else None,
    )
    return JSONResponse({"status_code": 200})


@api.put(f"{ROOT}/channels/{{channel_handle}}/auto-download", status_code=200, response_class=JSONResponse,
         dependencies=[Depends(require_auth)])
async def set_channel_auto_download_route(channel_handle: str, body: dict):
    classes.database_handler.update_channel_auto_download(
        channel_handle=channel_handle,
        auto_download=bool(body.get("auto_download", True)),
    )
    return JSONResponse({"status_code": 200})


@api.put(f"{ROOT}/channels/{{channel_handle}}/max-height", status_code=200, response_class=JSONResponse,
         dependencies=[Depends(require_auth)])
async def set_channel_max_height_route(channel_handle: str, body: dict):
    try:
        max_height = int(body.get("max_height", 1080))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "message": "max_height must be a number"}, status_code=422)
    if max_height != 0 and not 144 <= max_height <= 4320:
        return JSONResponse({"ok": False, "message": "max_height must be 0 (no cap) or 144-4320"}, status_code=422)
    if not classes.database_handler.check_channel_tracked(channel_handle):
        return JSONResponse({"ok": False, "message": "Channel is not tracked"}, status_code=404)
    classes.database_handler.update_channel_max_height(channel_handle=channel_handle, max_height=max_height)
    return JSONResponse({"status_code": 200, "max_height": max_height})


@api.delete(f"{ROOT}/channels/delete/{{channel_handle}}", status_code=200, response_class=JSONResponse,
            dependencies=[Depends(require_auth)])
async def delete_channels_route(channel_handle: str):
    return JSONResponse(
        classes.tracked_channels.remove_channel_from_tracked_list(channel_handle=channel_handle)
    )


@api.get(f"{ROOT}/channels/{{channel_handle}}/avatar", include_in_schema=False)
async def channel_avatar_route(channel_handle: str):
    config = ConfigManager().load_config()
    avatars_dir = pathlib.Path(config.AVATARS_DIRECTORY)
    avatars_dir.mkdir(parents=True, exist_ok=True)
    cached = avatars_dir / f"{channel_handle}.jpg"

    if not cached.exists():
        channel = classes.database_handler.get_channel_row(channel_handle=channel_handle)
        if channel is None:
            return JSONResponse({"detail": "not found"}, status_code=404)
        url = channel.channel_avatar_url_high or channel.channel_avatar_url_medium or channel.channel_avatar_url_default
        if not url:
            return JSONResponse({"detail": "no avatar url"}, status_code=404)
        try:
            res = _requests.get(url, timeout=15)
            res.raise_for_status()
            cached.write_bytes(res.content)
        except Exception:
            return JSONResponse({"detail": "fetch failed"}, status_code=502)

    return FileResponse(str(cached), media_type="image/jpeg")


# ── Video routes ──────────────────────────────────────────────────────────────

@api.get(f"{ROOT}/videos", status_code=200, response_class=JSONResponse)
async def videos_root_route():
    return JSONResponse({"status_code": 200, "message": "videos root route"})


@api.get(f"{ROOT}/videos/list/", status_code=200, response_class=JSONResponse,
         dependencies=[Depends(require_auth)])
def list_videos_route():
    return JSONResponse({
        "status_code": 200,
        "videos_info": classes.videos_handler.list_videos(),
    })


@api.post(f"{ROOT}/videos/{{video_id}}/watched", status_code=200, response_class=JSONResponse,
          dependencies=[Depends(require_auth)])
async def mark_video_watched_route(video_id: str):
    classes.database_handler.update_videos_values(
        video_id=video_id,
        values={"is_watched": True, "timestamp": date_in_gmt()},
    )
    logger.debug(f"Marked '{video_id}' as watched")
    return JSONResponse({"status_code": 200})


@api.post(f"{ROOT}/videos/{{video_id}}/unwatched", status_code=200, response_class=JSONResponse,
          dependencies=[Depends(require_auth)])
async def mark_video_unwatched_route(video_id: str):
    classes.database_handler.update_videos_values(
        video_id=video_id,
        values={"is_watched": False, "timestamp": date_in_gmt()},
    )
    logger.debug(f"Marked '{video_id}' as unwatched")
    return JSONResponse({"status_code": 200})


_active_downloads: set[str] = set()
_active_downloads_lock = threading.Lock()


def _valid_max_height(value) -> int | None:
    """0 = no cap, otherwise 144-4320. None if the value isn't usable."""
    try:
        h = int(value)
    except (TypeError, ValueError):
        return None
    return h if h == 0 or 144 <= h <= 4320 else None


def _video_file_paths(video) -> set[str]:
    paths = [video.download_path] + \
            [v.get("path", "") for v in (video.variants or [])] + \
            [s.get("path", "") for s in (video.subtitles or [])]
    return {p for p in paths if p}


def _queue_download(video, max_height: int, replace: bool) -> bool:
    """Start a background download (or re-download). False if one is already running for this video."""
    video_id = video.video_id
    with _active_downloads_lock:
        if video_id in _active_downloads:
            return False
        _active_downloads.add(video_id)

    config    = ConfigManager().load_config()
    old_paths = _video_file_paths(video) if replace else set()

    def _do_download():
        try:
            output_path, publish_date, title, thumbnail_url, size, height, subtitles, chapters = \
                classes.nyt.download_video(
                    video_id=video_id,
                    prefix_directory=config.VIDEOS_PREFIX_DIRECTORY,
                    max_height=max_height,
                    overwrite=replace,
                )
            classes.database_handler.update_videos_values(video_id=video_id, values={
                "download_path": output_path,
                "is_downloaded": True,
                "publish_date":  publish_date,
                "title":         title,
                "thumbnail_url": thumbnail_url,
                "size":          size,
                "subtitles":     subtitles,
                "chapters":      chapters,
                "variants":      [],
            })
            if replace:
                # Old file, its (now stale) quality versions and old subtitles, unless the new download reused the path.
                keep = {output_path} | {sub.get("path", "") for sub in subtitles}
                for path in old_paths - keep:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
            classes.nyt._report_done(video_id, title)
            if ConfigManager().load_config().TRANSCODING_ENABLED and classes.nyt.wants_transcode(height):
                classes.nyt._transcode_video(video_id, output_path)
        except Exception as exc:
            from nyt.src.api.download_state import update_progress
            update_progress(video_id, {"title": video.title or video_id, "status": "error"})
            logger.warning(f"Manual download failed for '{video_id}': {classes.nyt._explain_ydl_error(exc)}")
        finally:
            with _active_downloads_lock:
                _active_downloads.discard(video_id)

    threading.Thread(target=_do_download, daemon=True, name=f"download-{video_id}").start()
    return True


@api.post(f"{ROOT}/videos/{{video_id}}/download", status_code=202, response_class=JSONResponse,
          dependencies=[Depends(require_auth)])
async def download_video_route(video_id: str, body: dict | None = None):
    """Download a video. Optional body: {"max_height": 720, "replace": true} to (re)download at a chosen quality."""
    body  = body or {}
    video = classes.database_handler.get_video_from_videos(video_id=video_id)
    if not video:
        return JSONResponse({"status_code": 404, "message": "Not found"}, status_code=404)

    replace = bool(body.get("replace"))
    if video.is_downloaded and not replace:
        return JSONResponse({"status_code": 409, "message": "Already downloaded"}, status_code=409)

    if body.get("max_height") is not None:
        max_height = _valid_max_height(body["max_height"])
        if max_height is None:
            return JSONResponse({"status_code": 422, "message": "max_height must be 0 (best) or 144-4320"}, status_code=422)
    else:
        channel    = classes.database_handler.get_channel_row(channel_handle=video.channel_handle)
        max_height = channel.max_height if channel else classes.nyt.DEFAULT_MAX_HEIGHT

    if not _queue_download(video, max_height, replace):
        return JSONResponse({"status_code": 409, "message": "A download is already running for this video"}, status_code=409)

    quality = f"up to {max_height}p" if max_height else "best quality"
    logger.info(f"{'Re-download' if replace else 'Download'} queued for '{video.title or video_id}' ({quality})")
    return JSONResponse({"status_code": 202, "message": "Download started"})


def _delete_video_files(video) -> None:
    for path in [video.download_path] + \
                [v.get("path", "") for v in (video.variants or [])] + \
                [s.get("path", "") for s in (video.subtitles or [])]:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass


@api.delete(f"{ROOT}/videos/{{video_id}}/file", status_code=200, response_class=JSONResponse,
            dependencies=[Depends(require_auth)])
async def delete_video_file_route(video_id: str):
    video = classes.database_handler.get_video_from_videos(video_id=video_id)
    if not video:
        return JSONResponse({"status_code": 404, "message": "Not found"}, status_code=404)
    if not video.is_downloaded:
        return JSONResponse({"status_code": 409, "message": "Not downloaded"}, status_code=409)
    _delete_video_files(video)
    classes.database_handler.update_videos_values(video_id=video_id, values={
        "is_downloaded": False,
        "download_path": "",
        "size":          0,
        "variants":      [],
        "subtitles":     [],
    })
    logger.info(f"Deleted local file for '{video.title or video_id}'")
    return JSONResponse({"status_code": 200})


@api.delete(f"{ROOT}/videos/{{video_id}}", status_code=200, response_class=JSONResponse,
            dependencies=[Depends(require_auth)])
async def delete_video_route(video_id: str):
    video = classes.database_handler.get_video_from_videos(video_id=video_id)
    if not video:
        return JSONResponse({"status_code": 404, "message": "Not found"}, status_code=404)
    if video.is_downloaded:
        _delete_video_files(video)
    classes.database_handler.delete_video_row(video_id=video_id)
    logger.info(f"Removed video '{video.title or video_id}' from library")
    return JSONResponse({"status_code": 200})


@api.get(f"{ROOT}/videos/{{video_id}}/status", status_code=200, response_class=JSONResponse,
         dependencies=[Depends(require_auth)])
async def video_status_route(video_id: str):
    is_present = classes.database_handler.get_video_from_videos(video_id=video_id) is not None
    return JSONResponse({
        "status_code": 200,
        "video_status": {"is_present": is_present},
    })


@api.get(f"{ROOT}/videos/{{video_id}}/metadata", response_class=JSONResponse,
         dependencies=[Depends(require_auth)])
async def get_video_metadata_route(video_id: str):
    video = classes.database_handler.get_video_from_videos(video_id=video_id)
    if not video:
        return JSONResponse({"detail": "not found"}, status_code=404)
    return JSONResponse({
        "status_code": 200,
        "subtitles":   video.subtitles or [],
        "chapters":    video.chapters  or [],
    })


@api.get(f"{ROOT}/videos/{{video_id}}/subtitles/{{lang}}", include_in_schema=False)
async def get_subtitle_route(video_id: str, lang: str):
    import os
    video = classes.database_handler.get_video_from_videos(video_id=video_id)
    if not video:
        return JSONResponse({"detail": "not found"}, status_code=404)
    for sub in (video.subtitles or []):
        if sub.get("lang") == lang:
            path = sub.get("path", "")
            if path and os.path.exists(path):
                return FileResponse(path, media_type="text/vtt")
    return JSONResponse({"detail": "subtitle not found"}, status_code=404)


@api.get(f"{ROOT}/videos/{{video_id}}/formats", response_class=JSONResponse,
         dependencies=[Depends(require_auth)])
async def get_video_formats_route(video_id: str):
    video = classes.database_handler.get_video_from_videos(video_id=video_id)

    if not video or not video.is_downloaded or not video.download_path:
        return JSONResponse({"status_code": 404, "message": "Video is not downloaded"}, status_code=404)

    formats = classes.videos_handler.get_local_formats(video_id)
    return JSONResponse({"status_code": 200, "formats": formats, "is_local": True})


@api.get(f"{ROOT}/videos/{{video_id}}", response_class=FileResponse)
async def get_videos_route(video_id: str, format_id: str | None = None):
    path = classes.videos_handler.resolve_file(video_id, format_id)
    if not path:
        return JSONResponse({"detail": "not found"}, status_code=404)
    # FileResponse implements HTTP range requests (206 + Content-Range + Accept-Ranges). The browser needs
    # these to seek, so resume, scrubbing and switching quality all depend on them.
    return FileResponse(path, media_type=mime_for(path))


# ── Per-video actions (context menu) ──────────────────────────────────────────

class BulkWatchedBody(BaseModel):
    video_ids: list[str]
    watched: bool = True


@api.post(f"{ROOT}/videos/watched-bulk", response_class=JSONResponse, dependencies=[Depends(require_auth)])
async def bulk_watched_route(body: BulkWatchedBody):
    changed = classes.database_handler.set_videos_watched(body.video_ids, body.watched)
    return JSONResponse({"status_code": 200, "changed": changed})


@api.put(f"{ROOT}/videos/{{video_id}}/pin", response_class=JSONResponse, dependencies=[Depends(require_auth)])
async def pin_video_route(video_id: str, body: dict):
    if classes.database_handler.get_video_from_videos(video_id=video_id) is None:
        return JSONResponse({"status_code": 404, "message": "Not found"}, status_code=404)
    pinned = bool(body.get("pinned", True))
    classes.database_handler.update_videos_values(video_id=video_id, values={"pinned": pinned})
    return JSONResponse({"status_code": 200, "pinned": pinned})


@api.post(f"{ROOT}/videos/{{video_id}}/refresh-metadata", response_class=JSONResponse,
          dependencies=[Depends(require_auth)])
async def refresh_video_metadata_route(video_id: str):
    if classes.database_handler.get_video_from_videos(video_id=video_id) is None:
        return JSONResponse({"status_code": 404, "message": "Not found"}, status_code=404)
    try:
        title, thumbnail_url, publish_date = await asyncio.to_thread(classes.nyt.fetch_video_metadata, video_id)
    except Exception as exc:
        return JSONResponse({"status_code": 502, "message": classes.nyt._explain_ydl_error(exc)}, status_code=502)
    classes.database_handler.update_videos_values(video_id=video_id, values={
        "title": title, "thumbnail_url": thumbnail_url, "publish_date": publish_date,
    })
    return JSONResponse({"status_code": 200, "title": title})


@api.post(f"{ROOT}/videos/{{video_id}}/reveal", response_class=JSONResponse, dependencies=[Depends(require_auth)])
async def reveal_video_route(video_id: str, request: Request):
    """Open the video's file location in the OS file manager. Only meaningful (and only allowed) on the server machine."""
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        return JSONResponse({"status_code": 403, "message": "Only available from the machine running nyt"}, status_code=403)
    video = classes.database_handler.get_video_from_videos(video_id=video_id)
    if not video or not video.download_path or not os.path.exists(video.download_path):
        return JSONResponse({"status_code": 404, "message": "File not found"}, status_code=404)
    path = os.path.normpath(video.download_path)
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", f"/select,{path}"])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])
    except OSError as exc:
        return JSONResponse({"status_code": 500, "message": f"Could not open the file manager: {exc}"}, status_code=500)
    return JSONResponse({"status_code": 200})


def _probe_file(path: str) -> dict:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,codec_name:format=duration,bit_rate",
             "-of", "json", path],
            capture_output=True, text=True, timeout=30, check=True,
        )
        data = json.loads(out.stdout)
        st, fm = (data.get("streams") or [{}])[0], data.get("format") or {}
        return {
            "width":    st.get("width"),
            "height":   st.get("height"),
            "codec":    st.get("codec_name"),
            "duration": float(fm["duration"]) if fm.get("duration") else None,
            "bitrate":  int(fm["bit_rate"]) if fm.get("bit_rate") else None,
        }
    except Exception:
        return {}


@api.get(f"{ROOT}/videos/{{video_id}}/details", response_class=JSONResponse, dependencies=[Depends(require_auth)])
async def video_details_route(video_id: str):
    from nyt.src.api.download_state import get_all_progress

    video = classes.database_handler.get_video_from_videos(video_id=video_id)
    if not video:
        return JSONResponse({"status_code": 404, "message": "Not found"}, status_code=404)
    channel = classes.database_handler.get_channel_row(channel_handle=video.channel_handle)

    has_file = bool(video.is_downloaded and video.download_path and os.path.exists(video.download_path))
    probe    = await asyncio.to_thread(_probe_file, video.download_path) if has_file else {}

    variants = []
    for v in (video.variants or []):
        if v.get("path") and os.path.exists(v["path"]):
            variants.append({"height": v["height"], "label": v.get("label", f"{v['height']}p"), "size": v.get("size", 0)})
    have       = {v["height"] for v in variants}
    generating = sorted(
        (int(k.rsplit("_", 1)[1][:-1]) for k, d in get_all_progress().items()
         if k.startswith(f"{video_id}_") and d.get("status") == "transcoding"),
        reverse=True,
    )
    source_h = probe.get("height") or 0
    available = [h for h in classes.nyt.ON_DEMAND_HEIGHTS if source_h and h < source_h and h not in have and h not in generating]

    return JSONResponse({
        "status_code":        200,
        "video_id":           video.video_id,
        "title":              video.title,
        "channel_handle":     video.channel_handle,
        "channel_name":       channel.channel_name if channel else "",
        "channel_max_height": channel.max_height if channel else classes.nyt.DEFAULT_MAX_HEIGHT,
        "publish_date":       video.publish_date.isoformat() if video.publish_date else None,
        "added_at":           video.added_at.isoformat() if video.added_at else None,
        "is_downloaded":      bool(video.is_downloaded),
        "is_watched":         bool(video.is_watched),
        "is_pinned":          bool(video.pinned),
        "file_missing":       bool(video.is_downloaded and not has_file),
        "path":               video.download_path if has_file else "",
        "size":               video.size or 0,
        "probe":              probe,
        "variants":           variants,
        "generating":         generating,
        "available_heights":  available,
        "downloading":        video_id in _active_downloads,
    })


@api.post(f"{ROOT}/videos/{{video_id}}/variants", status_code=202, response_class=JSONResponse,
          dependencies=[Depends(require_auth)])
async def create_variant_route(video_id: str, body: dict):
    """Build one local quality version of a downloaded video, regardless of the global transcoding setting."""
    video = classes.database_handler.get_video_from_videos(video_id=video_id)
    if not video or not video.is_downloaded or not video.download_path or not os.path.exists(video.download_path):
        return JSONResponse({"status_code": 404, "message": "Video is not downloaded"}, status_code=404)
    try:
        height = int(body.get("height", 0))
    except (TypeError, ValueError):
        height = 0
    if height not in classes.nyt.ON_DEMAND_HEIGHTS:
        return JSONResponse({"status_code": 422, "message": "Unsupported quality"}, status_code=422)
    if any(v.get("height") == height and v.get("path") and os.path.exists(v["path"]) for v in (video.variants or [])):
        return JSONResponse({"status_code": 409, "message": f"{height}p already exists"}, status_code=409)

    try:
        _, source_h = await asyncio.to_thread(classes.nyt._probe_video, video.download_path)
    except Exception:
        return JSONResponse({"status_code": 500, "message": "Could not read the video file (is ffmpeg installed?)"}, status_code=500)
    if height >= source_h:
        return JSONResponse({"status_code": 422, "message": f"The original is only {source_h}p"}, status_code=422)

    threading.Thread(
        target=classes.nyt._transcode_video,
        args=(video_id, video.download_path, [height]),
        daemon=True, name=f"transcode-{video_id}-{height}",
    ).start()
    logger.info(f"Queued {height}p version of '{video.title or video_id}'")
    return JSONResponse({"status_code": 202, "message": f"Creating {height}p"})


@api.delete(f"{ROOT}/videos/{{video_id}}/variants/{{height}}", response_class=JSONResponse,
            dependencies=[Depends(require_auth)])
async def delete_variant_route(video_id: str, height: int):
    video = classes.database_handler.get_video_from_videos(video_id=video_id)
    if not video:
        return JSONResponse({"status_code": 404, "message": "Not found"}, status_code=404)
    keep, removed = [], False
    for v in (video.variants or []):
        if v.get("height") == height:
            removed = True
            try:
                os.remove(v.get("path", ""))
            except OSError:
                pass
        else:
            keep.append(v)
    if not removed:
        return JSONResponse({"status_code": 404, "message": f"No {height}p version"}, status_code=404)
    classes.database_handler.update_videos_values(video_id=video_id, values={"variants": keep})
    return JSONResponse({"status_code": 200})


# ── Settings routes ───────────────────────────────────────────────────────────

@api.get(f"{ROOT}/settings", response_class=JSONResponse,
         dependencies=[Depends(require_auth)])
async def get_settings_route():
    config     = ConfigManager().load_config()
    used_bytes = classes.database_handler.get_storage_used_bytes()
    return JSONResponse({
        "status_code": 200,
        "settings": {
            "watch_delay_minutes":       config.WATCH_DELAY_MINUTES,
            "videos_directory":          config.VIDEOS_PREFIX_DIRECTORY,
            "storage_limit_gb":          config.STORAGE_LIMIT_GB,
            "storage_used_bytes":        used_bytes,
            "auth_enabled":              bool(config.ADMIN_USERNAME),
            "admin_username":            config.ADMIN_USERNAME,
            "transcoding_enabled":       config.TRANSCODING_ENABLED,
            "auto_delete_watched_days":  config.AUTO_DELETE_WATCHED_DAYS,
            "auto_update_enabled":       config.AUTO_UPDATE_ENABLED,
        },
    })


class GeneralSettingsBody(BaseModel):
    watch_delay_minutes: int
    videos_directory: str
    storage_limit_gb: float | None = None
    transcoding_enabled: bool | None = None
    auto_delete_watched_days: int | None = None
    auto_update_enabled: bool | None = None


@api.put(f"{ROOT}/settings/general", response_class=JSONResponse,
         dependencies=[Depends(require_auth)])
async def update_general_settings_route(body: GeneralSettingsBody):
    if body.watch_delay_minutes < 1:
        return JSONResponse({"ok": False, "message": "Interval must be at least 1 minute"}, status_code=422)
    ConfigManager().save_settings(
        watch_delay_minutes=body.watch_delay_minutes,
        videos_directory=body.videos_directory.strip() or None,
        storage_limit_gb=max(0.0, body.storage_limit_gb) if body.storage_limit_gb is not None else None,
        transcoding_enabled=body.transcoding_enabled,
        auto_delete_watched_days=body.auto_delete_watched_days,
        auto_update_enabled=body.auto_update_enabled,
    )
    logger.info("Settings updated")
    return JSONResponse({"ok": True})


class AuthSettingsBody(BaseModel):
    username: str
    current_password: str = ""
    new_password: str


@api.put(f"{ROOT}/settings/auth", response_class=JSONResponse,
         dependencies=[Depends(require_auth)])
async def update_auth_settings_route(body: AuthSettingsBody):
    config = ConfigManager().load_config()
    if config.ADMIN_USERNAME:
        pw_hash = hashlib.pbkdf2_hmac(
            "sha256", body.current_password.encode(), config.ADMIN_SALT.encode(), 200_000
        ).hex()
        if pw_hash != config.ADMIN_PASSWORD_HASH:
            return JSONResponse({"ok": False, "message": "Current password is incorrect"}, status_code=401)
    if not body.username.strip():
        return JSONResponse({"ok": False, "message": "Username cannot be empty"}, status_code=422)
    if not body.new_password:
        return JSONResponse({"ok": False, "message": "Password cannot be empty"}, status_code=422)
    salt    = secrets.token_hex(16)
    pw_hash = hashlib.pbkdf2_hmac("sha256", body.new_password.encode(), salt.encode(), 200_000).hex()
    ConfigManager().save_auth(body.username.strip(), pw_hash, salt)
    logger.info(f"Auth credentials updated for user '{body.username.strip()}'")
    return JSONResponse({"ok": True})


class DisableAuthBody(BaseModel):
    current_password: str


@api.post(f"{ROOT}/settings/auth/disable", response_class=JSONResponse,
          dependencies=[Depends(require_auth)])
async def disable_auth_route(body: DisableAuthBody):
    config = ConfigManager().load_config()
    if not config.ADMIN_USERNAME:
        return JSONResponse({"ok": True})
    pw_hash = hashlib.pbkdf2_hmac(
        "sha256", body.current_password.encode(), config.ADMIN_SALT.encode(), 200_000
    ).hex()
    if pw_hash != config.ADMIN_PASSWORD_HASH:
        return JSONResponse({"ok": False, "message": "Password is incorrect"}, status_code=401)
    ConfigManager().save_auth("", "", "")
    logger.info("Authentication disabled")
    return JSONResponse({"ok": True})


# ── Version / update check ────────────────────────────────────────────────────

_GITHUB_RELEASES = f"https://api.github.com/repos/{constant.AUTHOR}/nyt/releases/latest"
_version_cache: dict = {"tag": None, "url": None, "fetched_at": None, "announced": None}
_VERSION_TTL = 3600  # re-check GitHub at most once per hour
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

_update_state: dict = {"running": False, "last_result": None, "message": None}
_update_lock = threading.Lock()


def _parse_version(tag: str | None) -> tuple[int, ...]:
    """'v0.2.4' -> (0, 2, 4). Anything unparseable becomes () and never counts as newer."""
    return tuple(int(n) for n in re.findall(r"\d+", (tag or "").split("-")[0]))


def _is_newer(latest: str | None, current: str) -> bool:
    lp = _parse_version(latest)
    return bool(lp) and lp > _parse_version(current)


def _is_source_checkout() -> bool:
    """True when running from a git checkout (e.g. `poetry run nyt`), where pip must not overwrite the code."""
    return (_REPO_ROOT / ".git").exists()


def _fetch_latest() -> tuple[str | None, str | None]:
    """Latest release (tag, url) from GitHub, cached for an hour. Blocking — call via a thread."""
    fetched_at = _version_cache["fetched_at"]
    if fetched_at is None or time.monotonic() - fetched_at > _VERSION_TTL:
        try:
            res = _requests.get(
                _GITHUB_RELEASES,
                timeout=5,
                headers={"Accept": "application/vnd.github+json"},
            )
            res.raise_for_status()
            body = res.json()
            _version_cache["tag"] = body.get("tag_name")
            _version_cache["url"] = body.get("html_url")
        except Exception as exc:
            logger.debug(f"Update check failed: {exc}")  # keep stale cache; retry on the next request
        else:
            _version_cache["fetched_at"] = time.monotonic()
    return _version_cache["tag"], _version_cache["url"]


def _run_update(tag: str | None) -> bool:
    """pip-install the given release tag (or the default branch if unknown). Records the outcome in _update_state."""
    if not _update_lock.acquire(blocking=False):
        return False
    _update_state.update(running=True, last_result=None, message=None)
    try:
        ref  = f"@{tag}" if tag else ""
        spec = f"git+https://github.com/{constant.AUTHOR}/{constant.PACKAGE}.git{ref}"
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", spec],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode == 0:
            _update_state.update(last_result="success", message=f"Updated to {tag or 'latest'}")
            logger.info(f"Update to {tag or 'latest'} completed - restart nyt to apply it")
        else:
            tail = (result.stderr or result.stdout or "").strip()[-400:]
            _update_state.update(last_result="error", message=tail or "pip exited with an error")
            logger.warning(f"Update failed:\n{result.stderr}")
    except Exception as exc:
        _update_state.update(last_result="error", message=str(exc))
        logger.warning(f"Update exception: {exc}")
    finally:
        _update_state["running"] = False
        _update_lock.release()
    return True


def _startup_update() -> None:
    if _is_source_checkout():
        logger.info("Startup auto-update skipped: running from a source checkout (use git pull)")
        return
    latest, _ = _fetch_latest()
    if not _is_newer(latest, constant.VERSION):
        return
    logger.info(f"Startup auto-update: {constant.VERSION} -> {latest}")
    _run_update(latest)


@api.get(f"{ROOT}/version", response_class=JSONResponse,
         dependencies=[Depends(require_auth)])
async def version_route():
    latest, release_url = await asyncio.to_thread(_fetch_latest)
    current   = constant.VERSION
    available = _is_newer(latest, current)
    if available and _version_cache["announced"] != latest:
        _version_cache["announced"] = latest  # log once per release, not on every poll
        logger.info(f"Update available: {current} -> {latest}")
    config = ConfigManager().load_config()
    return JSONResponse({
        "current_version":     current,
        "latest_version":      latest,
        "update_available":    available,
        "release_url":         release_url,
        "auto_update_enabled": config.AUTO_UPDATE_ENABLED,
        "can_self_update":     not _is_source_checkout(),
    })


@api.post(f"{ROOT}/update", response_class=JSONResponse,
          dependencies=[Depends(require_auth)])
async def trigger_update_route():
    if _is_source_checkout():
        return JSONResponse(
            {"ok": False, "message": "nyt is running from a source checkout - update it with git pull instead"},
            status_code=409,
        )
    if _update_state["running"]:
        return JSONResponse({"ok": False, "message": "Update already in progress"}, status_code=409)

    latest, _ = await asyncio.to_thread(_fetch_latest)
    if latest and not _is_newer(latest, constant.VERSION):
        return JSONResponse({"ok": False, "message": "Already up to date"}, status_code=409)

    threading.Thread(target=_run_update, args=(latest,), daemon=True, name="nyt-update").start()
    return JSONResponse({"ok": True, "message": "Update started"})


@api.get(f"{ROOT}/update/status", response_class=JSONResponse,
         dependencies=[Depends(require_auth)])
async def update_status_route():
    return JSONResponse(dict(_update_state))
