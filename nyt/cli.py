import sys
import time
import hashlib
import secrets
import argparse
import threading
import uvicorn
import logging
import logging.handlers

from datetime import datetime, timezone, timedelta
from loguru import logger

from nyt import constant

from nyt.src.nyt import NYT

from nyt.src.api.api import api as nyt_api
from nyt.src.logger.logger import InterceptHandler

# Models
from nyt.src.config import ConfigManager
from nyt.src.utils import check_assets, download_assets, create_assets_prefix, check_deno, install_deno

# Config manager
config_manager = ConfigManager()
config = config_manager.load_config()

# Custom intercept handler
INTERCEPT_HANDLER = InterceptHandler()

LOG_FORMAT = (
    "<bold><fg #f4845f> nyt </fg #f4845f></bold>"
    "<fg #a09880>{time:HH:mm:ss}</fg #a09880>"
    "  <level>{level:<8}</level>"
    "  {message}"
)


def cmd_version(args) -> None:
    logger.info(f"Version {constant.VERSION}")


def cmd_track(args) -> None:
    nyt = NYT()
    rs_code = nyt.add_channel(channel_handle=args.channel_handle)
    if rs_code == constant.CHANNEL_ALREADY_TRACKED:
        logger.info(f"Channel '{args.channel_handle}' is already being tracked")
        return
    logger.info(f"Channel '{args.channel_handle}' added to be tracked")


def cmd_remove(args) -> None:
    nyt = NYT()
    rs_code = nyt.remove_channel(channel_handle=args.channel_handle)
    if rs_code == constant.CHANNEL_NOT_TRACKED:
        logger.info(f"Channel '{args.channel_handle}' is not being tracked")
        return
    logger.info(f"Channel '{args.channel_handle}' removed from being tracked")


def cmd_watch(args) -> None:
    nyt = NYT()
    while True:
        try:
            nyt.watch()
        except KeyboardInterrupt:
            logger.info("Stopped.")
            sys.exit(0)
        except Exception as error:
            logger.error(f"Watch error: {error}")

        logger.info(f"Sleeping for {args.delay} minute(s)")
        time.sleep(args.delay * 60)


def _watcher_loop(delay_minutes: int) -> None:
    from nyt.src.api.classes import classes
    from nyt.src.api.watcher_state import watcher_state

    while True:
        watcher_state.running = True
        t0 = time.monotonic()
        logger.debug("Watcher cycle starting")
        try:
            classes.nyt.watch()
            cfg = ConfigManager().load_config()
            if cfg.AUTO_DELETE_WATCHED_DAYS > 0:
                classes.nyt.cleanup_watched_videos(cfg.AUTO_DELETE_WATCHED_DAYS)
            if cfg.TRANSCODING_ENABLED:
                classes.nyt.start_transcode_backfill()
            watcher_state.last_run = datetime.now(timezone.utc)
            watcher_state.error    = None
        except Exception as e:
            logger.error(f"Watcher error: {e}")
            watcher_state.error = str(e)

        elapsed = time.monotonic() - t0
        watcher_state.next_run = datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)
        watcher_state.running  = False
        logger.debug(f"Watcher cycle done in {elapsed:.1f}s — sleeping 60s (per-channel timing inside watch)")
        time.sleep(60)  # tight loop; per-channel timing handled inside watch()


def cmd_serve(args) -> None:
    cfg = config_manager.load_config()
    logger.info(f"nyt {constant.VERSION}")
    logger.info(f"Config:    {cfg.CONFIG_FILE_PATH}")
    logger.info(f"Database:  {cfg.DATABASE_PATH}")
    logger.info(f"Videos:    {cfg.VIDEOS_PREFIX_DIRECTORY}")
    logger.info(f"Auth:      {'enabled (user: ' + cfg.ADMIN_USERNAME + ')' if cfg.ADMIN_USERNAME else 'disabled'}")
    logger.info(f"Watcher:   {args.delay} min interval")
    logger.info(f"Serving on http://{args.host}:{args.port}")

    t = threading.Thread(
        target=_watcher_loop,
        args=(args.delay,),
        daemon=True,
        name="nyt-watcher",
    )
    t.start()
    uvicorn.run(
        nyt_api,
        host=args.host,
        port=args.port,
        log_config={
            "version": 1,
            "disable_existing_loggers": False,
            "loggers": {
                "uvicorn":        {"handlers": [], "level": "INFO", "propagate": True},
                "uvicorn.access": {"handlers": [], "level": "INFO", "propagate": True},
                "uvicorn.error":  {"handlers": [], "level": "INFO", "propagate": True},
            },
        },
    )


