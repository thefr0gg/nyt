import os
import shutil
import sys
import uuid
import subprocess
import requests as _requests

from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from nyt import constant


# ── Date ──────────────────────────────────────────────────────────────────────

def date_in_gmt() -> datetime:
    return datetime.now(timezone.utc)


# ── UID ───────────────────────────────────────────────────────────────────────

def generate_uid() -> str:
    return uuid.uuid4().hex


# ── Range header ──────────────────────────────────────────────────────────────

# ── Desktop notifications ─────────────────────────────────────────────────────

def send_notification(app_name: str, summary_text: str, message: str, icon_path: str = "") -> None:
    try:
        if sys.platform == "win32":
            _notify_windows(summary_text, message)
        elif sys.platform == "darwin":
            _notify_macos(summary_text, message)
        else:
            _notify_linux(app_name, summary_text, message, icon_path)
    except Exception as exc:
        logger.warning(f"Desktop notification failed: {exc}")


def _notify_linux(app_name: str, title: str, message: str, icon_path: str) -> None:
    cmd = ["notify-send", "--app-name", app_name, title, message]
    if icon_path:
        cmd += ["--icon", icon_path]
    subprocess.run(cmd, check=True)


def _notify_macos(title: str, message: str) -> None:
    subprocess.run(
        ["osascript", "-e", f'display notification "{message}" with title "{title}"'],
        check=True,
    )


def _notify_windows(title: str, message: str) -> None:
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$n = New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon = [System.Drawing.SystemIcons]::Information;"
        f'$n.BalloonTipTitle = "{title}";'
        f'$n.BalloonTipText = "{message}";'
        "$n.Visible = $true;"
        "$n.ShowBalloonTip(4000);"
        "Start-Sleep -Milliseconds 4500;"
        "$n.Dispose()"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)


# ── Assets ────────────────────────────────────────────────────────────────────

_ASSET_URLS: dict[str, str] = {
    "nyt-high-resolution-logo.png": (
        f"https://raw.githubusercontent.com/{constant.AUTHOR}/nyt/main/assets/nyt-high-resolution-logo.png"
    ),
    "nyt-high-resolution-logo-white.png": (
        f"https://raw.githubusercontent.com/{constant.AUTHOR}/nyt/main/assets/nyt-high-resolution-logo-white.png"
    ),
    "nyt-high-resolution-logo-black.png": (
        f"https://raw.githubusercontent.com/{constant.AUTHOR}/nyt/main/assets/nyt-high-resolution-logo-black.png"
    ),
    "nyt-high-resolution-logo-transparent.png": (
        f"https://raw.githubusercontent.com/{constant.AUTHOR}/nyt/main/assets/nyt-high-resolution-logo-transparent.png"
    ),
}


def _assets_dir() -> Path:
    from nyt.src.config import ConfigManager  # late import — avoids circular dependency
    return Path(ConfigManager().load_config().ASSETS_PREFIX)


def check_assets() -> bool:
    d = _assets_dir()
    return all((d / name).exists() for name in _ASSET_URLS)


def download_assets() -> None:
    d = _assets_dir()
    d.mkdir(parents=True, exist_ok=True)
    for name, url in _ASSET_URLS.items():
        (d / name).write_bytes(_requests.get(url, timeout=30).content)


def create_assets_prefix() -> None:
    _assets_dir().mkdir(parents=True, exist_ok=True)


# ── Deno (JS runtime required by yt-dlp for reliable YouTube extraction) ───────

def _deno_dir() -> Path:
    from nyt.src.config import ConfigManager  # late import — avoids circular dependency
    return Path(ConfigManager().load_config().DENO_DIRECTORY)


def _deno_bin_path() -> Path:
    name = "deno.exe" if sys.platform == "win32" else "deno"
    return _deno_dir() / "bin" / name


def find_deno() -> str | None:
    """Return a usable deno executable path, checking PATH first, then our own install dir."""
    on_path = shutil.which("deno")
    if on_path:
        return on_path
    local = _deno_bin_path()
    return str(local) if local.exists() else None


def check_deno() -> bool:
    return find_deno() is not None


def install_deno() -> str | None:
    """
    Download and install deno into ~/.nyt/deno/ so yt-dlp can solve YouTube's
    JS signature challenges. Returns the executable path on success, else None.
    Safe to call repeatedly — no-ops if deno is already available.
    """
    existing = find_deno()
    if existing:
        return existing

    install_dir = _deno_dir()
    install_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"Installing deno (required by yt-dlp for YouTube extraction) into '{install_dir}' ...")

    try:
        if sys.platform == "win32":
            script = _requests.get(
                "https://deno.land/install.ps1", timeout=30
            ).text
            result = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                capture_output=True, text=True, timeout=180,
                env={**os.environ, "DENO_INSTALL": str(install_dir)},
            )
        else:
            script = _requests.get(
                "https://deno.land/install.sh", timeout=30
            ).text
            result = subprocess.run(
                ["sh", "-c", script],
                capture_output=True, text=True, timeout=180,
                env={**os.environ, "DENO_INSTALL": str(install_dir)},
            )

        if result.returncode != 0:
            logger.warning(
                "Automatic deno install failed — YouTube extraction may fail with "
                "'Sign in to confirm you're not a bot' or format errors until it's "
                f"installed manually (see https://docs.deno.com/runtime/getting_started/installation/). "
                f"Install output:\n{result.stderr or result.stdout}"
            )
            return None
    except Exception as exc:
        logger.warning(
            "Automatic deno install failed — YouTube extraction may fail with "
            "'Sign in to confirm you're not a bot' or format errors until it's "
            f"installed manually (see https://docs.deno.com/runtime/getting_started/installation/). "
            f"Error: {exc}"
        )
        return None

    found = find_deno()
    if found:
        logger.info(f"deno installed at '{found}'")
    else:
        logger.warning(
            "deno install script ran but the executable could not be located afterwards — "
            "YouTube extraction may be degraded until it's installed manually."
        )
    return found