def cmd_superuser(args) -> None:
    if args.disable:
        import getpass
        cfg = config_manager.load_config()
        if not cfg.ADMIN_USERNAME:
            logger.info("Authentication is not currently enabled.")
            return
        print(f"Confirm identity for admin '{cfg.ADMIN_USERNAME}'")
        try:
            username = input("Username: ").strip()
            password = getpass.getpass("Password: ")
        except (KeyboardInterrupt, EOFError):
            print()
            logger.info("Aborted.")
            return
        pw_hash = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), cfg.ADMIN_SALT.encode(), 200_000
        ).hex()
        if username != cfg.ADMIN_USERNAME or pw_hash != cfg.ADMIN_PASSWORD_HASH:
            logger.error("Incorrect credentials. Authentication NOT disabled.")
            sys.exit(1)
        config_manager.save_auth("", "", "")
        logger.info("Authentication disabled. Restart the server to apply.")
        return

    if not args.username or not args.password:
        logger.error("--username and --password are required (or use --disable to turn off auth)")
        sys.exit(1)

    salt    = secrets.token_hex(16)
    pw_hash = hashlib.pbkdf2_hmac(
        "sha256",
        args.password.encode(),
        salt.encode(),
        200_000,
    ).hex()
    config_manager.save_auth(
        username=args.username,
        password_hash=pw_hash,
        salt=salt,
    )
    logger.info(f"Superuser '{args.username}' configured. Restart the server to apply.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nyt",
        description="No YouTube — self-hosted channel tracker and player",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    sub.add_parser("version", help="Show version")

    p_track = sub.add_parser("track", help="Add a YouTube channel to track")
    p_track.add_argument("--channel-handle", required=True, metavar="HANDLE",
                         help="Channel handle (without @)")

    p_remove = sub.add_parser("remove", help="Stop tracking a channel")
    p_remove.add_argument("--channel-handle", required=True, metavar="HANDLE",
                          help="Channel handle (without @)")

    p_watch = sub.add_parser("watch", help="Poll channels for new uploads (standalone loop)")
    p_watch.add_argument("--delay", type=int, default=config.WATCH_DELAY_MINUTES, metavar="MINUTES",
                         help=f"Delay between checks in minutes (default: {config.WATCH_DELAY_MINUTES})")

    p_serve = sub.add_parser("serve", help="Start the API server, web UI, and background watcher")
    p_serve.add_argument("--host", default=config.API_HOST, metavar="HOST",
                         help=f"Host to bind to (default: {config.API_HOST})")
    p_serve.add_argument("--port", type=int, default=config.API_PORT, metavar="PORT",
                         help=f"Port to bind to (default: {config.API_PORT})")
    p_serve.add_argument("--delay", type=int, default=config.WATCH_DELAY_MINUTES, metavar="MINUTES",
                         help=f"Global watcher interval (default: {config.WATCH_DELAY_MINUTES} min)")

    p_super = sub.add_parser("superuser", help="Configure admin credentials for the web UI")
    p_super.add_argument("--username", metavar="USERNAME", default=None)
    p_super.add_argument("--password", metavar="PASSWORD", default=None)
    p_super.add_argument("--disable", action="store_true",
                         help="Disable authentication (prompts for current credentials to confirm)")

    return parser


_DISPATCH = {
    "version":   cmd_version,
    "track":     cmd_track,
    "remove":    cmd_remove,
    "watch":     cmd_watch,
    "serve":     cmd_serve,
    "superuser": cmd_superuser,
}


def run() -> None:
    # Log messages contain non-ASCII (—, …); don't let a legacy console codepage crash logging.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    # Configure loguru
    logger.remove()
    logger.add(
        sys.stderr,
        format=LOG_FORMAT,
        colorize=True,
        level="DEBUG",
    )

    handler = logging.handlers.RotatingFileHandler(
        filename=config.API_LOGS_FILE_PATH,
        maxBytes=10 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",  # the locale default (cp1252 on Windows) can't encode arrows/dashes in messages
    )
    logger.add(handler, level="DEBUG", format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}")

    # Route all stdlib loggers (including uvicorn's) through loguru via the root logger.
    # uvicorn overwrites per-logger handlers when it starts, so we put the handler on
    # the root and pass log_config to uvicorn that keeps propagate=True with no own handlers.
    logging.root.handlers = [INTERCEPT_HANDLER]
    logging.root.setLevel(logging.DEBUG)

    # Bootstrap directories and assets
    create_assets_prefix()
    if not check_assets():
        download_assets()
    if not check_deno():
        install_deno()

    # Parse and dispatch
    parser = build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    _DISPATCH[args.command](args)
