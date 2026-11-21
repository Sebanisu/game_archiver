#!/usr/bin/env python3


from __future__ import annotations

import asyncio
import binascii
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
import tomllib
import webbrowser
from asyncio import to_thread
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum, StrEnum, auto
from hashlib import sha1
from pathlib import Path
from threading import Event
from typing import ClassVar, Literal, TypedDict
from urllib.parse import quote, urlsplit

import httpx
import vdf
from PIL import Image, UnidentifiedImageError
from rich.traceback import install
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Grid, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
    TextArea,
)
from textual_fspicker import FileOpen
from textual_image.widget import AutoImage

try:
    import tomli_w
except ImportError:
    tomli_w = None

install(show_locals=False)

# ============================================================
# CONFIG
# ============================================================
STEAM_DIR = Path.home() / ".steam" / "steam"
CONFIG_DIR = Path.home() / ".config" / "game_archiver"
CONFIG_FILE = CONFIG_DIR / "config.toml"
GAME_DATA = CONFIG_DIR / "game_data.json"
DATABASE_FILE = CONFIG_DIR / "game_archiver.db"
DATABASE_SCHEMA_VERSION = 10
RESCAN_INTERVAL_SEC = 1 * 60
SYNC_STATUS_INTERVAL_SEC = 30
MIN_GAME_SIZE = 0  # 1024 * 1024  # 1 MB
LOGGING_ENABLED = False  # DEBUG
LOGGING_FILE = "/tmp/game_archiver-debug.log"

CONFIG_DIR.mkdir(parents=True, exist_ok=True)

CACHE_DIR = Path.home() / ".cache" / "game_archiver"
ICON_CACHE = CACHE_DIR / "icons"
IMAGE_CACHE = CACHE_DIR / "steamgriddb" / "url"
THUMB_CACHE = CACHE_DIR / "steamgriddb" / "thumbs"
# STEAM_ART_CACHE = CACHE_DIR / "steam"

DATA_DIR = Path.home() / ".local" / "share" / "game_archiver"
STEAM_ICON_DIR = DATA_DIR / "icons"

ICON_CACHE.mkdir(parents=True, exist_ok=True)
IMAGE_CACHE.mkdir(parents=True, exist_ok=True)
THUMB_CACHE.mkdir(parents=True, exist_ok=True)
STEAM_ICON_DIR.mkdir(parents=True, exist_ok=True)
# STEAM_ART_CACHE.mkdir(parents=True, exist_ok=True)

STEAM_STORE_CDN = "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/"
STEAM_ICON_CDN = "https://shared.fastly.steamstatic.com/community_assets/images/apps/"
STEAM_CLIENTICON_CDN = (
    "https://cdn.cloudflare.steamstatic.com/steamcommunity/public/images/apps/"
)

PROTON = (
    Path.home()
    / ".local"
    / "share"
    / "Steam"
    / "steamapps"
    / "common"
    / "Proton - Experimental"
    / "proton"
)


@dataclass(frozen=True)
class GameLocation:
    name: str
    local_path: Path | None = None
    remote_path: Path | None = None
    move_to: str | None = None
    steam_shortcuts: bool = True

    def scan_path(self, remote_available: bool) -> Path | None:
        if self.remote_path is not None and remote_available:
            return self.remote_path

        return self.local_path

    def is_movable(self, remote_available: bool) -> bool:
        return self.move_to is not None and remote_available


# Game Storage Status:
#
# Shared:
#   - Game is synced locally to both the PC and Steam Deck.
#   - Launches locally using the installed game files.
#
# Archived:
#   - Game is stored on the server and is not synced locally.
#   - Still playable; assets must be streamed/accessed over the network
#     when launching remotely.
#
# Shared vs. Archived describes storage/sync status, NOT playability.
# Both statuses must remain available for launching games.
# POOL_DIR is the mount point for the remote game directories.


POOL_DIR = Path("/mnt/media/pool")
GAME_LOCATIONS = (
    GameLocation(
        name="Shared",
        local_path=Path.home() / "Games" / "shared",
        remote_path=POOL_DIR / "Games" / "shared",
        move_to="Archived",
    ),
    GameLocation(
        name="Archived",
        remote_path=POOL_DIR / "Games" / "archived",
        move_to="Shared",
    ),
    GameLocation(
        name="Heroic",
        local_path=Path.home() / "Games" / "Heroic",
        remote_path=POOL_DIR / "Games" / "Heroic",
    ),
    GameLocation(
        name="Steam",
        local_path=Path.home() / ".local" / "share" / "Steam" / "steamapps" / "common",
        remote_path=POOL_DIR / "Games" / "steam",
        steam_shortcuts=False,
    ),
)


class AppMode(StrEnum):
    MOVE = "move"
    LIBRARY = "library"


class ImageExtension(StrEnum):
    PNG = ".png"
    JPG = ".jpg"
    JPEG = ".jpeg"
    WEBP = ".webp"
    APNG = ".apng"
    ICO = ".ico"
    EXE = ".exe"


ARTWORK_EXTENSIONS = (
    ImageExtension.PNG,
    ImageExtension.JPG,
    ImageExtension.JPEG,
    ImageExtension.WEBP,
    ImageExtension.APNG,
    ImageExtension.ICO,
)
RENAME_TO_IMAGE_EXTENSIONS = {
    ImageExtension.APNG: ImageExtension.PNG,
    ImageExtension.WEBP: ImageExtension.PNG,
}


def correct_image_extension(path: Path) -> Path | None:
    if not path:
        return path
    ext = path.suffix.lower()
    if ext in RENAME_TO_IMAGE_EXTENSIONS:
        return path.with_suffix(RENAME_TO_IMAGE_EXTENSIONS[ext])
    return path


ICON_FALLBACK = Path("/usr/share/icons/hicolor/256x256/apps/steam" + ImageExtension.PNG)
BAD_LAUNCHER_NAMES = (
    "unins",
    "uninstall",
    "setup",
    "install",
    "config",
    "configure",
    "crash",
    "crashpad",
    "updater",
    "update",
    "dxsetup",
    "vc_redist",
    "redist",
    "xalia",
    "explorer",
    "zsync",
    "zsyncmake",
)


LIMIT_GB = 256
LIMIT_BYTES = LIMIT_GB * 1024 * 1024 * 1024

IGNORE_DIRS = {
    "shadercache",
    "compatdata",
    "cache",
    ".cache",
    "__pycache__",
    "drive_c",
    "windows",
    "Steamworks",
    "Proton",
    "DotNet",
    "_CommonRedist",
}

LAUNCHER_EXTS = (".sh", ".AppImage", ".x86_64", ".exe", ".swf", ".apk")


class ArtworkType(StrEnum):
    GRID_PORTRAIT = "grid_portrait"
    GRID_SQUARE = "grid_square"
    GRID_LANDSCAPE = "grid_landscape"
    HERO = "hero"
    LOGO = "logo"
    ICON = "icon"
    CANCEL = "cancel"


class SteamArtworkAuthor(TypedDict):
    name: str
    avatar: str | None
    steam64: str | None


class SteamArtwork(TypedDict):
    id: str
    language: str
    thumb: str
    url: str
    urls: list[str]
    author: SteamArtworkAuthor
    style: str
    source: str


suffixes = {
    ArtworkType.GRID_PORTRAIT: "p",
    ArtworkType.GRID_LANDSCAPE: "",
    ArtworkType.GRID_SQUARE: None,
    ArtworkType.HERO: "_hero",
    ArtworkType.LOGO: "_logo",
    ArtworkType.ICON: "_icon",
}

Filter = Literal["false", "true", "any"]


@dataclass(slots=True)
class SearchOptions:
    nsfw: Filter = "any"
    humor: Filter = "any"
    epilepsy: Filter = "any"

    def params(self) -> dict[str, str]:
        return {k: v for k, v in asdict(self).items() if v is not None}


# ============================================================
# SQLite
# ============================================================


def get_db() -> sqlite3.Connection:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    db = sqlite3.connect(DATABASE_FILE)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")

    return db


def create_database(db: sqlite3.Connection):
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            size INTEGER NOT NULL DEFAULT 0,
            mtime REAL NOT NULL DEFAULT 0,
            file_count INTEGER NOT NULL DEFAULT 0,
            last_played REAL NOT NULL DEFAULT 0,
            launcher TEXT,
            gamescope_enabled BOOLEAN NOT NULL DEFAULT 1
        )
        """
    )

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS steam_games (
            game_id INTEGER PRIMARY KEY,
            steam_key TEXT,
            steam_entry TEXT,
            in_steam INTEGER NOT NULL DEFAULT 0,
            steam_broken INTEGER NOT NULL DEFAULT 0,
            duplicate_steam INTEGER NOT NULL DEFAULT 0,

            FOREIGN KEY (game_id)
                REFERENCES games(id)
                ON DELETE CASCADE
        )
        """
    )

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS steamgriddb_games (
            game_id INTEGER PRIMARY KEY,
            sgdb_id INTEGER,
            name TEXT,
            search TEXT,
            game_data TEXT,
            steam_platform_data TEXT,
            cached REAL,

            FOREIGN KEY (game_id)
                REFERENCES games(id)
                ON DELETE CASCADE
        )
        """
    )

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS artwork (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            source TEXT NOT NULL,
            external_id TEXT,
            url TEXT,
            thumb_url TEXT,
            author TEXT,
            data TEXT NOT NULL
        )
        """
    )

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS steamgriddb_artwork (
            artwork_id INTEGER PRIMARY KEY,
            sgdb_game_id INTEGER NOT NULL,
            sgdb_art_id TEXT NOT NULL,

            FOREIGN KEY (artwork_id)
                REFERENCES artwork(id)
                ON DELETE CASCADE,

            FOREIGN KEY (sgdb_game_id)
                REFERENCES steamgriddb_games(game_id)
                ON DELETE CASCADE,

            UNIQUE (sgdb_game_id, sgdb_art_id)
        )
        """
    )

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS selected_artwork (
            game_id INTEGER NOT NULL,
            artwork_type TEXT NOT NULL,
            artwork_id INTEGER NOT NULL,
            downloaded_path TEXT,
            installed_path TEXT,

            PRIMARY KEY (game_id, artwork_type),

            FOREIGN KEY (game_id)
                REFERENCES games(id)
                ON DELETE CASCADE,

            FOREIGN KEY (artwork_id)
                REFERENCES artwork(id)
                ON DELETE CASCADE
        )
        """
    )

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS game_artwork (
            game_id INTEGER NOT NULL,
            artwork_id INTEGER NOT NULL,

            PRIMARY KEY (game_id, artwork_id),

            FOREIGN KEY (game_id)
                REFERENCES games(id)
                ON DELETE CASCADE,

            FOREIGN KEY (artwork_id)
                REFERENCES artwork(id)
                ON DELETE CASCADE
        )
        """
    )


def get_schema_version(db: sqlite3.Connection) -> int:
    row = db.execute("PRAGMA user_version").fetchone()

    return row[0]


def set_schema_version(
    db: sqlite3.Connection,
    version: int,
):
    db.execute(f"PRAGMA user_version = {version}")


def migrate_database(
    db: sqlite3.Connection,
    current_version: int,
):
    if current_version > DATABASE_SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema version {current_version} "
            f"is newer than supported version {DATABASE_SCHEMA_VERSION}"
        )

    if current_version < 1:
        create_database(db)
        set_schema_version(db, DATABASE_SCHEMA_VERSION)
        current_version = DATABASE_SCHEMA_VERSION

    if current_version < 2:
        migrate_to_v2(db)
        set_schema_version(db, 2)
        current_version = 2

    if current_version < 3:
        migrate_to_v3(db)
        set_schema_version(db, 3)
        current_version = 3

    if current_version < 4:
        migrate_to_v4(db)
        set_schema_version(db, 4)
        current_version = 4

    if current_version < 5:
        migrate_to_v5(db)
        set_schema_version(db, 5)
        current_version = 5

    if current_version < 6:
        migrate_to_v6(db)
        set_schema_version(db, 6)
        current_version = 6

    if current_version < 7:
        migrate_to_v7(db)
        set_schema_version(db, 7)
        current_version = 7

    if current_version < 8:
        migrate_to_v8(db)
        set_schema_version(db, 8)
        current_version = 8

    if current_version < 10:
        migrate_to_v9_and_v10(db)
        set_schema_version(db, 10)
        current_version = 10
    # Future migrations go here


def init_database():
    with get_db() as db:
        current_version = get_schema_version(db)

        migrate_database(
            db,
            current_version,
        )

        db.commit()


def migrate_to_v2(db: sqlite3.Connection):
    db.executescript(
        """
        CREATE TABLE steamgriddb_games_new (
            game_id INTEGER PRIMARY KEY,
            sgdb_id INTEGER,
            name TEXT,
            search TEXT,
            game_data TEXT,
            steam_platform_data TEXT,
            cached REAL,
            FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE
        );

        INSERT INTO steamgriddb_games_new (
            game_id,
            sgdb_id,
            name,
            search,
            game_data,
            steam_platform_data,
            cached
        )
        SELECT
            game_id,
            sgdb_id,
            name,
            search,
            game_data,
            steam_platform_data,
            cached
        FROM steamgriddb_games;

        DROP TABLE steamgriddb_games;

        ALTER TABLE steamgriddb_games_new
        RENAME TO steamgriddb_games;
        """
    )


def migrate_to_v3(db: sqlite3.Connection):
    db.execute(
        """
        ALTER TABLE games
        ADD COLUMN icon_manual INTEGER NOT NULL DEFAULT 0
        """
    )


def migrate_to_v4(db: sqlite3.Connection):
    db.execute(
        """
        CREATE TABLE artwork_v4 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            source TEXT NOT NULL,
            external_id TEXT,
            url TEXT,
            thumb_url TEXT,
            author TEXT,
            data TEXT NOT NULL
        )
        """
    )

    db.execute(
        """
        INSERT INTO artwork_v4 (
            id,
            type,
            source,
            external_id,
            url,
            thumb_url,
            author,
            data
        )
        SELECT
            id,
            type,
            'steamgriddb',
            sgdb_art_id,
            url,
            thumb_url,
            author,
            data
        FROM steamgriddb_artwork
        """
    )

    db.execute(
        """
        CREATE TABLE steamgriddb_artwork_v4 (
            artwork_id INTEGER PRIMARY KEY,
            sgdb_game_id INTEGER NOT NULL,
            sgdb_art_id TEXT NOT NULL,

            FOREIGN KEY (artwork_id)
                REFERENCES artwork_v4(id)
                ON DELETE CASCADE,

            FOREIGN KEY (sgdb_game_id)
                REFERENCES steamgriddb_games(game_id)
                ON DELETE CASCADE,

            UNIQUE (sgdb_game_id, sgdb_art_id)
        )
        """
    )

    db.execute(
        """
        INSERT INTO steamgriddb_artwork_v4 (
            artwork_id,
            sgdb_game_id,
            sgdb_art_id
        )
        SELECT
            id,
            sgdb_game_id,
            sgdb_art_id
        FROM steamgriddb_artwork
        """
    )

    db.execute(
        """
        CREATE TABLE selected_artwork_v4 (
            game_id INTEGER NOT NULL,
            artwork_id INTEGER NOT NULL,
            downloaded_path TEXT,

            PRIMARY KEY (game_id, artwork_id),

            FOREIGN KEY (game_id)
                REFERENCES games(id)
                ON DELETE CASCADE,

            FOREIGN KEY (artwork_id)
                REFERENCES artwork_v4(id)
                ON DELETE CASCADE
        )
        """
    )

    db.execute(
        """
        INSERT INTO selected_artwork_v4 (
            game_id,
            artwork_id
        )
        SELECT
            game_id,
            artwork_id
        FROM game_artwork
        WHERE selected = 1
        """
    )

    db.execute("DROP TABLE game_artwork")
    db.execute("DROP TABLE steamgriddb_artwork")

    db.execute(
        """
        ALTER TABLE artwork_v4
        RENAME TO artwork
        """
    )

    db.execute(
        """
        ALTER TABLE steamgriddb_artwork_v4
        RENAME TO steamgriddb_artwork
        """
    )

    db.execute(
        """
        ALTER TABLE selected_artwork_v4
        RENAME TO selected_artwork
        """
    )


def migrate_to_v5(db: sqlite3.Connection) -> None:
    db.execute("""
        CREATE TABLE selected_artwork_v5 (
            game_id INTEGER NOT NULL,
            artwork_type TEXT NOT NULL,
            artwork_id INTEGER NOT NULL,
            downloaded_path TEXT,
            PRIMARY KEY (game_id, artwork_type),
            FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE,
            FOREIGN KEY (artwork_id) REFERENCES artwork(id) ON DELETE CASCADE
        )
    """)

    db.execute("""
        INSERT INTO selected_artwork_v5 (
            game_id, artwork_type, artwork_id, downloaded_path
        )
        SELECT
            sa.game_id,
            a.type,
            sa.artwork_id,
            sa.downloaded_path
        FROM selected_artwork sa
        JOIN artwork a ON a.id = sa.artwork_id
        WHERE sa.artwork_id = (
            SELECT MAX(sa2.artwork_id)
            FROM selected_artwork sa2
            JOIN artwork a2 ON a2.id = sa2.artwork_id
            WHERE sa2.game_id = sa.game_id
              AND a2.type = a.type
        )
    """)

    db.execute("DROP TABLE selected_artwork")
    db.execute("ALTER TABLE selected_artwork_v5 RENAME TO selected_artwork")


def migrate_to_v6(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS game_artwork (
            game_id INTEGER NOT NULL,
            artwork_id INTEGER NOT NULL,

            PRIMARY KEY (game_id, artwork_id),

            FOREIGN KEY (game_id)
                REFERENCES games(id)
                ON DELETE CASCADE,

            FOREIGN KEY (artwork_id)
                REFERENCES artwork(id)
                ON DELETE CASCADE
        )
        """
    )

    db.execute(
        """
        INSERT OR IGNORE INTO game_artwork (
            game_id,
            artwork_id
        )
        SELECT DISTINCT
            game_id,
            artwork_id
        FROM selected_artwork

        UNION

        SELECT DISTINCT
            sg.game_id,
            sga.artwork_id
        FROM steamgriddb_artwork AS sga
        JOIN steamgriddb_games AS sg
            ON sg.game_id = sga.sgdb_game_id
        """
    )


def migrate_to_v7(db: sqlite3.Connection):
    db.execute("""
        ALTER TABLE games
        ADD COLUMN gamescope_enabled BOOLEAN NOT NULL DEFAULT 1
    """)


def migrate_to_v8(db: sqlite3.Connection) -> None:
    def drop_column_if_exists(
        table: str,
        column: str,
    ) -> None:
        columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}

        if column in columns:
            db.execute(f"ALTER TABLE {table} DROP COLUMN {column}")

    columns = {row["name"] for row in db.execute("PRAGMA table_info(selected_artwork)")}

    if "installed_path" not in columns:
        db.execute("""
            ALTER TABLE selected_artwork
            ADD COLUMN installed_path TEXT
        """)

    rows = db.execute("""
        SELECT
            g.id,
            g.name,
            g.launcher,
            g.icon,
            g.icon_manual,
            s.steam_entry,
            sa.artwork_id AS selected_artwork_id,
            sa.downloaded_path
        FROM games g
        LEFT JOIN steam_games s
            ON s.game_id = g.id
        LEFT JOIN selected_artwork sa
            ON sa.game_id = g.id
           AND sa.artwork_type = 'ICON'
    """).fetchall()

    for row in rows:
        game_id = row["id"]

        icon_path = Path(row["icon"]) if row["icon"] else None

        # Determine the Steam AppID.
        steam_entry = json.loads(row["steam_entry"]) if row["steam_entry"] else None

        if steam_entry is not None:
            appid = steam_entry["appid"]
            if not isinstance(appid, str):
                appid = str(appid & 0xFFFFFFFF)

        elif row["launcher"]:
            appid = generate_appid(
                Path(row["launcher"]),
                row["name"],
            )

        else:
            appid = None

        # Find existing Steam-installed icon files.
        installed_paths = []

        if appid is not None:
            for extension in ARTWORK_EXTENSIONS:
                path = STEAM_ICON_DIR / f"{appid}{extension}"

                if path.is_file():
                    installed_paths.append(path)

        # Prefer the existing selection's downloaded path.
        # Fall back to the legacy icon if necessary.
        downloaded_path = (
            Path(row["downloaded_path"]) if row["downloaded_path"] else icon_path
        )

        installed_path = None

        if (
            icon_path is not None
            and icon_path.is_file()
            and icon_path.suffix.lower() != ImageExtension.EXE
        ):
            # A non-EXE legacy icon path means the icon was already installed.
            installed_path = icon_path

        elif downloaded_path is not None:
            # EXE-derived icons need to be matched against Steam's installed icon.
            installed_path = match_installed_artwork(
                downloaded_path,
                installed_paths,
            )

        # Preserve existing selections.
        if row["selected_artwork_id"] is not None:
            if installed_path is not None:
                db.execute(
                    """
                    UPDATE selected_artwork
                    SET installed_path = ?
                    WHERE game_id = ?
                      AND artwork_type = 'ICON'
                """,
                    (str(installed_path), game_id),
                )

            continue

        # Nothing to migrate if there is no legacy icon.
        if icon_path is None:
            continue

        asset = {
            "id": str(icon_path),
            "path": icon_path,
            "url": icon_path.resolve().as_uri(),
            "name": icon_path.name,
            "source": "local",
            "kind": icon_path.suffix.lower(),
        }

        data = json.dumps(
            asset,
            indent=2,
            sort_keys=True,
            default=json_default,
        )

        # Reuse an existing local ICON asset if one already exists.
        existing = db.execute(
            """
            SELECT id
            FROM artwork
            WHERE type = 'ICON'
              AND source = 'local'
              AND external_id = ?
            LIMIT 1
        """,
            (str(icon_path),),
        ).fetchone()

        if existing is not None:
            artwork_id = existing["id"]

            db.execute(
                """
                UPDATE artwork
                SET url = ?, data = ?
                WHERE id = ?
            """,
                (asset["url"], data, artwork_id),
            )

        else:
            cursor = db.execute(
                """
                INSERT INTO artwork (
                    type, source, external_id, url, data
                )
                VALUES ('ICON', 'local', ?, ?, ?)
            """,
                (str(icon_path), asset["url"], data),
            )

            artwork_id = cursor.lastrowid

        db.execute(
            """
            INSERT OR IGNORE INTO game_artwork (
                game_id, artwork_id
            )
            VALUES (?, ?)
        """,
            (game_id, artwork_id),
        )

        db.execute(
            """
            INSERT INTO selected_artwork (
                game_id,
                artwork_type,
                artwork_id,
                downloaded_path,
                installed_path
            )
            VALUES (?, 'ICON', ?, ?, ?)
        """,
            (
                game_id,
                artwork_id,
                str(icon_path),
                str(installed_path) if installed_path else None,
            ),
        )

    # These columns are no longer needed after the artwork migration.
    drop_column_if_exists("games", "icon_manual")
    drop_column_if_exists("games", "icon")


def migrate_to_v9_and_v10(db: sqlite3.Connection) -> None:
    """Merge duplicate game records across movable locations."""
    # Existing v9 migration logic, with the corrected deduplication.

    if not POOL_DIR.is_mount():
        raise RuntimeError(
            f"Game storage is not mounted at {POOL_DIR}. "
            "Cannot safely run database migration."
        )

    rows = db.execute(
        """
        SELECT id, path
        FROM games
        WHERE path IS NOT NULL
        ORDER BY id
        """
    ).fetchall()

    identity_groups = {}

    for row in rows:
        game = Game(path=Path(row["path"]))
        identity = game_identity(game)

        if identity is not None:
            identity_groups.setdefault(identity, []).append(row)

    merge_groups = []

    for identity, group_rows in identity_groups.items():
        if len(group_rows) < 2:
            continue

        # Group records by their actual location.
        location_groups = {}

        for row in group_rows:
            location = get_game_location(Path(row["path"]))
            group = _movable_group(location) if location else None

            key = location.name if group is not None else None
            location_groups.setdefault(key, []).append(row)

        # Find records whose paths currently exist.
        existing_by_location = {}

        for name, location_rows in location_groups.items():
            existing = [row for row in location_rows if Path(row["path"]).exists()]

            if existing:
                existing.sort(key=lambda r: r["id"])
                existing_by_location[name] = existing

        # If multiple movable locations have existing copies,
        # preserve one record per location.
        if len(existing_by_location) > 1:
            keepers = []

            for location_rows in existing_by_location.values():
                keeper, *duplicates = location_rows
                keepers.append(keeper)

                if duplicates:
                    merge_groups.append((keeper, duplicates))

            # Merge records whose paths no longer exist.
            missing = [row for row in group_rows if not Path(row["path"]).exists()]

            if missing:
                merge_groups.append((keepers[0], missing))

        else:
            # Prefer an existing record; otherwise keep the lowest ID.
            existing = next(
                iter(existing_by_location.values()),
                [],
            )

            keeper = existing[0] if existing else min(group_rows, key=lambda r: r["id"])

            duplicates = [row for row in group_rows if row["id"] != keeper["id"]]

            if duplicates:
                merge_groups.append((keeper, duplicates))

    # ----------------------------------------------------------
    # Merge duplicate records.
    # ----------------------------------------------------------

    for keeper_row, duplicate_rows in merge_groups:
        keeper_id = keeper_row["id"]

        duplicate_ids = [row["id"] for row in duplicate_rows]

        debug_log(f"Deduplicating game IDs {duplicate_ids}; keeping {keeper_id}")

        for duplicate_id in duplicate_ids:
            # --------------------------------------------------
            # Merge games table metadata.
            # --------------------------------------------------

            db.execute(
                """
                UPDATE games
                SET
                    last_played = MAX(last_played, (
                        SELECT last_played
                        FROM games
                        WHERE id = ?
                    )),
                    launcher = COALESCE(
                        launcher,
                        (SELECT launcher FROM games WHERE id = ?)
                    )
                WHERE id = ?
                """,
                (duplicate_id, duplicate_id, keeper_id),
            )

            # --------------------------------------------------
            # Merge Steam metadata.
            # --------------------------------------------------

            duplicate = db.execute(
                """
                SELECT *
                FROM steam_games
                WHERE game_id = ?
                """,
                (duplicate_id,),
            ).fetchone()

            if duplicate is not None:
                keeper = db.execute(
                    """
                    SELECT *
                    FROM steam_games
                    WHERE game_id = ?
                    """,
                    (keeper_id,),
                ).fetchone()

                if keeper is None:
                    db.execute(
                        """
                        INSERT INTO steam_games (
                            game_id,
                            steam_key,
                            steam_entry,
                            in_steam,
                            steam_broken,
                            duplicate_steam
                        )
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            keeper_id,
                            duplicate["steam_key"],
                            duplicate["steam_entry"],
                            duplicate["in_steam"],
                            duplicate["steam_broken"],
                            duplicate["duplicate_steam"],
                        ),
                    )
                else:
                    db.execute(
                        """
                        UPDATE steam_games
                        SET
                            steam_key = COALESCE(steam_key, ?),
                            steam_entry = COALESCE(steam_entry, ?),
                            in_steam = MAX(in_steam, ?),
                            steam_broken = MAX(steam_broken, ?),
                            duplicate_steam = MAX(duplicate_steam, ?)
                        WHERE game_id = ?
                        """,
                        (
                            duplicate["steam_key"],
                            duplicate["steam_entry"],
                            duplicate["in_steam"],
                            duplicate["steam_broken"],
                            duplicate["duplicate_steam"],
                            keeper_id,
                        ),
                    )

            # --------------------------------------------------
            # Merge SteamGridDB game metadata.
            # --------------------------------------------------

            duplicate = db.execute(
                """
                SELECT *
                FROM steamgriddb_games
                WHERE game_id = ?
                """,
                (duplicate_id,),
            ).fetchone()

            if duplicate is not None:
                keeper = db.execute(
                    """
                    SELECT *
                    FROM steamgriddb_games
                    WHERE game_id = ?
                    """,
                    (keeper_id,),
                ).fetchone()

                if keeper is None:
                    db.execute(
                        """
                        INSERT INTO steamgriddb_games (
                            game_id,
                            sgdb_id,
                            name,
                            search,
                            game_data,
                            steam_platform_data,
                            cached
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            keeper_id,
                            duplicate["sgdb_id"],
                            duplicate["name"],
                            duplicate["search"],
                            duplicate["game_data"],
                            duplicate["steam_platform_data"],
                            duplicate["cached"],
                        ),
                    )
                else:
                    db.execute(
                        """
                        UPDATE steamgriddb_games
                        SET
                            sgdb_id = COALESCE(sgdb_id, ?),
                            name = COALESCE(name, ?),
                            search = COALESCE(search, ?),
                            game_data = COALESCE(game_data, ?),
                            steam_platform_data = COALESCE(
                                steam_platform_data, ?
                            ),
                            cached = COALESCE(cached, ?)
                        WHERE game_id = ?
                        """,
                        (
                            duplicate["sgdb_id"],
                            duplicate["name"],
                            duplicate["search"],
                            duplicate["game_data"],
                            duplicate["steam_platform_data"],
                            duplicate["cached"],
                            keeper_id,
                        ),
                    )

            # --------------------------------------------------
            # Transfer artwork associations.
            # --------------------------------------------------

            db.execute(
                """
                INSERT OR IGNORE INTO game_artwork (
                    game_id,
                    artwork_id
                )
                SELECT ?, artwork_id
                FROM game_artwork
                WHERE game_id = ?
                """,
                (keeper_id, duplicate_id),
            )

            # Preserve the keeper's existing selection.
            db.execute(
                """
                INSERT OR IGNORE INTO selected_artwork (
                    game_id,
                    artwork_type,
                    artwork_id,
                    downloaded_path,
                    installed_path
                )
                SELECT
                    ?,
                    artwork_type,
                    artwork_id,
                    downloaded_path,
                    installed_path
                FROM selected_artwork
                WHERE game_id = ?
                """,
                (keeper_id, duplicate_id),
            )

            # Transfer SteamGridDB artwork associations.
            db.execute(
                """
                INSERT OR IGNORE INTO steamgriddb_artwork (
                    artwork_id,
                    sgdb_game_id,
                    sgdb_art_id
                )
                SELECT
                    artwork_id,
                    ?,
                    sgdb_art_id
                FROM steamgriddb_artwork
                WHERE sgdb_game_id = ?
                """,
                (keeper_id, duplicate_id),
            )

            # --------------------------------------------------
            # Remove the duplicate after transferring its data.
            # --------------------------------------------------

            db.execute(
                "DELETE FROM games WHERE id = ?",
                (duplicate_id,),
            )

        debug_log(f"Finished deduplicating; retained game ID {keeper_id}")

    # ----------------------------------------------------------
    # Verify remaining duplicate identities.
    # ----------------------------------------------------------

    remaining = {}

    for row in db.execute("SELECT id, path FROM games WHERE path IS NOT NULL"):
        identity = game_identity(Game(path=Path(row["path"])))

        if identity is not None:
            remaining.setdefault(identity, []).append(row)

    duplicates = {}

    for identity, group_rows in remaining.items():
        if len(group_rows) < 2:
            continue

        locations = set()
        all_exist = True

        for row in group_rows:
            path = Path(row["path"])
            location = get_game_location(path)

            if not path.exists() or location is None:
                all_exist = False
                break

            locations.add(location.name)

        # Multiple existing copies in different locations are intentional.
        if not all_exist or len(locations) != len(group_rows):
            duplicates[identity] = [row["id"] for row in group_rows]

    if duplicates:
        raise RuntimeError(
            f"Duplicate game identities remain after migration: {duplicates}"
        )


def reconcile_game_paths(
    db: sqlite3.Connection,
    games: list[Game],
) -> None:
    rows = db.execute("SELECT id, path FROM games WHERE path IS NOT NULL").fetchall()

    existing_paths = {}

    for row in rows:
        identity = game_identity(Game(name="", path=Path(row["path"])))

        if identity is not None:
            existing_paths.setdefault(identity, []).append(row)

    used_rows = set()

    for game in games:
        if game.path is None:
            continue

        identity = game_identity(game)
        if identity is None:
            continue

        location = get_game_location(game.path)
        canonical_path = game.path

        if location is not None:
            relative_path = next(
                (
                    game.path.relative_to(base)
                    for base in (location.local_path, location.remote_path)
                    if base is not None and game.path.is_relative_to(base)
                ),
                None,
            )

            if relative_path is not None:
                paths = (
                    location.remote_path / relative_path
                    if location.remote_path is not None
                    else None,
                    location.local_path / relative_path
                    if location.local_path is not None
                    else None,
                )

                canonical_path = next(
                    (path for path in paths if path is not None and path.exists()),
                    game.path,
                )

        matching_rows = existing_paths.get(identity, [])

        unused_rows = [row for row in matching_rows if row["id"] not in used_rows]

        if not unused_rows:
            continue

        # Prefer a record belonging to the same logical location.
        existing_row = next(
            (
                row
                for row in unused_rows
                if location is not None
                and (row_location := get_game_location(Path(row["path"]))) is not None
                and row_location.name == location.name
            ),
            None,
        )

        # If only one record remains, it can represent a moved game.
        if existing_row is None and len(unused_rows) == 1:
            existing_row = unused_rows[0]

        if existing_row is None:
            continue

        used_rows.add(existing_row["id"])

        if existing_row["path"] != str(canonical_path):
            db.execute(
                "UPDATE games SET path = ? WHERE id = ?",
                (str(canonical_path), existing_row["id"]),
            )


def save_game(db: sqlite3.Connection, game: Game) -> None:
    if game.path is None:
        raise RuntimeError(f"Cannot save game without a path: {game.name}")

    path = str(game.path)
    params = (
        path,
        game.name,
        game.size,
        game.mtime,
        game.file_count,
        game.last_played,
        str(game.launcher) if game.launcher is not None else None,
        int(game.gamescope_enabled),
    )

    game_id = game.id

    # 1. Already known ID → just update
    if game_id is not None:
        db.execute(
            """
            UPDATE games
            SET path = ?, name = ?, size = ?, mtime = ?,
                file_count = ?, last_played = ?, launcher = ?,
                gamescope_enabled = ?
            WHERE id = ?
            """,
            (*params, game_id),
        )
    else:
        # 2. Try identity match (path may have changed)
        identity = game_identity(game)
        if identity is not None:
            rows = db.execute(
                "SELECT id, path FROM games WHERE path != ?",
                (path,),
            ).fetchall()

            for row in rows:
                existing = Game(name="", path=Path(row["path"]))
                if game_identity(existing) == identity:
                    game_id = row["id"]
                    db.execute(
                        """
                        UPDATE games
                        SET path = ?, name = ?, size = ?, mtime = ?,
                            file_count = ?, last_played = ?, launcher = ?,
                            gamescope_enabled = ?
                        WHERE id = ?
                        """,
                        (*params, game_id),
                    )
                    break

        # 3. Still no ID → insert (or upsert on path)
        if game_id is None:
            db.execute(
                """
                INSERT INTO games (
                    path, name, size, mtime, file_count,
                    last_played, launcher, gamescope_enabled
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    name = excluded.name,
                    size = excluded.size,
                    mtime = excluded.mtime,
                    file_count = excluded.file_count,
                    last_played = excluded.last_played,
                    launcher = excluded.launcher,
                    gamescope_enabled = excluded.gamescope_enabled
                """,
                params,
            )
            row = db.execute("SELECT id FROM games WHERE path = ?", (path,)).fetchone()
            if row is None:
                raise RuntimeError(f"Failed to retrieve database ID for game: {path}")
            game_id = row["id"]

    game.id = game_id

    save_steam(db, game_id, game)
    save_steamgriddb(db, game_id, game)
    debug_log(f"DEBUG: saving artwork for {game.name}")
    save_steamgriddb_artwork(db, game_id, game)


def purge_game_record(
    db: sqlite3.Connection,
    game_id: int | None,
    game_path: Path | None,
) -> None:
    if game_id is not None:
        db.execute(
            "DELETE FROM games WHERE id = ?",
            (game_id,),
        )
    elif game_path is not None:
        db.execute(
            "DELETE FROM games WHERE path = ?",
            (str(game_path),),
        )
    else:
        raise ValueError("Cannot delete a game without an ID or path")

    # Remove artwork that is no longer associated with any game.
    # Artwork shared by other games is preserved.
    db.execute(
        """
        DELETE FROM artwork
        WHERE NOT EXISTS (
            SELECT 1
            FROM game_artwork
            WHERE game_artwork.artwork_id = artwork.id
        )
        AND NOT EXISTS (
            SELECT 1
            FROM selected_artwork
            WHERE selected_artwork.artwork_id = artwork.id
        )
        AND NOT EXISTS (
            SELECT 1
            FROM steamgriddb_artwork
            WHERE steamgriddb_artwork.artwork_id = artwork.id
        )
        """
    )


def save_steam(
    db: sqlite3.Connection,
    game_id: int,
    game: Game,
):
    if not any(
        value is not None
        for value in (
            game.steam_key,
            game.steam_entry,
        )
    ) and not any(
        (
            game.in_steam,
            game.steam_broken,
            game.duplicate_steam,
        )
    ):
        db.execute(
            "DELETE FROM steam_games WHERE game_id = ?",
            (game_id,),
        )
        return

    db.execute(
        """
        INSERT INTO steam_games (
            game_id,
            steam_key,
            steam_entry,
            in_steam,
            steam_broken,
            duplicate_steam
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(game_id) DO UPDATE SET
            steam_key = excluded.steam_key,
            steam_entry = excluded.steam_entry,
            in_steam = excluded.in_steam,
            steam_broken = excluded.steam_broken,
            duplicate_steam = excluded.duplicate_steam
        """,
        (
            game_id,
            game.steam_key,
            (
                json.dumps(
                    game.steam_entry,
                    indent=2,
                    sort_keys=True,
                    default=json_default,
                )
                if game.steam_entry is not None
                else None
            ),
            int(game.in_steam),
            int(game.steam_broken),
            int(game.duplicate_steam),
        ),
    )


def save_steamgriddb(
    db: sqlite3.Connection,
    game_id: int,
    game: Game,
):
    sgdb = game.steamgriddb

    if not sgdb:
        db.execute(
            "DELETE FROM steamgriddb_games WHERE game_id = ?",
            (game_id,),
        )
        return

    sgdb_game = sgdb.get("game")

    db.execute(
        """
        INSERT INTO steamgriddb_games (
            game_id,
            sgdb_id,
            name,
            search,
            game_data,
            steam_platform_data,
            cached
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(game_id) DO UPDATE SET
            sgdb_id = excluded.sgdb_id,
            name = excluded.name,
            search = excluded.search,
            game_data = excluded.game_data,
            steam_platform_data = excluded.steam_platform_data,
            cached = excluded.cached
        """,
        (
            game_id,
            sgdb_game.get("id") if sgdb_game else None,
            sgdb_game.get("name") if sgdb_game else None,
            sgdb.get("search"),
            (
                json.dumps(
                    sgdb_game,
                    indent=2,
                    sort_keys=True,
                    default=json_default,
                )
                if sgdb_game is not None
                else None
            ),
            (
                json.dumps(
                    sgdb.get("steam_platform_data"),
                    indent=2,
                    sort_keys=True,
                    default=json_default,
                )
                if sgdb.get("steam_platform_data") is not None
                else None
            ),
            sgdb.get("cached"),
        ),
    )


def save_steamgriddb_artwork(
    db: sqlite3.Connection,
    game_id: int,
    game: Game,
):
    sgdb = game.steamgriddb
    artwork_by_type = sgdb.get("art", {})
    selected = sgdb.get("selected", {})
    debug_log(f"DEBUG selected artwork for {game.name}: {selected!r}")
    sgdb_game = sgdb.get("game") or {}
    sgdb_game_id = sgdb_game.get("id")

    def type_name(artwork_type: ArtworkType | str) -> str:
        return (
            artwork_type.name
            if isinstance(artwork_type, ArtworkType)
            else str(artwork_type)
        )

    def persist_artwork(
        artwork_type: ArtworkType,
        asset: dict,
    ) -> int | None:
        source = asset.get("source", "steamgriddb")
        external_id = asset.get("id")
        external_id = str(external_id) if external_id is not None else None

        def associate_with_game(artwork_id: int) -> None:
            db.execute(
                """
                INSERT OR IGNORE INTO game_artwork (game_id, artwork_id)
                VALUES (?, ?)
                """,
                (game_id, artwork_id),
            )

        data = json.dumps(
            asset,
            indent=2,
            sort_keys=True,
            default=json_default,
        )

        author = asset.get("author")
        if author is not None:
            author = json.dumps(
                author,
                indent=2,
                sort_keys=True,
                default=json_default,
            )

        # Local artwork does not require a SteamGridDB game ID.
        if source in ("local", "steam"):
            row = db.execute(
                """
                SELECT id
                FROM artwork
                WHERE source = ?
                AND type = ?
                AND external_id = ?
                ORDER BY id
                LIMIT 1
                """,
                (source, artwork_type.name, external_id),
            ).fetchone()

            if row is not None:
                artwork_id = row["id"]
                db.execute(
                    """
                    UPDATE artwork
                    SET url = ?, thumb_url = ?, author = ?, data = ?
                    WHERE id = ?
                    """,
                    (
                        asset.get("url"),
                        asset.get("thumb"),
                        author,
                        data,
                        artwork_id,
                    ),
                )
                associate_with_game(artwork_id)
                return artwork_id

            cursor = db.execute(
                """
                INSERT INTO artwork (
                    type, source, external_id, url, thumb_url, author, data
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artwork_type.name,
                    source,
                    external_id,
                    asset.get("url"),
                    asset.get("thumb"),
                    author,
                    data,
                ),
            )
            artwork_id = cursor.lastrowid
            associate_with_game(artwork_id)
            return artwork_id

        # SteamGridDB assets are identified through their association table.
        if sgdb_game_id is None:
            debug_log(
                f"Skipping {source!r} artwork {external_id!r}: "
                "missing SteamGridDB game ID"
            )
            return None

        if external_id is None:
            debug_log(f"Skipping {source!r} artwork: missing external ID")
            return None

        row = db.execute(
            """
            SELECT artwork_id
            FROM steamgriddb_artwork
            WHERE sgdb_game_id = ? AND sgdb_art_id = ?
            """,
            (game_id, external_id),
        ).fetchone()

        if row is not None:
            artwork_id = row["artwork_id"]
            db.execute(
                """
                UPDATE artwork
                SET type = ?, source = ?, external_id = ?, url = ?,
                    thumb_url = ?, author = ?, data = ?
                WHERE id = ?
                """,
                (
                    artwork_type.name,
                    source,
                    external_id,
                    asset.get("url"),
                    asset.get("thumb"),
                    author,
                    data,
                    artwork_id,
                ),
            )
            associate_with_game(artwork_id)
            return artwork_id

        cursor = db.execute(
            """
            INSERT INTO artwork (
                type, source, external_id, url, thumb_url, author, data
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artwork_type.name,
                source,
                external_id,
                asset.get("url"),
                asset.get("thumb"),
                author,
                data,
            ),
        )
        artwork_id = cursor.lastrowid

        db.execute(
            """
            INSERT INTO steamgriddb_artwork (
                artwork_id, sgdb_game_id, sgdb_art_id
            )
            VALUES (?, ?, ?)
            """,
            (artwork_id, game_id, external_id),
        )

        associate_with_game(artwork_id)
        return artwork_id

    # Save all available SteamGridDB results when a match exists.
    # Local selections are persisted separately below.
    for artwork_type, artwork_list in artwork_by_type.items():
        if not artwork_list:
            continue

        for asset in artwork_list:
            persist_artwork(artwork_type, asset)

    # Save the current selection independently of the search results.
    for artwork_type, selected_info in selected.items():
        artwork_type_name = type_name(artwork_type)

        # A None value means this type currently has no selection.
        if selected_info is None:
            db.execute(
                """
                DELETE FROM selected_artwork
                WHERE game_id = ? AND artwork_type = ?
                """,
                (game_id, artwork_type_name),
            )
            continue

        asset = selected_info.get("asset")
        if not isinstance(asset, dict):
            continue

        if not isinstance(artwork_type, ArtworkType):
            try:
                artwork_type = ArtworkType(artwork_type)
            except ValueError as exc:
                raise ValueError(f"Unknown artwork type: {artwork_type!r}") from exc

        artwork_id = persist_artwork(artwork_type, asset)
        if not artwork_id:
            continue
        downloaded_path = selected_info.get("downloaded_path")
        installed_path = selected_info.get("installed_path")

        db.execute(
            """
            INSERT INTO selected_artwork (
                game_id,
                artwork_type,
                artwork_id,
                downloaded_path,
                installed_path
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(game_id, artwork_type)
            DO UPDATE SET
                artwork_id = excluded.artwork_id,
                downloaded_path = excluded.downloaded_path,
                installed_path = excluded.installed_path
            """,
            (
                game_id,
                artwork_type_name,
                artwork_id,
                str(downloaded_path) if downloaded_path is not None else None,
                str(installed_path) if installed_path is not None else None,
            ),
        )


def load_selected_artwork(
    db: sqlite3.Connection,
    game_id: int,
) -> dict:
    selected = {
        ArtworkType.GRID_PORTRAIT: None,
        ArtworkType.GRID_SQUARE: None,
        ArtworkType.GRID_LANDSCAPE: None,
        ArtworkType.HERO: None,
        ArtworkType.LOGO: None,
        ArtworkType.ICON: None,
    }

    rows = db.execute(
        """
        SELECT
            sa.artwork_type,
            a.data,
            sa.downloaded_path,
            sa.installed_path
        FROM selected_artwork AS sa
        JOIN artwork AS a
            ON a.id = sa.artwork_id
        WHERE sa.game_id = ?
        """,
        (game_id,),
    ).fetchall()

    for row in rows:
        try:
            artwork_type = ArtworkType[row["artwork_type"]]
        except KeyError:
            continue

        try:
            artwork = json.loads(row["data"])
        except json.JSONDecodeError:
            continue

        # Restore local asset paths after JSON deserialization.
        if artwork.get("source") == "local" and artwork.get("path") is not None:
            artwork["path"] = Path(artwork["path"])

        downloaded_path = row["downloaded_path"]
        if downloaded_path is not None:
            downloaded_path = Path(downloaded_path)

        installed_path = row["installed_path"]
        if installed_path is not None:
            installed_path = Path(installed_path)

        selected[artwork_type] = {
            "asset": artwork,
            "downloaded_path": downloaded_path,
            "installed_path": installed_path,
        }

    return selected


def load_steamgriddb_artwork(
    db: sqlite3.Connection,
    game_id: int,
) -> tuple[dict, bool]:
    art = {
        ArtworkType.GRID_PORTRAIT: [],
        ArtworkType.GRID_SQUARE: [],
        ArtworkType.GRID_LANDSCAPE: [],
        ArtworkType.HERO: [],
        ArtworkType.LOGO: [],
        ArtworkType.ICON: [],
    }

    rows = db.execute(
        """
        SELECT
            a.type,
            a.data
        FROM game_artwork AS ga
        JOIN artwork AS a
            ON a.id = ga.artwork_id
        WHERE ga.game_id = ?
        """,
        (game_id,),
    ).fetchall()

    for row in rows:
        try:
            artwork_type = ArtworkType[row["type"]]
        except KeyError:
            continue

        try:
            artwork = json.loads(row["data"])
        except json.JSONDecodeError:
            continue

        art[artwork_type].append(artwork)

    return art, bool(rows)


# ============================================================
# JSON to SQLite
# ============================================================


def migrate_legacy_icon(
    db: sqlite3.Connection,
    game_id: int,
    game_data: dict,
) -> None:
    icon = game_data.get("icon")

    if not icon:
        return

    # Preserve an ICON selection already imported from SteamGridDB.
    existing = db.execute(
        """
        SELECT 1
        FROM selected_artwork
        WHERE game_id = ?
          AND artwork_type = 'ICON'
        """,
        (game_id,),
    ).fetchone()

    if existing is not None:
        return

    icon_path = Path(icon)

    asset = {
        "id": str(icon_path),
        "path": icon_path,
        "url": icon_path.resolve().as_uri(),
        "name": icon_path.name,
        "source": "local",
        "kind": icon_path.suffix.lower(),
    }

    data = json.dumps(
        asset,
        indent=2,
        sort_keys=True,
        default=json_default,
    )

    existing = db.execute(
        """
        SELECT id
        FROM artwork
        WHERE type = 'ICON'
          AND source = 'local'
          AND external_id = ?
        LIMIT 1
        """,
        (str(icon_path),),
    ).fetchone()

    if existing is not None:
        artwork_id = existing["id"]
    else:
        cursor = db.execute(
            """
            INSERT INTO artwork (
                type, source, external_id, url, data
            )
            VALUES ('ICON', 'local', ?, ?, ?)
            """,
            (str(icon_path), asset["url"], data),
        )
        artwork_id = cursor.lastrowid

    db.execute(
        """
        INSERT OR IGNORE INTO game_artwork (
            game_id, artwork_id
        )
        VALUES (?, ?)
        """,
        (game_id, artwork_id),
    )

    # Preserve the old installed path when it is an existing image.
    installed_path = (
        icon_path
        if (icon_path.is_file() and icon_path.suffix.lower() != ImageExtension.EXE)
        else None
    )

    db.execute(
        """
        INSERT INTO selected_artwork (
            game_id,
            artwork_type,
            artwork_id,
            downloaded_path,
            installed_path
        )
        VALUES (?, 'ICON', ?, ?, ?)
        """,
        (
            game_id,
            artwork_id,
            str(icon_path),
            str(installed_path) if installed_path else None,
        ),
    )


def migrate_game_data():
    if not GAME_DATA.exists():
        return

    with GAME_DATA.open() as f:
        data = json.load(f)

    with get_db() as db:
        for game_data in data.get("shared", []):
            migrate_game(db, game_data)

        for game_data in data.get("archived", []):
            migrate_game(db, game_data)

        db.commit()


def migrate_game(
    db: sqlite3.Connection,
    game_data: dict,
):
    path = str(game_data["path"])
    name = game_data["name"]

    db.execute(
        """
        INSERT INTO games (
            path,
            name,
            size,
            mtime,
            file_count,
            last_played,
            launcher
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            name = excluded.name,
            size = excluded.size,
            mtime = excluded.mtime,
            file_count = excluded.file_count,
            last_played = excluded.last_played,
            launcher = excluded.launcher
        """,
        (
            path,
            name,
            game_data.get("size", 0),
            game_data.get("mtime", 0),
            game_data.get("file_count", 0),
            game_data.get("last_played", 0),
            str(game_data["launcher"])
            if game_data.get("launcher") is not None
            else None,
        ),
    )

    row = db.execute(
        "SELECT id FROM games WHERE path = ?",
        (path,),
    ).fetchone()

    if row is None:
        raise RuntimeError(f"Failed to retrieve database ID for game: {path}")

    game_id = row["id"]

    migrate_steam(db, game_id, game_data)
    migrate_steamgriddb(db, game_id, game_data)
    migrate_legacy_icon(db, game_id, game_data)


def migrate_steam(
    db: sqlite3.Connection,
    game_id: int,
    game_data: dict,
):
    if not any(
        game_data.get(key) is not None
        for key in (
            "steam_key",
            "steam_entry",
            "in_steam",
            "steam_broken",
            "duplicate_steam",
        )
    ):
        return

    db.execute(
        """
        INSERT INTO steam_games (
            game_id,
            steam_key,
            steam_entry,
            in_steam,
            steam_broken,
            duplicate_steam
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(game_id) DO UPDATE SET
            steam_key = excluded.steam_key,
            steam_entry = excluded.steam_entry,
            in_steam = excluded.in_steam,
            steam_broken = excluded.steam_broken,
            duplicate_steam = excluded.duplicate_steam
        """,
        (
            game_id,
            game_data.get("steam_key"),
            json.dumps(
                game_data.get("steam_entry"),
                indent=2,
                sort_keys=True,
                default=json_default,
            )
            if game_data.get("steam_entry") is not None
            else None,
            int(game_data.get("in_steam", False)),
            int(game_data.get("steam_broken", False)),
            int(game_data.get("duplicate_steam", False)),
        ),
    )


def migrate_steamgriddb(
    db: sqlite3.Connection,
    game_id: int,
    game_data: dict,
):
    sgdb = game_data.get("steamgriddb")

    if not sgdb:
        return

    game = sgdb.get("game")

    db.execute(
        """
        INSERT INTO steamgriddb_games (
            game_id,
            sgdb_id,
            name,
            search,
            game_data,
            steam_platform_data,
            cached
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(game_id) DO UPDATE SET
            sgdb_id = excluded.sgdb_id,
            name = excluded.name,
            search = excluded.search,
            game_data = excluded.game_data,
            steam_platform_data = excluded.steam_platform_data,
            cached = excluded.cached
        """,
        (
            game_id,
            game.get("id") if game else None,
            game.get("name") if game else None,
            sgdb.get("search"),
            json.dumps(game, indent=2, sort_keys=True, default=json_default)
            if game is not None
            else None,
            json.dumps(
                sgdb.get("steam_platform_data"),
                indent=2,
                sort_keys=True,
                default=json_default,
            )
            if sgdb.get("steam_platform_data") is not None
            else None,
            sgdb.get("cached"),
        ),
    )


# ============================================================
# MODEL
# ============================================================


@dataclass
class LaunchInfo:
    cmd: list[str]
    cwd: Path
    env: dict[str, str]


def get_launch_info(game: Game, *, for_steam: bool = False) -> LaunchInfo:
    def wrap_gamescope(cmd: list[str]) -> list[str]:
        if not game.gamescope_enabled:
            return cmd

        if for_steam:
            return cmd

        if not shutil.which("gamescope"):
            return cmd

        return [
            "gamescope",
            "-f",
            "-w",
            "1920",
            "-h",
            "1080",
            "-W",
            "1920",
            "-H",
            "1080",
            "--force-grab-cursor",
            "--backend",
            "sdl",
            "--",
            *cmd,
        ]

    if game.launcher is None:
        game.launcher = find_launcher(game)

    if game.launcher is None:
        raise RuntimeError(f"No launcher found for '{game.name}'.")
    launcher = game.launcher

    if is_steam_launcher(launcher):
        raise RuntimeError("Steam URI launchers must be opened directly.")

    launcher = Path(launcher)
    env = os.environ.copy()
    env["SDL_VIDEODRIVER"] = "x11"

    if launcher.suffix.casefold() == ".sh":
        if for_steam:
            cmd = [str(launcher)]
        else:
            cmd = wrap_gamescope(["bash", str(launcher)])

    elif launcher.suffix == ".swf":
        cmd = wrap_gamescope(["ruffle", str(launcher)])

    elif launcher.suffix == ".apk" and for_steam == False:
        cmd = [
            "waydroid",
            "app",
            "install",
            str(launcher),
        ]

    elif launcher.suffix == ".exe":
        if for_steam:
            cmd = [str(launcher)]
        else:
            compat = Path.home() / ".local/share/game_archiver/compatdata" / game.name

            compat.mkdir(parents=True, exist_ok=True)

            env["STEAM_COMPAT_DATA_PATH"] = str(compat)
            env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(
                Path.home() / ".local/share/Steam"
            )

            # env["WINE_FULLSCREEN_FSR"] = "1"
            # env["DXVK_HUD"] = "full"

            bash_cmd = f"""
set -x

echo "=== Environment ==="
env | grep -E '^(STEAM|WINE|PROTON|DXVK)'
echo

echo "=== Working Directory ==="
pwd
echo

echo "=== Proton ==="
echo {shlex.quote(str(PROTON))}
echo

echo "=== Wine Version ==="
{shlex.quote(str(PROTON))} run wine --version
echo

echo "=== Prefix Contents ==="
find "$STEAM_COMPAT_DATA_PATH" -maxdepth 2
echo

echo "=== Registry DirectX ==="
{shlex.quote(str(PROTON))} run reg query "HKLM\\Software\\Microsoft\\DirectX"
echo

echo "=== Windows System32 ==="
ls "$STEAM_COMPAT_DATA_PATH/pfx/drive_c/windows/system32" | grep -i d3d
echo

echo "=== Installed DLLs ==="
find "$STEAM_COMPAT_DATA_PATH/pfx/drive_c/windows" \\
    \\( -iname "d3d*.dll" -o -iname "d3dx*.dll" -o -iname "xinput*.dll" \\)
echo

echo "=== Looking for D3DX ==="
find "$STEAM_COMPAT_DATA_PATH/pfx" -iname "d3dx*.dll"
echo

echo "=== Looking for XInput ==="
find "$STEAM_COMPAT_DATA_PATH/pfx" -iname "xinput*.dll"
echo

echo "=== Launching ==="
exec {shlex.quote(str(PROTON))} run {shlex.quote(str(launcher))}
"""

            cmd = [
                "kitty",
                "--hold",
                "bash",
                "-lc",
                shlex.join(wrap_gamescope(["bash", "-c", bash_cmd])),
            ]

    else:
        cmd = [str(launcher)]

    return LaunchInfo(
        cmd=cmd,
        cwd=launcher.parent,
        env=env,
    )


class SteamGridDBAction(Enum):
    BROWSE = auto()
    DOWNLOAD = auto()
    CHANGE = auto()
    CANCEL = auto()


@dataclass
class Game:
    id: int | None = None
    name: str = ""
    path: Path | None = None
    size: int = 0
    mtime: float = 0.0
    file_count: int = 0
    last_played: float = 0.0
    selected: bool = False
    launcher: Path | str | None = None
    steam_key: str | None = None
    steam_entry: dict | None = None
    in_steam: bool = False
    steam_broken: bool = False
    duplicate_steam: bool = False
    steamgriddb: dict = field(default_factory=dict)
    gamescope_enabled: bool = True


# ============================================================
# HELPERS
# ============================================================


def steam_appid_from_uri(value: str) -> str | None:
    parsed = urlsplit(value)

    if (
        parsed.scheme == "steam"
        and parsed.netloc == "rungameid"
        and parsed.path.lstrip("/").isdigit()
    ):
        return parsed.path.lstrip("/")

    return None


def is_steam_launcher(launcher: Path | str | None) -> bool:
    return isinstance(launcher, str) and steam_appid_from_uri(launcher) is not None


def purge_duplicate_shortcuts(
    entries: dict[str, dict],
    games: list[Game],
) -> bool:
    """Remove duplicate Steam shortcuts while keeping the oldest entry."""
    changed = False

    for game in games:
        if not game.steam_key or not game.duplicate_steam:
            continue

        if game.steam_key not in entries:
            continue

        # Get the directory associated with the game's cached shortcut.
        target_dir = shortcut_game_directory(entries[game.steam_key])

        if target_dir is None:
            continue

        target_dir = target_dir.resolve()

        # Find shortcuts pointing to this exact directory.
        matches = []

        for key, entry in entries.items():
            directory = shortcut_game_directory(entry)

            if directory is None:
                continue

            if directory.resolve() == target_dir:
                matches.append(key)

        if len(matches) < 2:
            game.duplicate_steam = False
            continue

        # Keep the oldest shortcut (lowest numeric key).
        matches.sort(key=int)
        keep_key = matches[0]

        # Never delete entries unless the cached shortcut is among the matches.
        if game.steam_key not in matches:
            continue

        for key in matches:
            if key != keep_key:
                del entries[key]
                changed = True

        game.steam_key = keep_key
        game.steam_entry = entries[keep_key]
        game.duplicate_steam = False

    return changed


def shortcut_game_directory(entry: dict) -> Path | None:
    start_dir = entry.get("StartDir") or entry.get("startdir")

    if not start_dir:
        return None

    return Path(start_dir.strip('"'))


def game_launch_identity(
    game: Game,
) -> tuple[tuple[str, ...], Path] | None:
    path = launch_path(game)

    if path is None:
        return None

    return game_identity(Game(name=game.name, path=path))


def has_steamgriddb_match(game: Game) -> bool:
    sgdb_game = game.steamgriddb.get("game")
    return isinstance(sgdb_game, dict) and bool(sgdb_game.get("id"))


def should_ignore_game(path: Path, root: Path) -> bool:
    return any(
        part.startswith(".") or part in IGNORE_DIRS
        for part in path.relative_to(root).parts
    )


def can_add_to_steam(game: Game) -> bool:
    if game.path is None:
        return False

    location = get_game_location(game.path)
    return location is None or location.steam_shortcuts


def get_move_locations(remote_available: bool) -> list[GameLocation]:
    return [
        location for location in GAME_LOCATIONS if location.is_movable(remote_available)
    ]


def get_location(name: str) -> GameLocation:
    return next(location for location in GAME_LOCATIONS if location.name == name)


def _movable_group(
    location: GameLocation,
) -> tuple[str, str] | None:
    """Return the locations linked by a reciprocal move_to relationship."""
    if location.move_to is None:
        return None

    linked = next(
        (
            other
            for other in GAME_LOCATIONS
            if other.name == location.move_to and other.move_to == location.name
        ),
        None,
    )

    if linked is None:
        return None

    return tuple(sorted((location.name, linked.name)))


def game_identity(
    game: Game,
) -> tuple[tuple[str, ...], Path] | None:
    if game.path is None:
        return None

    location = get_game_location(game.path)

    if location is None:
        return (("path",), game.path)

    for base in (location.local_path, location.remote_path):
        if base is not None and game.path.is_relative_to(base):
            relative = game.path.relative_to(base)
            group = _movable_group(location)

            return (group or (location.name,), relative)

    return (("path",), game.path)


def is_on_disk_duplicate(game: Game) -> tuple[str, ...] | None:
    """Return the movable group when the game exists in multiple locations."""
    if game.path is None:
        return None

    location = get_game_location(game.path)
    if location is None:
        return None

    group = _movable_group(location)
    if group is None:
        return None

    relative = None
    for base in (location.local_path, location.remote_path):
        if base is not None and game.path.is_relative_to(base):
            relative = game.path.relative_to(base)
            break

    if relative is None:
        return None

    locations_present = set()

    for name in group:
        loc = get_location(name)
        if loc is None:
            continue

        for base in (loc.local_path, loc.remote_path):
            if base is not None and (base / relative).exists():
                locations_present.add(name)
                break

    return group if len(locations_present) > 1 else None


def has_on_disk_duplicate(game: Game) -> bool:
    """Return whether the game exists in multiple movable locations."""
    return is_on_disk_duplicate(game) is not None


def get_game_location(path: Path) -> GameLocation | None:
    for location in GAME_LOCATIONS:
        for base in (location.local_path, location.remote_path):
            if base is not None and path.is_relative_to(base):
                return location

    return None


def match_installed_artwork(
    downloaded_path: Path,
    installed_paths: list[Path],
) -> Path | None:
    expected_path = correct_image_extension(downloaded_path)

    if expected_path is None or not expected_path.is_file():
        return None

    expected_suffix = expected_path.suffix.lower()
    expected_size = expected_path.stat().st_size

    matches = [
        path
        for path in installed_paths
        if path.suffix.lower() == expected_suffix
        and path.stat().st_size == expected_size
    ]

    if len(matches) == 1:
        return matches[0]

    if not matches:
        return None

    # Only hash when the original downloaded format matches
    # the installed format. Converted formats cannot be compared
    # directly against their original source.
    if downloaded_path.suffix.lower() != expected_suffix:
        return None

    expected_hash = hashlib.sha256(downloaded_path.read_bytes()).digest()

    return next(
        (
            path
            for path in matches
            if hashlib.sha256(path.read_bytes()).digest() == expected_hash
        ),
        None,
    )


def update_artwork_paths_for_game(
    game: Game,
    old_path: Path,
    new_path: Path,
) -> None:
    if game.steamgriddb is None:
        return

    shared = get_location("Shared")
    if shared.local_path is None:
        return

    archived = get_location("Archived")

    # Shared -> Archived
    if shared.remote_path is not None and old_path.is_relative_to(shared.remote_path):
        path_mappings = [
            (
                shared.remote_path / old_path.name,
                new_path,
            ),
            (
                shared.local_path / old_path.name,
                new_path,
            ),
        ]

    # Archived -> Shared
    elif archived.remote_path is not None and old_path.is_relative_to(
        archived.remote_path
    ):
        path_mappings = [
            (
                old_path,
                shared.local_path / old_path.name,
            ),
        ]

    else:
        return

    art = game.steamgriddb.get("art", {})
    selected = game.steamgriddb.get("selected", {})

    for assets in art.values():
        for asset in assets:
            update_artwork_paths(asset, path_mappings)

    for selected_info in selected.values():
        if selected_info is None:
            continue

        update_artwork_paths(selected_info, path_mappings)

        asset = selected_info.get("asset")
        if asset:
            update_artwork_paths(asset, path_mappings)


def update_artwork_paths(
    artwork: dict | None,
    path_mappings: list[tuple[Path, Path]],
) -> None:
    if artwork is None:
        return

    for key in ("path", "downloaded_path", "installed_path"):
        value = artwork.get(key)

        if value is None:
            continue

        value = Path(value)

        for old_root, new_root in path_mappings:
            try:
                relative_path = value.relative_to(old_root)
            except ValueError:
                continue

            artwork[key] = new_root / relative_path
            break

    value = artwork.get("url")

    if value and value.startswith("file://"):
        try:
            path = Path.from_uri(value)
        except ValueError:
            return

        for old_root, new_root in path_mappings:
            try:
                relative_path = path.relative_to(old_root)
            except ValueError:
                continue

            artwork["url"] = (new_root / relative_path).as_uri()
            break


def get_selected_artwork(
    game: Game,
    artwork_type: ArtworkType,
) -> dict | None:
    if not game.steamgriddb:
        return None
    return (game.steamgriddb.get("selected") or {}).get(artwork_type)


def icon_candidate_to_artwork(
    candidate: IconCandidate,
) -> dict:
    return {
        "id": str(candidate.path),
        "path": candidate.path,
        "url": candidate.path.as_uri(),
        "name": candidate.path.name,
        "source": "local",
        "style": "Detected",
        "kind": candidate.kind,
        "score": candidate.score,
    }


FIRSTLOG = True


def debug_log(message: str):
    global FIRSTLOG

    if LOGGING_ENABLED:
        with open(LOGGING_FILE, "w" if FIRSTLOG else "a") as f:
            f.write(f"{message}\n")
        FIRSTLOG = False


def add_selected_artwork_to_art(
    game: Game,
    artwork_type: ArtworkType,
    asset: dict,
) -> None:
    sgdb = game.steamgriddb
    art = sgdb.setdefault("art", {})
    artwork_list = art.setdefault(artwork_type, [])

    asset_id = asset.get("id")
    source = asset.get("source")

    # Avoid adding the same asset more than once.
    if any(
        existing.get("id") == asset_id and existing.get("source") == source
        for existing in artwork_list
    ):
        return

    artwork_list.append(asset)


def executable_files(root: Path) -> list[Path]:
    executables = []

    for path in root.rglob("*"):
        if not path.is_file():
            continue

        exec_bits = path.stat().st_mode & (stat.S_IXUSR | stat.S_IXGRP)
        if exec_bits:
            executables.append(path.relative_to(root))
    return executables


async def get_asset_file(client: SteamGridDBClient, art: dict) -> Path | None:
    if art.get("source") == "local":
        if art.get("kind") == "exe":
            return extract_exe_icon(art["path"])
        return art["path"]

    path = IMAGE_CACHE / f"{art['id']}{ImageExtension.PNG}"

    if not path.exists():
        urls = art.get("urls", [art["url"]])

        for url in urls:
            result = await client.download_file(
                url,
                path,
            )

            if result["success"]:
                return path

        return None

    return path


def json_default(obj):
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def classify_grid(grid: dict) -> ArtworkType:
    width = grid["width"]
    height = grid["height"]

    ratio = width / height

    if ratio < 0.9:
        return ArtworkType.GRID_PORTRAIT

    if ratio <= 1.1:
        return ArtworkType.GRID_SQUARE

    return ArtworkType.GRID_LANDSCAPE


def preferred_language(images: dict[str, str]) -> tuple[str, str] | None:
    """
    Returns (language, filename) for the preferred image.

    Preference:
        english -> en -> first available
    """
    if not images:
        return None

    for lang in ("english", "en"):
        if lang in images:
            return lang, images[lang]

    lang = next(iter(images))
    return lang, images[lang]


FALLBACKS: dict[str, list[dict[str, str]]] = {
    "library_capsule": [
        {
            "thumb": "library_600x900" + ImageExtension.JPG,
            "full": "library_600x900_2x" + ImageExtension.JPG,
        }
    ],
    "header_image": [
        {
            "thumb": "header" + ImageExtension.JPG,
            "full": "header" + ImageExtension.JPG,
        }
    ],
    "library_hero": [
        {
            "thumb": "library_hero" + ImageExtension.JPG,
            "full": "library_hero_2x" + ImageExtension.JPG,
        },
        {  # when hero is missing we could use this.
            "thumb": "page_bg_raw" + ImageExtension.JPG,
            "full": "page_bg_raw" + ImageExtension.JPG,
        },
    ],
    "library_logo": [
        {
            "thumb": "logo" + ImageExtension.PNG,
            "full": "logo_2x" + ImageExtension.PNG,
        }
    ],
}


def steam_art_urls(
    metadata: dict | None,
) -> dict[ArtworkType, list[SteamArtwork]]:
    """
    Returns:
    {
        "grid": ("english", "https://..."),
        "hero": ("english", "https://..."),
        "logo": ("english", "https://...")
    }
    """

    art: dict[ArtworkType, list[SteamArtwork]] = {
        ArtworkType.GRID_PORTRAIT: [],
        ArtworkType.GRID_SQUARE: [],
        ArtworkType.GRID_LANDSCAPE: [],
        ArtworkType.HERO: [],
        ArtworkType.LOGO: [],
        ArtworkType.ICON: [],
    }

    if not metadata:
        return art

    app = metadata.get("external_platform_data", {}).get("steam", [{}])[0]

    appid = app.get("id")
    meta = app.get("metadata", {})

    if not appid:
        return art

    def build(steam_type: str) -> list[SteamArtwork] | None:
        data = meta.get(f"{steam_type}_full")
        icon = meta.get("icon")
        clienticon = meta.get("clienticon")

        language = "english"
        candidates: list[tuple[str, str]] = []

        if steam_type == "icon":
            if clienticon:
                url = f"{STEAM_CLIENTICON_CDN}{appid}/{clienticon}{ImageExtension.ICO}"
                candidates.append((url, url))

            if icon:
                url = f"{STEAM_ICON_CDN}{appid}/{icon}{ImageExtension.JPG}"
                candidates.append((url, url))

        elif isinstance(data, dict):
            thumb = preferred_language(data.get("image", data))
            full = preferred_language(data.get("image2x", data.get("image", data)))

            if thumb:
                language, thumb_file = thumb

                if full is None:
                    full_file = thumb_file
                else:
                    _, full_file = full

                thumb_url = f"{STEAM_STORE_CDN}{appid}/{thumb_file}"
                full_url = f"{STEAM_STORE_CDN}{appid}/{full_file}"
                candidates.append((thumb_url, full_url))

        # Always append fallbacks after metadata artwork.
        if steam_type != "icon":
            for fallback in FALLBACKS.get(steam_type, []):
                thumb_url = f"{STEAM_STORE_CDN}{appid}/{fallback['thumb']}"
                full_url = f"{STEAM_STORE_CDN}{appid}/{fallback['full']}"
                candidates.append((thumb_url, full_url))

        if not candidates:
            return None

        mtime = meta.get("store_asset_mtime")
        artwork = []
        seen: set[str] = set()

        for thumb_url, full_url in candidates:
            if full_url in seen:
                continue

            seen.add(full_url)

            if mtime:
                thumb_url += f"?t={mtime}"
                full_url += f"?t={mtime}"

            artwork.append(
                {
                    "id": sha1(full_url.encode()).hexdigest(),
                    "language": language,
                    "thumb": thumb_url,
                    "url": full_url,
                    "urls": [full_url],
                    "author": {
                        "name": "Steam",
                        "avatar": None,
                        "steam64": None,
                    },
                    "style": "official",
                    "source": "steam",
                }
            )

        return artwork

    art[ArtworkType.GRID_PORTRAIT] = build("library_capsule") or []
    art[ArtworkType.GRID_LANDSCAPE] = build("header_image") or []
    art[ArtworkType.HERO] = build("library_hero") or []
    art[ArtworkType.LOGO] = build("library_logo") or []
    art[ArtworkType.ICON] = build("icon") or []
    return art


def add_dict(parts: list[str], data: dict, prefix: str = ""):
    for key, value in data.items():
        name = f"{prefix}.{key}" if prefix else key

        if isinstance(value, dict):
            add_dict(parts, value, name)

        elif isinstance(value, list):
            if not value:
                parts.append(f"{name}: []")
                continue

            for i, item in enumerate(value):
                if isinstance(item, dict):
                    add_dict(parts, item, f"{name}[{i}]")
                elif isinstance(item, list):
                    parts.append(f"{name}[{i}]: {len(item)} items")
                else:
                    parts.append(f"{name}[{i}]: {item}")

        else:
            parts.append(f"{name}: {value}")


def launcher_signature(path: str | Path) -> tuple[str, str]:
    path = Path(normalize_exe(str(path)))

    if len(path.parts) >= 2:
        return (
            path.parent.name.casefold(),
            path.name.casefold(),
        )

    return ("", path.name.casefold())


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}

    with CONFIG_FILE.open("rb") as f:
        return tomllib.load(f)


def save_config(config: dict):
    if tomli_w is None:
        raise RuntimeError("tomli-w is required to save config.")

    with CONFIG_FILE.open("wb") as f:
        tomli_w.dump(config, f)


async def get_vndb_api_token(app) -> str | None:
    config = load_config()
    key = config.get("vndb", {}).get("api_token", "").strip()

    if key:
        return key

    while True:
        choice = await app.push_screen_wait(
            OptionDialog(
                title="VNDB",
                message=("A VNDB API token is required.\n\nOpen the API token page?"),
                options=[
                    ("Open", "open"),
                    ("Paste Token", "paste"),
                    ("Cancel", "cancel"),
                ],
            )
        )

        if choice is None or choice == "cancel":
            return None

        if choice == "open":
            webbrowser.open("https://vndb.org/u/tokens")
            continue

        if choice == "paste":
            result = await app.push_screen_wait(
                InputDialog(
                    title="VNDB API Token",
                    prompt="Paste your API token:",
                    placeholder="Paste API token...",
                    password=False,
                )
            )
            key = result.value.strip() if result and result.value else ""

            if not key:
                app.notify(
                    "API token cannot be empty.",
                    severity="warning",
                )
                continue

            config.setdefault("vndb", {})["api_token"] = key
            save_config(config)

            return key


def extract_exe_icon(exe: Path) -> Path | None:
    try:
        if not exe.is_file():
            return None

        key = hashlib.sha1(
            f"{exe.resolve()}:{exe.stat().st_mtime_ns}".encode()
        ).hexdigest()

        def image_width(path: Path) -> int:
            with Image.open(path) as img:
                return img.width

        out_dir = ICON_CACHE / key
        out_dir.mkdir(parents=True, exist_ok=True)

        pngs = list(out_dir.glob("*" + ImageExtension.PNG))
        if pngs:
            try:
                return max(pngs, key=image_width)
            except (OSError, ValueError):
                pass

        subprocess.run(
            [
                "wrestool",
                "-x",
                "-t14",
                "-o",
                str(out_dir),
                str(exe),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        ico_files = list(out_dir.glob("*" + ImageExtension.ICO))
        if not ico_files:
            return None

        subprocess.run(
            [
                "icotool",
                "-x",
                "-o",
                str(out_dir),
                *map(str, ico_files),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        pngs = list(out_dir.glob("*" + ImageExtension.PNG))
        if not pngs:
            return None

        return max(pngs, key=image_width)

    except (
        subprocess.CalledProcessError,
        FileNotFoundError,
        PermissionError,
        OSError,
        ValueError,
    ):
        return None


def steam_running() -> bool:
    try:
        subprocess.run(
            ["pgrep", "-x", "steam"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except subprocess.CalledProcessError:
        return False


@dataclass(order=True)
class IconCandidate:
    score: tuple
    path: Path
    kind: Literal[ImageExtension.PNG, ImageExtension.ICO, ImageExtension.EXE]


def find_icons(root: Path) -> list[IconCandidate]:
    icons: list[IconCandidate] = []
    best_exe: Path | None = None

    for current, dirs, files in os.walk(root):
        depth = len(Path(current).relative_to(root).parts)

        if depth > 3:
            dirs[:] = []
            continue

        if should_ignore_game(Path(current), root):
            continue

        for file in sorted(files):
            path = Path(current) / file
            name = file.casefold()

            if path.suffix.casefold() == ImageExtension.EXE:
                name = path.name.casefold()

                if any(bad in name for bad in BAD_LAUNCHER_NAMES):
                    continue

                if best_exe is None or launcher_score(path, root) < launcher_score(
                    best_exe, root
                ):
                    best_exe = path

                continue

            # Best possible icon
            if name == "window_icon" + ImageExtension.PNG:
                icons.append(
                    IconCandidate(
                        score=(0, 0),
                        path=path,
                        kind=ImageExtension.PNG,
                    )
                )
                continue

            # icon_\d+ + ImageExtension.PNG
            if match := re.search(r"icon[-_]?(\d+)", name):
                size = int(match.group(1))
                icons.append(
                    IconCandidate(
                        score=(1, -size),
                        path=path,
                        kind=ImageExtension.PNG,
                    )
                )
                continue

            # Fallbacks
            if name == "icon" + ImageExtension.PNG:
                icons.append(
                    IconCandidate(
                        score=(2, 0),
                        path=path,
                        kind=ImageExtension.PNG,
                    )
                )
                continue

            if name == "android-icon_foreground" + ImageExtension.PNG:
                icons.append(
                    IconCandidate(
                        score=(3, 0),
                        path=path,
                        kind=ImageExtension.PNG,
                    )
                )
                continue

            if path.suffix.casefold() == ImageExtension.ICO:
                icons.append(
                    IconCandidate(
                        score=(4, 0),
                        path=path,
                        kind=ImageExtension.ICO,
                    )
                )

    if best_exe is not None:
        icons.append(
            IconCandidate(
                score=(5,) + launcher_score(best_exe, root),
                path=best_exe,
                kind=ImageExtension.EXE,
            )
        )

    icons.sort()
    return icons


def find_icon(root: Path) -> Path:
    icons = find_icons(root)

    if not icons:
        return ICON_FALLBACK

    best = icons[0]

    if best.kind == ImageExtension.EXE:
        if icon := extract_exe_icon(best.path):
            return icon
        return ICON_FALLBACK

    return best.path


def launcher_score(path: Path, root: Path):
    name = path.stem.casefold()

    score = 0

    if any(bad in name for bad in BAD_LAUNCHER_NAMES):
        score += 1000

    try:
        ext_score = LAUNCHER_EXTS.index(path.suffix)
    except ValueError:
        ext_score = len(LAUNCHER_EXTS)

    return (
        score,
        len(path.relative_to(root).parts),
        ext_score,
        natural_key(path.stem),
    )


def game_appid(game: Game) -> str | None:
    if game.steam_entry is not None:
        appid = game.steam_entry["appid"]
        # new generated app ids are str; when read from the parser they are int
        return appid if isinstance(appid, str) else str(appid & 0xFFFFFFFF)

    if game.launcher is None:
        return None
    if is_steam_launcher(game.launcher):
        return steam_appid_from_uri(game.launcher)

    return generate_appid(Path(game.launcher), game.name)


def shortcuts_path(user: SteamUser) -> Path:
    return STEAM_DIR / "userdata" / user.userdata_id / "config" / "shortcuts.vdf"


def steam_grid_dir(user: SteamUser) -> Path:
    return STEAM_DIR / "userdata" / user.userdata_id / "config" / "grid"


def load_shortcuts(user: SteamUser) -> dict:
    path = shortcuts_path(user)

    if not path.exists():
        return {"shortcuts": {}}

    try:
        with path.open("rb") as f:
            return vdf.binary_load(f)
    except (OSError, ValueError):
        return {"shortcuts": {}}


def save_shortcuts(user: SteamUser, data: dict):
    path = shortcuts_path(user)

    path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        dir=path.parent,
        delete=False,
    ) as tmp:
        vdf.binary_dump(data, tmp)
        tmp_path = Path(tmp.name)

    tmp_path.replace(path)


def normalize_exe(exe: str) -> str:
    exe = exe.strip()

    if not exe:
        return ""

    # Strip surrounding quotes only.
    if len(exe) >= 2 and exe[0] == exe[-1] and exe[0] in "\"'":
        exe = exe[1:-1]

    if exe.startswith("env "):
        parts = shlex.split(exe)

        for part in parts[1:]:
            if "=" not in part:
                return part

        return ""

    return exe


def shortcut_target(entry: dict) -> str:
    exe = normalize_exe(entry.get("Exe") or entry.get("exe") or "")

    launch_options = entry.get("LaunchOptions") or entry.get("launchoptions") or ""

    # Old shortcuts:
    # Exe = "bash"
    # LaunchOptions = "/path/to/game.sh"
    if Path(exe).name == "bash":
        try:
            args = shlex.split(launch_options)
        except ValueError:
            return exe

        for arg in args:
            if arg.endswith(".sh"):
                return normalize_exe(arg)

    return exe


def find_existing_shortcuts(
    entries: dict,
    game: Game,
) -> list[str]:
    target_identity = game_launch_identity(game)

    if target_identity is not None:
        matches = []

        for key, entry in entries.items():
            directory = shortcut_game_directory(entry)

            if directory is None:
                continue

            existing_identity = game_identity(Game(name="", path=directory))

            if existing_identity == target_identity:
                matches.append(key)

        if matches:
            return matches

    # Fall back to matching game name, but only if unambiguous.
    target_name = game.name.casefold()

    matches = [
        key
        for key, entry in entries.items()
        if (entry.get("AppName") or entry.get("appname") or "").casefold()
        == target_name
    ]

    return matches if len(matches) == 1 else []


def generate_appid(exe: Path, stable_id: str) -> str:
    crc = binascii.crc32((str(exe) + stable_id).encode("utf-8")) | 0x80000000

    return str(crc & 0xFFFFFFFF)


def fmt_time(ts: float) -> str:
    if not ts:
        return "Never"

    return time.strftime(
        "%Y-%m-%d %H:%M:%S",
        time.localtime(ts),
    )


def natural_key(s: str):
    return [
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", s)
    ]


def launch_path(game: Game) -> Path:
    if game.path is None:
        raise RuntimeError(f"Game has no path: {game.name}")

    shared = get_location("Shared")

    if shared.local_path is not None and (
        (
            shared.remote_path is not None
            and game.path.is_relative_to(shared.remote_path)
        )
        or game.path.is_relative_to(shared.local_path)
    ):
        local = shared.local_path / game.name

        if local.is_dir():
            return local

    return game.path


def gen_launchers(
    game: Game,
    max_depth: int = 3,
    cancel_event: Event | None = None,
) -> Iterator[Path | str]:
    def cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    path = launch_path(game)

    if not can_add_to_steam(game):
        metadata = game.steamgriddb.get("steam_platform_data") or {}
        steam_apps = metadata.get("external_platform_data", {}).get("steam", [])
        appid = steam_apps[0].get("id") if steam_apps else None

        if not appid:
            appid = game_appid(game)

        if appid:
            yield f"steam://rungameid/{appid}"

    if cancelled() or not path.is_dir():
        return

    def scan(directory: Path, depth: int) -> Iterator[Path]:
        if cancelled():
            return

        for p in directory.iterdir():
            if cancelled():
                return

            if p.is_file():
                if p.suffix.lower() in LAUNCHER_EXTS or (
                    p.stat().st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                ):
                    yield p

            elif p.is_dir() and depth < max_depth:
                yield from scan(p, depth + 1)

    yield from scan(path, 1)


def find_launchers(
    game: Game,
    max_depth: int = 3,
) -> list[Path | str]:
    path = launch_path(game)

    launchers = list(gen_launchers(game, max_depth))

    path_launchers = [launcher for launcher in launchers if isinstance(launcher, Path)]

    steam_launchers = [
        launcher for launcher in launchers if is_steam_launcher(launcher)
    ]

    path_launchers.sort(key=lambda p: launcher_score(p, path))

    return path_launchers + steam_launchers


def find_launcher(game: Game) -> Path | str | None:
    launchers = find_launchers(game)

    if not launchers:
        return None

    return launchers[0]


def game_list_changed(
    path: Path | None,
    games: list[Game],
) -> bool:
    cached = {(g.name, g.mtime) for g in games}

    if path is None or not Path(path).exists():
        return False

    current = {
        (p.name, p.stat().st_mtime)
        for p in path.iterdir()
        if p.is_dir() and not should_ignore_game(p, path)
    }

    return current != cached


def local_sync_status() -> str:
    shared = get_location("Shared")
    local_path = shared.local_path
    remote_path = shared.remote_path

    if local_path is None or not local_path.exists():
        return "Local copy missing"

    if remote_path is None or not remote_path.exists():
        return "Remote storage offline"

    try:
        remote = {p.name for p in remote_path.iterdir() if p.is_dir()}
        local = {p.name for p in local_path.iterdir() if p.is_dir()}
    except FileNotFoundError:
        return "Remote storage offline"

    if remote == local:
        return "In Sync"

    missing = remote - local
    extra = local - remote

    parts = []
    if missing:
        parts.append(f"{len(missing)} missing locally")
    if extra:
        parts.append(f"{len(extra)} extra locally")

    return ", ".join(parts)


def fmt_size(num: int) -> str:
    gb = num / (1024**3)

    if gb >= 1:
        return f"{gb:.1f} GB"

    mb = num / (1024**2)

    if mb >= 1:
        return f"{mb:.1f} MB"
    if mb == 0:
        return ""

    return f"{num / 1024:.1f} KB"


def dir_size(path: Path) -> int:
    try:
        if path.is_symlink():
            target = path.resolve()
        else:
            target = path

        exclude_args = [
            *(f"--exclude={name}" for name in IGNORE_DIRS),
            "--exclude=*/.*",
        ]

        result = subprocess.run(
            ["du", "-sb", *exclude_args, str(target)],
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode != 0:
            # du often returns non-zero on permission errors; still try to parse what we got
            if result.stdout.strip():
                return int(result.stdout.split()[0])
            raise RuntimeError(
                f"du failed (exit {result.returncode}): {result.stderr.strip() or 'unknown error'}"
            )

        return int(result.stdout.split()[0])

    except Exception as e:
        # Keep the message short and free of [ ] characters
        raise RuntimeError(f"dir_size failed for {path}: {type(e).__name__}") from e


def latest_activity(path: Path) -> float:
    newest = 0.0

    try:
        for p in path.rglob("*"):
            if not p.is_file():
                continue

            if should_ignore_game(p, path):
                continue

            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue

            # prioritize launcher-like files
            if p.suffix in LAUNCHER_EXTS:
                newest = max(newest, mtime + 10_000_000)

            newest = max(newest, mtime)

    except OSError:
        pass

    if newest > 0:
        return newest

    return path.stat().st_mtime


# ============================================================
# UI
# ============================================================


class GameRow(ListItem):
    def __init__(self, game: Game):
        set_width: int = 80
        self.game = game

        self.icon = AutoImage()
        self.icon.styles.width = 2
        self.icon.styles.height = 1
        self.icon.styles.max_width = 2
        self.icon.styles.max_height = 1
        self.icon.styles.flex_shrink = 0

        self.label = Static()
        self.label.styles.width = "1fr"
        row = Horizontal(self.icon, self.label)
        row.styles.height = 1
        row.styles.width = set_width
        row.styles.max_height = 1

        super().__init__(row)
        self.styles.width = set_width
        self.refresh_row()

    async def refresh_icon(self):
        """Update the row icon from the game's configured icon path."""
        icon_path = await self.app.get_selected_artwork_path(
            self.game,
            ArtworkType.ICON,
            installed_only=True,
        )

        if not icon_path:
            self.icon.image = None
            return

        icon_path = Path(icon_path)

        if not icon_path.is_file():
            self.icon.image = None
            return

        if icon_path.suffix.lower() == ImageExtension.EXE:
            icon_path = await to_thread(extract_exe_icon, icon_path)

        self.icon.image = icon_path if icon_path else None

    def refresh_tooltip(self):
        parts = [
            f"Launcher: {self.game.launcher}" if self.game.launcher else None,
            "Steam: Duplicate shortcut exists" if self.game.duplicate_steam else None,
            "Steam: Shortcut path is out of date" if self.game.steam_broken else None,
            "Steam: Shortcut is synchronized"
            if self.game.in_steam
            and not self.game.steam_broken
            and not self.game.duplicate_steam
            else None,
            f"Last Played: {fmt_time(self.game.last_played)}",
            f"Modified:    {fmt_time(self.game.mtime)}",
        ]
        if self.game.steam_entry:
            parts.append(f"Steam Exe: {self.game.steam_entry['Exe']}")
            launch_options = self.game.steam_entry.get("LaunchOptions", "")
            if launch_options:
                parts.append(f"Steam LaunchOptions: {launch_options}")
            if self.game.steamgriddb:
                sgdb_game = self.game.steamgriddb.get("game")

                if isinstance(sgdb_game, dict):
                    name = self.game.steamgriddb.get("name") or sgdb_game.get("name")
                else:
                    name = self.game.steamgriddb.get("name")

                if name:
                    parts.append(f"SteamGridDB: {name}")

                if search := self.game.steamgriddb.get("search"):
                    parts.append(f"SteamGridDB Search: {search}")

                game_id = self.game.steamgriddb.get("id")

                if game_id is None and isinstance(sgdb_game, dict):
                    game_id = sgdb_game.get("id")

                if game_id:
                    parts.append(f"SteamGridDB ID: {game_id}")

        self.tooltip = "\n".join(part for part in parts if part)

    def build_line(self) -> str:

        # mark = "[*]" if self.game.selected else "[ ]"
        mark = "[🗸]" if self.game.selected else "[ ]"

        date = time.strftime("%Y-%m-%d", time.localtime(self.game.mtime))

        size = fmt_size(self.game.size)

        if not can_add_to_steam(self.game):
            steam = "X"
        elif self.game.duplicate_steam:
            steam = "D"
        elif self.game.steam_broken:
            steam = "!"
        elif self.game.in_steam:
            steam = "S"
        else:
            steam = " "

        if self.game.steamgriddb:
            sgdb_game = self.game.steamgriddb.get("game")

            if isinstance(sgdb_game, dict) and sgdb_game.get("id"):
                sgdb = "G"
            elif self.game.steamgriddb.get("id"):
                sgdb = "g"
            else:
                sgdb = " "
        else:
            sgdb = " "

        return f"{mark} {self.game.name:<45.45} {size:>9} {date} {steam} {sgdb} "

    def refresh_row(self):
        asyncio.create_task(self.refresh_icon())

        self.label.update(self.build_line())

        self.refresh_tooltip()


# ============================================================
# APP
# ============================================================


class GameArchiver(App):
    CSS = """
    Screen {
        layout: vertical;
    }

    #move-mode {
        height: 1fr;
        display: none;
    }

    #library-mode {
        height: 1fr;
        display: none;
    }

    #library-details {
        width: 2fr;
        height: 1fr;
        border: solid white;
        padding: 1;
    }

    ListView {
        width: 1fr;
        border: solid white;
        overflow-x: auto;          /* enable horizontal scroll */
    }

    #status {
        height: 3;
        padding: 0 1;
    }

    .pane-title {
        dock: top;
        height: 1;
        padding-left: 1;
        text-style: bold;
    }

    #details-artwork {
        width: 100%;
        height: 0;
        padding: 0;
        align: center top;
    }

    #library-art {
        padding: 0;
        margin: 0;
        width: auto;
        height: auto;
    }

    #search-bar {
        height: 3;
        padding: 0 1;
        align: center middle;
    }

    #game-search {
        width: 1fr;
    }

    #group-matches {
        width: auto;
        margin-left: 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header()

        with Horizontal(id="move-mode"):
            self.shared_view = ListView(id="shared")
            yield self.shared_view

            self.archived_view = ListView(id="archived")
            yield self.archived_view

        with Horizontal(id="library-mode"):
            self.library_view = ListView(id="library")
            yield self.library_view

            with Vertical(id="library-details"):
                yield Vertical(id="details-artwork")
                yield Static("", id="details-info")

        with Horizontal(id="search-bar"):
            yield Input(placeholder="Regex search...", id="game-search")
            yield Checkbox("Not in Steam", id="filter-not-steam")
            yield Checkbox("No SGDB match", id="filter-no-sgdb")
            yield Checkbox("Group by match", id="group-matches")

        yield Static(id="status")

        yield Footer(show_command_palette=False, compact=True)

    def on_mount(self):
        self.update_mode_display()
        self.shared_view.border_title = " Shared / Synced "
        self.archived_view.border_title = " Archived / Remote "
        self.library_view.border_title = " Library / All "

        self.shared_view = self.query_one("#shared", ListView)
        self.archived_view = self.query_one("#archived", ListView)
        self.library_view = self.query_one("#library", ListView)

        self.load_game_data()
        self.action_refresh()

        self.current_view().focus()

        self.set_interval(
            SYNC_STATUS_INTERVAL_SEC,
            self.refresh_sync_status,
        )
        self.set_interval(
            RESCAN_INTERVAL_SEC,
            self.refresh_if_changed,
        )

        if len(self.steam_users) > 1:
            self.bind("[", "prev_steam_user", "Steam-")
            self.bind("]", "next_steam_user", "Steam+")

        self.refresh_bindings()
        self.save_game_data()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "game-search":
            return

        if self.search_timer:
            self.search_timer.stop()

        self.search_timer = self.set_timer(
            0.5,
            self.refresh_views,
        )

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id == "group-matches":
            self.sort_games(self.games)
            self.sort_games(self.shared_games)
            self.sort_games(self.archived_games)

        self.refresh_views()

    def update_mode_display(self):
        move_mode = self.query_one("#move-mode")
        library_mode = self.query_one("#library-mode")
        search = self.query_one("#game-search", Input)

        move_mode.display = self.app_mode == AppMode.MOVE
        library_mode.display = self.app_mode == AppMode.LIBRARY
        view = self.current_view()

        if view and not search.has_focus:
            self.set_focus(view)

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("tab", "switch_pane", "Switch"),
        Binding("space", "toggle", "Toggle"),
        Binding("a", "auto_select", "Auto"),
        Binding("m", "move_selected", "Move"),
        Binding("r", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
        Binding("l", "launch", "Launch"),
        Binding("alt+l", "choose_launcher", "Change Launcher"),
        Binding("o", "open_folder", "Open"),
        Binding("p", "open_remote_folder", "Open Remote"),
        Binding("t", "terminal", "Terminal"),
        Binding("s", "remote_terminal", "SSH"),
        Binding("u", "sync_shortcut", "Sync Steam"),
        Binding("ctrl+a", "select_all", "Select All"),
        Binding("ctrl+shift+a", "select_none", "Select None"),
        Binding("g", "download_steamgriddb", "SteamGridDB"),
        Binding("alt+p", "open_proton_prefix", "Open Proton Prefix"),
        Binding("f2", "switch_mode", "Switch Mode"),
        Binding("delete", "delete_game", "Delete"),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        config = load_config()
        try:
            self.app_mode = AppMode(
                config.get("ui", {}).get("last_mode", AppMode.MOVE.value)
            )
        except ValueError:
            self.app_mode: AppMode = AppMode.MOVE
        self.computing_sizes: bool = False
        self.detecting_steam: bool = False
        self.moving: bool = False
        self.dialog_open: bool = False
        self.game_rows = {}
        self.sync_status: str = "Detecting"
        self.storage_online: bool = False
        self.search_timer: Timer | None = None
        self._artwork_generation: int = 0
        self._artwork_timer: Timer | None = None

        self.games: list[Game] = []
        self.shared_games: list[Game] = []
        self.archived_games: list[Game] = []
        self.library_only_games: list[Game] = []
        self.unavailable_games: list[Game] = []
        self.steam_users = get_steam_users()
        self.selected_steam_user = 0
        self.highlighted_game_path: Path | None = None

    async def clear_library_artwork(self, generation: int):
        if generation != self._artwork_generation:
            return

        container = self.query_one("#details-artwork", Vertical)

        await container.remove_children()

        if generation != self._artwork_generation:
            return

        container.styles.height = 0

    def artwork_update_is_current(self, game: Game, generation: int) -> bool:
        return (
            self.app_mode == AppMode.LIBRARY
            and self.highlighted_game_path == game.path
            and self._artwork_generation == generation
        )

    async def update_library_artwork(self, game: Game, generation: int):
        if not self.artwork_update_is_current(game, generation):
            return

        artwork = None
        artwork_type = None

        for candidate_type in (
            ArtworkType.HERO,
            ArtworkType.GRID_LANDSCAPE,
            ArtworkType.GRID_PORTRAIT,
            ArtworkType.GRID_SQUARE,
            ArtworkType.ICON,
        ):
            path = await self.get_selected_artwork_path(game, candidate_type)

            if not self.artwork_update_is_current(game, generation):
                return

            if path and path.is_file():
                artwork = path
                artwork_type = candidate_type
                break

        if not self.artwork_update_is_current(game, generation):
            return

        container = self.query_one("#details-artwork", Vertical)

        await container.remove_children()

        if not self.artwork_update_is_current(game, generation):
            return

        if not artwork:
            container.styles.height = 0
            return

        if artwork.suffix.lower() == ".exe":
            artwork = await to_thread(extract_exe_icon, artwork)

            if not self.artwork_update_is_current(game, generation):
                return

        if artwork is None or not artwork.is_file():
            container.styles.height = 0
            return

        try:
            with Image.open(artwork) as img:
                img.verify()

            with Image.open(artwork) as img:
                image_width, image_height = img.size

        except (UnidentifiedImageError, OSError):
            debug_log(f"Invalid artwork image: {artwork}")
            container.styles.height = 0
            return

        if artwork_type is ArtworkType.HERO:
            width = container.size.width or 1
            height = round((width * image_height / image_width) / 2)
            container.styles.height = height
        else:
            container.styles.height = 10

        if not self.artwork_update_is_current(game, generation):
            return

        image = AutoImage(str(artwork), id="library-art")
        image.can_focus = False

        await container.mount(image)

    def sort_games(self, games: list[Game]):
        group_matches = self.query_one("#group-matches", Checkbox).value

        if group_matches:
            games.sort(
                key=lambda g: (
                    not isinstance(g.steamgriddb.get("game"), dict),
                    ((g.steamgriddb.get("game") or {}).get("name") or "").casefold(),
                    -g.last_played,
                    natural_key(g.name),
                )
            )
        else:
            games.sort(
                key=lambda g: (
                    -g.last_played,
                    natural_key(g.name),
                )
            )

    def scan_game_locations(
        self,
        locations: tuple[GameLocation, ...],
        remote_available: bool,
    ) -> dict[str, list[Game]]:
        games = {}

        for location in locations:
            base = location.scan_path(remote_available)

            if base is None:
                continue

            games[location.name] = self.scan_games(base)

        return games

    def scan_games(self, base: Path | None) -> list[Game]:
        games = []

        if base is None:
            return games

        if not base.exists():
            return games

        for item in sorted(base.iterdir()):
            if not item.is_dir() or should_ignore_game(Path(item), base):
                continue

            game = Game(name=item.name, path=item)

            games.append(game)

        self.sort_games(games)
        return games

    def merge_games(
        self,
        existing: list[Game],
        scanned: list[Game],
    ) -> tuple[list[Game], list[Game]]:
        existing_map = {
            game_identity(game): game
            for game in existing
            if game_identity(game) is not None
        }

        scanned_map = {
            game_identity(game): game
            for game in scanned
            if game_identity(game) is not None
        }

        found = []
        not_found = []

        for identity, scanned_game in scanned_map.items():
            existing_game = existing_map.get(identity)

            if existing_game is None:
                found.append(scanned_game)
            else:
                # Preserve metadata and database ID.
                existing_game.path = scanned_game.path
                existing_game.name = scanned_game.name
                found.append(existing_game)

        not_found = [
            game
            for identity, game in existing_map.items()
            if identity not in scanned_map
        ]

        self.sort_games(found)
        self.sort_games(not_found)

        return found, not_found

    def load_game_data(self) -> None:
        try:
            with get_db() as db:
                rows = db.execute(
                    """
                    SELECT
                        g.id,
                        g.name,
                        g.path,
                        g.size,
                        g.mtime,
                        g.file_count,
                        g.last_played,
                        g.launcher,
                        g.gamescope_enabled,

                        s.steam_key,
                        s.steam_entry,
                        s.in_steam,
                        s.steam_broken,
                        s.duplicate_steam,

                        sg.sgdb_id,
                        sg.search AS sgdb_search,
                        sg.game_data,
                        sg.steam_platform_data,
                        sg.cached

                    FROM games g

                    LEFT JOIN steam_games s
                        ON s.game_id = g.id

                    LEFT JOIN steamgriddb_games sg
                        ON sg.game_id = g.id

                    ORDER BY g.id
                    """
                ).fetchall()

                games = []
                seen_identities = {}

                for row in rows:
                    game = Game(
                        id=row["id"],
                        name=row["name"],
                        path=Path(row["path"]) if row["path"] else None,
                        size=row["size"],
                        mtime=row["mtime"],
                        file_count=row["file_count"],
                        last_played=row["last_played"],
                        launcher=(
                            row["launcher"]
                            if is_steam_launcher(row["launcher"])
                            else Path(row["launcher"])
                            if row["launcher"] is not None
                            else None
                        ),
                        gamescope_enabled=bool(row["gamescope_enabled"]),
                    )

                    identity = game_identity(game)

                    if identity is not None:
                        existing_id = seen_identities.get(identity)

                        if existing_id is not None:
                            debug_log(
                                f"Skipping duplicate game ID {row['id']} "
                                f"({game.name!r}, {game.path}); "
                                f"keeping ID {existing_id}"
                            )
                            continue

                        seen_identities[identity] = row["id"]

                        location = get_game_location(game.path)

                        if location is not None and location.remote_path is not None:
                            relative_path = next(
                                (
                                    game.path.relative_to(base)
                                    for base in (
                                        location.local_path,
                                        location.remote_path,
                                    )
                                    if base is not None
                                    and game.path.is_relative_to(base)
                                ),
                                None,
                            )

                            if relative_path is not None:
                                remote_path = location.remote_path / relative_path

                                if remote_path.is_dir() and game.path != remote_path:
                                    debug_log(
                                        f"Updating game ID {row['id']} path: "
                                        f"{game.path} -> {remote_path}"
                                    )

                                    db.execute(
                                        "UPDATE games SET path = ? WHERE id = ?",
                                        (str(remote_path), row["id"]),
                                    )

                                    game.path = remote_path

                    if row["steam_key"] is not None:
                        game.steam_key = row["steam_key"]

                    if row["steam_entry"] is not None:
                        game.steam_entry = json.loads(row["steam_entry"])

                    game.in_steam = bool(row["in_steam"])
                    game.steam_broken = bool(row["steam_broken"])
                    game.duplicate_steam = bool(row["duplicate_steam"])

                    game_id = row["id"]
                    selected = load_selected_artwork(db, game_id)

                    has_sgdb_data = (
                        row["sgdb_id"] is not None
                        or row["sgdb_search"] is not None
                        or row["game_data"] is not None
                    )
                    has_selected_artwork = any(
                        artwork is not None for artwork in selected.values()
                    )

                    if has_sgdb_data or has_selected_artwork:
                        game.steamgriddb = {
                            "search": row["sgdb_search"],
                            "game": (
                                json.loads(row["game_data"])
                                if row["game_data"] is not None
                                else None
                            ),
                            "steam_platform_data": (
                                json.loads(row["steam_platform_data"])
                                if row["steam_platform_data"] is not None
                                else None
                            ),
                            "cached": row["cached"],
                            "art": {},
                            "selected": selected,
                        }

                        (
                            game.steamgriddb["art"],
                            game.steamgriddb["artwork_cached"],
                        ) = load_steamgriddb_artwork(db, game_id)

                    games.append(game)

                self.sort_games(games)

                shared_games = []
                archived_games = []
                library_only_games = []

                for game in games:
                    location = get_game_location(game.path) if game.path else None

                    if location is None:
                        library_only_games.append(game)
                        continue

                    if location.name == "Shared":
                        shared_games.append(game)
                    elif location.name == "Archived":
                        archived_games.append(game)
                    else:
                        library_only_games.append(game)

                self.shared_games = shared_games
                self.archived_games = archived_games
                self.library_only_games = library_only_games

                self.games = games

        except Exception as e:  # noqa: BLE001 - Prevent database loading errors from crashing startup.
            self.notify(f"Error loading game data: {type(e).__name__}: {e}")

    def save_game_data(self) -> None:
        debug_log("DEBUG: save_game_data called")

        try:
            with get_db() as db:
                games = (
                    self.shared_games + self.archived_games + self.library_only_games
                )

                reconcile_game_paths(db, games)

                for game in games:
                    save_game(db, game)

                db.commit()

        except Exception as e:
            self.notify(f"Error saving game data: {type(e).__name__}: {e}")
            raise

    async def get_selected_artwork_path(
        self,
        game: Game,
        artwork_type: ArtworkType,
        installed_only: bool = False,
    ) -> Path | None:
        selected = get_selected_artwork(game, artwork_type)

        if not selected:
            return None

        if installed_only and selected.get("installed_path") is None:
            await self.install_artwork(
                game, artwork_type, selected.get("downloaded_path")
            )

        path = (
            selected.get("installed_path")
            if installed_only
            else selected.get("installed_path") or selected.get("downloaded_path")
        )

        return Path(path) if path is not None else None

    async def make_shortcut(self, game: Game) -> dict | None:
        if is_steam_launcher(game.launcher):
            return None
        launcher = game.launcher
        assert launcher is not None

        try:
            info = get_launch_info(game, for_steam=True)
        except (RuntimeError, OSError) as e:
            self.notify(str(e))
            return None

        icon_path = await self.get_selected_artwork_path(
            game,
            ArtworkType.ICON,
            installed_only=True,
        )

        return {
            "appid": generate_appid(launcher, game.name),
            "AppName": game.name,
            "Exe": f'"{info.cmd[0]}"',
            "StartDir": f'"{info.cwd}"',
            "icon": str(icon_path) if icon_path is not None else "",
            "ShortcutPath": "",
            "LaunchOptions": " ".join(shlex.quote(arg) for arg in info.cmd[1:]),
            "IsHidden": 0,
            "AllowDesktopConfig": 1,
            "AllowOverlay": 1,
            "OpenVR": 0,
            "Devkit": 0,
            "DevkitGameID": "",
            "LastPlayTime": 0,
            "tags": {
                "0": "Non-Steam",
            },
        }

    async def sync_shortcut(self, game: Game) -> bool:

        # Don't create shortcuts for games already installed through Steam.
        if not can_add_to_steam(game):
            return False

        if is_steam_launcher(game.launcher):
            return False

        user = self.current_steam_user()
        if user is None:
            return False

        if game.launcher is None:
            return False

        shortcuts = load_shortcuts(user)
        entries = shortcuts["shortcuts"]

        key = game.steam_key

        game.duplicate_steam = False
        game.steam_broken = False

        # A cached key may no longer exist.
        if key is not None and key not in entries:
            key = None
            game.steam_key = None

        if key is None:
            matches = find_existing_shortcuts(entries, game)

            if matches:
                key = matches[0]
                game.steam_key = key
                game.duplicate_steam = len(matches) > 1

        # Remember the current appid in case it changes.
        old_appid = game_appid(game) if key is not None else None

        # Build the shortcut as it should exist now.
        desired = await self.make_shortcut(game)

        self.save_game_data()

        changed = False

        if key is None:
            # This game is not in Steam yet. Create a new shortcut.
            index = max((int(k) for k in entries), default=-1) + 1

            key = str(index)
            entries[key] = desired
            changed = True

        else:
            # Update only fields that have changed.
            existing = entries[key]

            for field, value in desired.items():
                if existing.get(field) != value:
                    existing[field] = value
                    changed = True

        # Keep Steam artwork filenames in sync if the appid changed.
        if old_appid is not None:
            changed |= await self.rename_artwork(game, old_appid)

        changed |= purge_duplicate_shortcuts(entries, self.games)

        # Persist changes to shortcuts.vdf.
        if changed:
            save_shortcuts(user, shortcuts)

        # Refresh cached state on the Game object.
        game.steam_key = key
        game.steam_entry = entries.get(key)
        game.in_steam = True

        return changed

    def is_scan_running(self) -> bool:
        if self.detecting_steam:
            self.notify("Please wait for the Steam scan to finish.")
            return True
        if self.computing_sizes:
            self.notify("Please wait for the size scan to finish.")
            return True
        if self.moving:
            self.notify("Please wait for moving to complete.")
            return True
        return False

    def can_modify_steam(self) -> bool:

        if steam_running():
            self.notify("Steam is running. Close Steam before modifying shortcuts.")
            return False
        return not self.is_scan_running()

    def _resolve_steam_entry(
        self,
        game: Game,
        entries: dict[str, dict],
    ) -> tuple[str, dict] | None:
        matches = find_existing_shortcuts(entries, game)

        game.duplicate_steam = len(matches) > 1

        # Prefer the cached shortcut if it is one of the matches.
        if game.steam_key in matches:
            return game.steam_key, entries[game.steam_key]

        if not matches:
            # Still trust an existing cached key.
            if game.steam_key is not None and game.steam_key in entries:
                return game.steam_key, entries[game.steam_key]

            return None

        key = matches[0]
        game.steam_key = key

        return key, entries[key]

    def _set_steam_entry_status(
        self,
        game: Game,
        entry: dict,
    ) -> None:
        game.steam_entry = entry
        game.in_steam = True

    async def update_game_steam_status(
        self,
        game: Game,
        entries: dict[str, dict],
    ):
        if game.launcher is None:
            game.launcher = find_launcher(game)

        if get_selected_artwork(game, ArtworkType.ICON) is None:
            local_icons = await to_thread(find_icons, launch_path(game))
            await self.add_detected_icons(game, local_icons)

        game.in_steam = False
        game.steam_broken = False
        game.duplicate_steam = False
        game.steam_entry = None

        if game.launcher is None:
            return

        result = self._resolve_steam_entry(game, entries)

        if result is None:
            # No matching shortcut was found.
            if game.steam_key is not None:
                game.steam_broken = True
            return

        _, entry = result
        self._set_steam_entry_status(game, entry)

    async def compute_steam_status(self):
        user = self.current_steam_user()

        if user is None:
            return

        shortcuts = load_shortcuts(user)
        entries = shortcuts["shortcuts"]

        games = (
            self.shared_games + self.archived_games + self.library_only_games
            if self.storage_online
            else self.shared_games + self.library_only_games
        )

        for game in games:
            await self.update_game_steam_status(
                game,
                entries,
            )
            self.refresh_game_row(game)

        self.steam_scan_finished()
        self.update_status()

    def current_steam_user(self) -> SteamUser | None:
        if not self.steam_users:
            return None

        return self.steam_users[self.selected_steam_user]

    def refresh_sync_status(self):
        self.sync_status = local_sync_status()
        self.update_status()

    def refresh_if_changed(self):
        if self.dialog_open:
            return

        self.verify_storage()

        if self.is_scan_running():
            return

        for location in GAME_LOCATIONS:
            base = location.scan_path(self.storage_online)

            if base is None:
                continue

            if location.name == "Shared":
                games = self.shared_games
            elif location.name == "Archived":
                games = self.archived_games
            else:
                games = [
                    game
                    for game in self.library_only_games
                    if game.path is not None and game.path.is_relative_to(base)
                ]

            if game_list_changed(base, games):
                self.action_refresh()
                return

        self.refresh_launchers()

    async def refresh_launchers(self):
        if not self.can_modify_steam():
            return

        games = list(self.shared_games)
        if self.storage_online:
            games += self.archived_games

        for game in games:
            # Keep the current launcher if it still exists.
            if is_steam_launcher(game.launcher):
                continue

            if game.launcher is not None and Path(game.launcher).is_file():
                continue
            launcher = find_launcher(game)

            # The game directory may have disappeared if remote storage went offline.
            if launcher is None:
                continue

            if launcher != game.launcher:
                game.launcher = launcher
                if get_selected_artwork(game, ArtworkType.ICON) is None:
                    local_icons = await to_thread(find_icons, launch_path(game))
                    await self.add_detected_icons(game, local_icons)

                if game.in_steam:
                    await self.sync_shortcut(game)

                self.refresh_game_row(game)

    def refresh_game_row(self, game: Game):
        view = self.current_view()
        for row in view.children:
            if row.game is game:
                row.refresh_row()
                self.update_status()
                return

    def update_game_info(self, game: Game):
        path = game.path

        try:
            mtime = path.stat().st_mtime
        except OSError:
            game.size = 0
            game.mtime = 0.0
            return

        file_count = sum(1 for _ in os.scandir(path))

        if game.mtime == mtime and game.file_count == file_count:
            return

        try:
            game.size = dir_size(path)
        except OSError as e:
            self.notify(
                f"Error computing size for {path}: {type(e).__name__}",
                severity="error",
                markup=False,
            )
            game.size = 0

        game.mtime = mtime
        game.file_count = file_count

    async def compute_sizes(self):
        collections = [
            self.shared_games,
            self.library_only_games,
        ]

        if self.storage_online:
            collections.append(self.archived_games)

        for games in collections:
            for game in games:
                await asyncio.to_thread(self.update_game_info, game)
                self.refresh_game_row(game)

            games[:] = [game for game in games if game.size > MIN_GAME_SIZE]

        self.games = (
            self.shared_games + self.archived_games + self.library_only_games
            if self.storage_online
            else self.shared_games + self.library_only_games
        )

        self.sizes_finished()
        self.update_status()
        self.refresh_views()
        self.save_game_data()

    def sizes_finished(self):
        self.computing_sizes = False

    def steam_scan_finished(self):
        self.detecting_steam = False

    def start_scan(self) -> bool:
        self.refresh_bindings()
        self.update_status()
        if self.is_scan_running() or self.dialog_open:
            return False

        self.computing_sizes = True
        self.run_worker(
            self.compute_sizes(),
            name="compute-sizes",
        )
        self.detecting_steam = True
        self.run_worker(
            self.compute_steam_status(),
            name="compute-steam",
        )
        return True

    def verify_storage(self):
        self.storage_online = True

        if not POOL_DIR.is_mount():
            self.notify(f"{POOL_DIR} is not mounted!")
            self.storage_online = False
            return

        for location in get_move_locations(remote_available=True):
            if location.remote_path is None or not location.remote_path.exists():
                self.notify(f"{location.remote_path} is missing!")
                self.storage_online = False

    # ========================================================

    async def on_list_view_highlighted(self, event: ListView.Highlighted):
        row = event.item

        if isinstance(row, GameRow):
            self.highlighted_game_path = row.game.path
        else:
            self.highlighted_game_path = None

        if self.app_mode != AppMode.LIBRARY:
            return

        if isinstance(row, GameRow):
            self.refresh_library_details(row.game)
        else:
            self.refresh_library_details(None)

    def get_row_for_game(self, game: Game) -> GameRow | None:
        return next(
            (
                row
                for row in self.current_view().query(GameRow)
                if (
                    (game.id is not None and row.game.id == game.id)
                    or row.game.path == game.path
                )
            ),
            None,
        )

    def get_highlighted_row(self) -> GameRow | None:
        view = self.current_view()

        if view.index is None:
            return None

        if not (0 <= view.index < len(view.children)):
            view.index = None
            return None

        return view.children[view.index]

    def get_highlighted_game(self) -> Game | None:
        row = self.get_highlighted_row()
        return row.game if row is not None else None

    def refresh_library_details(self, game: Game | None):
        if self._artwork_timer is not None:
            self._artwork_timer.stop()
            self._artwork_timer = None

        self._artwork_generation += 1
        self.update_library_details(game, self._artwork_generation)

    def update_library_details(
        self,
        game: Game | None,
        generation: int,
    ):
        info = self.query_one("#details-info", Static)
        details = self.query_one("#library-details")
        if game is None:
            details.border_title = "Select a game"
            info.update("")

            self.run_worker(
                self.clear_library_artwork(generation),
                group="library-artwork",
            )
            return

        sgdb = game.steamgriddb or {}
        sgdb_game = sgdb.get("game")

        name = sgdb.get("name") or (
            sgdb_game.get("name") if isinstance(sgdb_game, dict) else None
        )

        details.border_title = f"{game.name} ({name})" if name else game.name

        last_played = (
            datetime.fromtimestamp(game.last_played).strftime("%Y-%m-%d %H:%M")
            if game.last_played
            else "Never"
        )

        launcher = (
            "Launch through Steam"
            if is_steam_launcher(game.launcher)
            else Path(game.launcher).name
            if game.launcher
            else "Automatic"
        )

        steam_status = (
            "Broken"
            if game.steam_broken
            else "Installed"
            if game.in_steam
            else "Not installed"
        )

        location = get_game_location(game.path) if game.path else None

        info.update(
            f"Size: {game.size:,} bytes\n"
            f"Files: {game.file_count:,}\n"
            f"Last Played: {last_played}\n"
            f"Launcher: {launcher}\n"
            f"Steam: {steam_status}\n"
            f"Shared/Synced: {game in self.shared_games}\n"
            f"Archived/Remote: {game in self.archived_games}\n"
            f"Game Location: {location.name if location else 'Outside configured locations'}\n"
            f"Gamescope: {'Enabled' if game.gamescope_enabled else 'Disabled'}"
        )

        def update_artwork():
            self._artwork_timer = None
            self.run_worker(
                self.update_library_artwork(game, generation),
                group="library-artwork",
            )

        self._artwork_timer = self.set_timer(0.15, update_artwork)

    def check_action(
        self,
        action: str,
        parameters: tuple[object, ...],
    ) -> bool | None:
        search = self.query_one("#game-search", Input)

        return not search.has_focus

    def filter_games(self, games: list[Game]) -> list[Game]:
        pattern = self.query_one("#game-search", Input).value
        filter_not_steam = self.query_one("#filter-not-steam", Checkbox).value
        filter_no_sgdb = self.query_one("#filter-no-sgdb", Checkbox).value

        try:
            regex = re.compile(pattern, re.IGNORECASE) if pattern else None
        except re.error:
            regex = None

        return [
            game
            for game in games
            if (not filter_not_steam or (not game.in_steam and can_add_to_steam(game)))
            and (
                not filter_no_sgdb
                or not (
                    isinstance(game.steamgriddb.get("game"), dict)
                    and game.steamgriddb["game"].get("id")
                )
            )
            and (
                regex is None
                or regex.search(game.name)
                or regex.search(str(game.path))
                or (
                    isinstance(game.steamgriddb.get("game"), dict)
                    and regex.search(str(game.steamgriddb["game"].get("name") or ""))
                )
            )
        ]

    def refresh_views(self):
        search = self.query_one("#game-search", Input)
        search_focused = search.has_focus
        self.update_mode_display()
        selected_path = self.highlighted_game_path

        archived_display = self.archived_games if self.storage_online else []
        self.archived_view.display = bool(archived_display)

        self.sort_games(self.shared_games)

        if self.storage_online:
            self.sort_games(self.archived_games)

        self.sort_games(self.games)

        self.shared_view.clear()
        self.archived_view.clear()
        self.library_view.clear()

        self.shared_view.extend(
            [GameRow(g) for g in self.filter_games(self.shared_games)]
        )
        self.archived_view.extend(
            [GameRow(g) for g in self.filter_games(archived_display)]
        )
        self.library_view.extend([GameRow(g) for g in self.filter_games(self.games)])

        self.update_status()

        def restore_highlight():

            if selected_path is not None:
                views = ()

                if self.app_mode == AppMode.LIBRARY:
                    views = (self.library_view,)
                elif self.app_mode == AppMode.MOVE:
                    views = (self.shared_view, self.archived_view)
                # Future modes here

                for view in views:
                    for i, row in enumerate(view.children):
                        if row.game.path == selected_path:
                            view.index = i

                            if self.app_mode == AppMode.LIBRARY:
                                self.refresh_library_details(row.game)

                            if not search_focused:
                                self.set_focus(view)
                            return
            view = self.current_view()
            if view is not None:
                view.index = 0

                row = view.children[0] if view.children else None

                if row is not None:
                    self.highlighted_game_path = row.game.path
                else:
                    self.highlighted_game_path = None

                if self.app_mode == AppMode.LIBRARY:
                    self.refresh_library_details(row.game if row else None)

                if not search_focused:
                    self.set_focus(view)

        self.call_after_refresh(restore_highlight)

    def update_status(self):
        parts = []

        if self.app_mode == AppMode.MOVE:
            shared_size = sum(g.size for g in self.shared_games)
            parts.append(f"Shared: {fmt_size(shared_size)} / {LIMIT_GB} GB")

            selected_archive = (
                sum(g.size for g in self.shared_games if g.selected) or None
            )
            if selected_archive is not None:
                parts.append(f"To Archive: {fmt_size(selected_archive)}")

            selected_restore = (
                sum(g.size for g in self.archived_games if g.selected) or None
            )
            if selected_restore is not None:
                parts.append(f"To Restore: {fmt_size(selected_restore)}")

        view = self.current_view()
        selected_count = sum(row.game.selected for row in view.children)
        if selected_count:
            parts.append(f"Selected: {selected_count}")

        if self.storage_online:
            parts.append(f"Sync: {self.sync_status}")
        else:
            parts.append("Remote Storage: Offline")

        steam = self.current_steam_user()
        parts.append(f"Steam: {steam.display_name if steam else 'None'}")

        if self.computing_sizes:
            parts.append("Computing sizes...")

        if self.detecting_steam:
            parts.append("Detecting Steam...")

        text = "   ".join(parts)
        self.query_one("#status", Static).update(text)

    def current_view(self) -> ListView | None:
        if self.app_mode == AppMode.LIBRARY:
            return self.library_view
        if self.app_mode == AppMode.MOVE:
            for view in (self.shared_view, self.archived_view):
                if view.has_focus:
                    return view
            return self.shared_view
        # future modes need handled here or they will return None causing errors in get_highlighted_game and action_toggle
        return None

    # ========================================================

    @work
    async def action_delete_game(self):
        if self.dialog_open or self.is_scan_running():
            return

        game = self.get_highlighted_game()

        if game is None:
            return

        # Remember the adjacent game so we can preserve the selection.
        view = self.current_view()
        current_index = view.index if view and view.index is not None else 0

        remaining_visible = (
            [
                row.game
                for row in view.children
                if isinstance(row, GameRow) and row.game is not game
            ]
            if view is not None
            else []
        )

        self.dialog_open = True

        try:
            choice = await self.push_screen_wait(
                OptionDialog(
                    title="Delete Game",
                    message=(
                        f"Remove {game.name} and all its saved library metadata?\n\n"
                        "The game files themselves will NOT be deleted."
                    ),
                    options=[
                        ("Delete", "delete"),
                        ("Cancel", "cancel"),
                    ],
                )
            )
        finally:
            self.dialog_open = False

        if choice != "delete":
            return

        # Delete the database record and its orphaned artwork.
        try:
            with get_db() as db:
                purge_game_record(db, game.id, game.path)
                db.commit()

        except (sqlite3.Error, ValueError) as e:
            self.notify(
                f"Failed to delete {game.name}: {type(e).__name__}: {e}",
                severity="error",
            )
            return

        # Remove the game from every in-memory collection.
        for games in (
            self.shared_games,
            self.archived_games,
            self.library_only_games,
            self.games,
        ):
            games[:] = [candidate for candidate in games if candidate is not game]

        # Select the next game in the current view, if available.
        if remaining_visible:
            next_index = min(current_index, len(remaining_visible) - 1)
            self.highlighted_game_path = remaining_visible[next_index].path
        else:
            self.highlighted_game_path = None

        self.refresh_views()

        if self.app_mode == AppMode.LIBRARY and not remaining_visible:
            self.refresh_library_details(None)

        self.notify(f"Removed {game.name} from the library.")

    def action_switch_mode(self):
        modes = list(AppMode)
        current_index = modes.index(self.app_mode)
        self.app_mode = modes[(current_index + 1) % len(modes)]

        # Invalidate pending artwork updates when switching modes.
        if self._artwork_timer is not None:
            self._artwork_timer.stop()
            self._artwork_timer = None

        self._artwork_generation += 1

        config = load_config()
        config.setdefault("ui", {})["last_mode"] = self.app_mode.value
        save_config(config)

        self.refresh_bindings()
        self.refresh_views()

    @work
    async def action_choose_launcher(self):
        game = self.get_highlighted_game()

        if game is None:
            return

        if not self.can_modify_steam():
            return

        previous_gamescope_enabled = game.gamescope_enabled
        self.dialog_open = True
        try:
            selected = await self.push_screen_wait(
                LauncherDialog(
                    max_depth=5,
                    game=game,
                )
            )
            gamescope_changed = previous_gamescope_enabled != game.gamescope_enabled
            launcher_changed = selected is not None and selected != game.launcher

            if not gamescope_changed and not launcher_changed:
                return

            if launcher_changed:
                game.launcher = selected
                await self.sync_shortcut(game)

            self.save_game_data()
            self.refresh_game_row(game)

            if launcher_changed:
                name = (
                    "Launch through Steam"
                    if is_steam_launcher(selected)
                    else Path(selected).name
                )

                self.notify(f"Launcher updated: {name}")

            if gamescope_changed:
                self.notify(
                    f"Gamescope {'enabled' if game.gamescope_enabled else 'disabled'}"
                )
            self.refresh_library_details(game)
        finally:
            self.dialog_open = False

    def action_open_proton_prefix(self):
        game = self.get_highlighted_game()

        if game is None:
            return

        compat = Path.home() / ".local/share/game_archiver/compatdata" / game.name

        if not compat.is_dir():
            self.notify("Proton prefix not found.", severity="warning")
            return

        subprocess.Popen(["xdg-open", str(compat)])

    def action_switch_pane(self):
        search = self.query_one("#game-search", Input)
        search_focused = search.has_focus
        if not search_focused:
            if self.shared_view.has_focus:
                self.archived_view.focus()
            else:
                self.shared_view.focus()

    def action_toggle(self):
        view = self.current_view()

        if view.index is None:
            return

        if not (0 <= view.index < len(view.children)):
            view.index = None
            return

        row = view.children[view.index]

        row.game.selected = not row.game.selected
        row.refresh_row()

        self.update_status()

    def action_auto_select(self):
        for g in self.shared_games:
            g.selected = False

        current = sum(g.size for g in self.shared_games)

        excess = current - LIMIT_BYTES

        if excess <= 0:
            self.notify("Already under limit")
            return

        freed = 0

        for game in sorted(self.shared_games, key=lambda g: g.mtime):
            if freed >= excess:
                break

            game.selected = True
            freed += game.size

        self.refresh_views()

    def get_current_game_list(self) -> list[Game]:
        if self.app_mode is AppMode.LIBRARY:
            return self.games
        elif self.app_mode is AppMode.MOVE and self.shared_view.has_focus:
            return self.shared_games
        elif self.app_mode is AppMode.MOVE and self.archived_view.has_focus:
            return self.archived_games

        return []

    def get_selected_games(self) -> list[Game]:
        return [g for g in self.get_current_game_list() if g.selected]

    def get_games_for_action(self) -> list[Game]:
        games = self.get_selected_games()

        if not games:
            game = self.get_highlighted_game()
            if game is not None:
                games = [game]

        return games

    async def action_move_selected(self):
        if self.app_mode != AppMode.MOVE:
            self.notify("Only available in MOVE mode")
            return
        if not self.can_modify_steam():
            return
        if not self.storage_online:
            self.notify("Storage Offline, can't move")
            return
        self.moving = True

        shared_location = get_location("Shared")
        archived_location = get_location("Archived")

        if self.shared_view.has_focus:
            destination = self.archived_games
            target = archived_location.remote_path
        else:
            destination = self.shared_games
            target = shared_location.remote_path

        if target is None:
            self.notify("Target location is not configured.")
            self.moving = False
            return

        moved = await self.move_games(destination, target)

        if not moved:
            self.moving = False
            self.notify("No games selected")
            return

        self.refresh_views()
        self.moving = False

    async def move_games(
        self,
        destination_games: list[Game],
        target: Path,
    ) -> bool:
        if not self.storage_online:
            return False
        source_games = self.get_current_game_list()
        moving = self.get_selected_games()

        if not moving:
            return False

        target.mkdir(
            parents=True,
            exist_ok=True,
        )

        for game in moving:
            duplicate_group = is_on_disk_duplicate(game)
            if duplicate_group:
                self.notify(
                    f"{game.name} exists in both {' and '.join(duplicate_group)} – "
                    "resolve the duplicate before moving."
                )
                game.selected = False
                continue
            self.notify(f"Moving {game.name}")

            old_path = game.path
            new_path = target / game.name

            # Preserve the current launcher and icon relative to the game directory.
            launcher_rel = None
            if game.launcher is not None and not is_steam_launcher(game.launcher):
                try:
                    launcher_rel = Path(game.launcher).relative_to(old_path)
                except ValueError:
                    pass

            if old_path == self.highlighted_game_path:
                self.highlighted_game_path = new_path

            executables = executable_files(old_path)

            await to_thread(shutil.move, old_path, new_path)

            for rel_path in executables:
                path = new_path / rel_path

                if path.exists():
                    mode = path.stat().st_mode
                    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP)
            game.path = new_path
            game.mtime = new_path.stat().st_mtime

            # Restore the launcher using its previous relative path.
            if is_steam_launcher(game.launcher):
                # Steam URI doesn't depend on the game directory.
                pass
            else:
                launchers = find_launchers(game)

                game.launcher = next(
                    (
                        path
                        for path in launchers
                        if isinstance(path, Path)
                        and launcher_rel is not None
                        and path == new_path / launcher_rel
                    ),
                    None,
                )

                if game.launcher is None:
                    game.launcher = find_launcher(game)

            # Preserve the icon if it was inside the game directory.
            update_artwork_paths_for_game(game, old_path, new_path)

            if get_selected_artwork(game, ArtworkType.ICON) is None:
                local_icons = await to_thread(find_icons, launch_path(game))
                await self.add_detected_icons(game, local_icons)

            await self.sync_shortcut(game)

            game.selected = False

            source_games.remove(game)
            destination_games.append(game)

        self.sort_games(source_games)
        self.sort_games(destination_games)

        self.save_game_data()

        return True

    def action_refresh(self):
        if self.is_scan_running():
            return
        if self.focused == self.query_one("#game-search", Input):
            return False

        self.verify_storage()

        scanned = self.scan_game_locations(GAME_LOCATIONS, self.storage_online)

        scanned_games = [game for games in scanned.values() for game in games]

        existing_games = self.games + self.unavailable_games

        self.games, self.unavailable_games = self.merge_games(
            existing_games,
            scanned_games,
        )

        shared_games = []
        archived_games = []
        library_only_games = []

        for game in self.games:
            location = get_game_location(game.path) if game.path else None

            if location is None:
                library_only_games.append(game)
                continue

            if location.name == "Shared":
                shared_games.append(game)
            elif location.name == "Archived":
                archived_games.append(game)
            else:
                library_only_games.append(game)

        self.shared_games = shared_games
        self.archived_games = archived_games
        self.library_only_games = library_only_games

        self.games = (
            self.shared_games + self.archived_games + self.library_only_games
            if self.storage_online
            else self.shared_games + self.library_only_games
            # this might not be good enough for library_only_games that are remote only but we don't have any of those right now.
        )

        self.refresh_views()
        self.start_scan()

    def action_launch(self):
        game = self.get_highlighted_game()
        if game is None:
            return

        game.last_played = time.time()

        self.sort_games(self.games)

        if game in self.shared_games:
            self.sort_games(self.shared_games)
        elif game in self.archived_games:
            self.sort_games(self.archived_games)

        self.refresh_views()

        try:
            if game.launcher is None:
                game.launcher = find_launcher(game)

            if is_steam_launcher(game.launcher):
                subprocess.Popen(
                    ["xdg-open", game.launcher],
                    start_new_session=True,
                )
                return

            info = get_launch_info(game)

            subprocess.Popen(
                info.cmd,
                cwd=info.cwd,
                env=info.env,
                start_new_session=True,
            )

        except (RuntimeError, OSError) as e:
            self.notify(str(e))
        finally:
            self.save_game_data()

    def action_open_folder(self):
        view = self.current_view()

        if view.index is None:
            return

        row = view.children[view.index]
        game = row.game

        path = launch_path(game)

        try:
            subprocess.Popen(
                ["xdg-open", str(path)],
                start_new_session=True,
            )
        except OSError as e:
            self.notify(f"Open failed: {e}")

    def action_open_remote_folder(self):
        if not self.storage_online:
            self.notify("Storage Offline")
            return
        view = self.current_view()

        if view.index is None:
            return

        row = view.children[view.index]
        path = row.game.path

        try:
            subprocess.Popen(
                ["xdg-open", str(path)],
                start_new_session=True,
            )
        except OSError as e:
            self.notify(f"Open failed: {e}")

    def action_terminal(self):
        view = self.current_view()

        if view.index is None:
            return

        row = view.children[view.index]
        game = row.game

        path = launch_path(game)

        self.notify(f"Opening terminal in {game.name}")

        subprocess.Popen(
            [
                "alacritty",
                "--working-directory",
                str(path),
            ],
            start_new_session=True,
        )

    def action_remote_terminal(self):
        if not self.storage_online:
            self.notify("Storage Offline")
            return
        view = self.current_view()

        if view.index is None:
            return

        row = view.children[view.index]
        game = row.game

        path = game.path

        subprocess.Popen(
            [
                "alacritty",
                "-e",
                "ssh",
                "-t",
                "sebanisuserver",
                f"cd '{path}' && exec $SHELL -l",
            ],
            start_new_session=True,
        )

    def action_next_steam_user(self):
        if len(self.steam_users) <= 1:
            return

        self.selected_steam_user = (self.selected_steam_user + 1) % len(
            self.steam_users
        )

        self.update_status()

    def action_prev_steam_user(self):
        if len(self.steam_users) <= 1:
            return

        self.selected_steam_user = (self.selected_steam_user - 1) % len(
            self.steam_users
        )

        self.update_status()

    async def action_sync_shortcut(self):
        if not self.can_modify_steam():
            return

        user = self.current_steam_user()
        if user is None:
            return False

        if self.shared_view.has_focus:
            games = [g for g in self.shared_games if g.selected]
        else:
            games = [g for g in self.archived_games if g.selected]

        if not games:
            game = self.get_highlighted_game()
            if game is None:
                self.notify("No games selected or highlighted.")
                return
            games = [game]

        shortcuts = load_shortcuts(self.current_steam_user())
        entries = shortcuts["shortcuts"]

        updated = 0

        for game in games:
            if await self.sync_shortcut(game):
                updated += 1

        # Reload after any changes
        shortcuts = load_shortcuts(self.current_steam_user())
        entries = shortcuts["shortcuts"]

        for game in games:
            await self.update_game_steam_status(game, entries)

        # Remove duplicate shortcuts with identical StartDir paths.
        if purge_duplicate_shortcuts(entries, games):
            save_shortcuts(user, shortcuts)

            # Recalculate status after removing entries.
            for game in games:
                await self.update_game_steam_status(game, entries)

        for game in games:
            self.refresh_game_row(game)

        self.notify(f"Updated {updated} Steam shortcut{'s' if updated != 1 else ''}.")

    def action_select_none(self):
        games = self.get_current_game_list()

        if not games:
            self.notify("No games.")
            return

        for game in games:
            if game.selected:
                game.selected = False
                self.refresh_game_row(game)

        self.update_status()
        self.save_game_data()

    def action_select_all(self):
        games = self.get_current_game_list()
        filtered_games = self.filter_games(games)

        if not filtered_games:
            self.notify("No games to select.")
            self.action_select_none()
            return

        filtered_paths = {game.path for game in filtered_games}

        for game in games:
            selected = game.path in filtered_paths

            if game.selected != selected:
                game.selected = selected
                self.refresh_game_row(game)

        self.update_status()
        self.save_game_data()

    async def link_steamgriddb(
        self,
        game: Game,
        client: SteamGridDBClient,
    ):
        query = steamgriddb_search_name(game)
        results = []
        prompt_for_search = False

        while True:
            if not prompt_for_search:
                result = await client.search_game(query)

                if not result["success"]:
                    self.notify(result["error"])
                    return

                results = result["data"]
            if not results:
                action = await self.push_screen_wait(NoArtworkMatchDialog(query))

                match action:
                    case NoArtworkMatchAction.CANCEL:
                        return

                    case None:
                        return

                    case NoArtworkMatchAction.SEARCH:
                        new_query = await self.push_screen_wait(
                            InputDialog(
                                title="SteamGridDB Search",
                                prompt="Search for:",
                                value=query,
                                allow_local_artwork=True,
                            )
                        )

                        if (
                            new_query is None
                            or new_query.action == InputDialogAction.CANCEL
                        ):
                            return

                        if new_query.action == InputDialogAction.LOCAL:
                            await self.download_local_artwork(game, client)
                            return

                        assert new_query.action == InputDialogAction.OK
                        new_query = new_query.value.strip()

                        if not new_query:
                            self.notify("Search query cannot be empty.")
                            prompt_for_search = True
                            continue

                        query = new_query
                        game.steamgriddb["search"] = query
                        self.save_game_data()
                        prompt_for_search = False
                        continue

                    case NoArtworkMatchAction.LOCAL:
                        await self.download_local_artwork(game, client)
                        return

            if prompt_for_search:
                prompt_for_search = False

                new_query = await self.push_screen_wait(
                    InputDialog(
                        title="SteamGridDB Search",
                        prompt="Search for:",
                        value=query,
                        allow_local_artwork=True,
                    )
                )

                if new_query is None or new_query.action == InputDialogAction.CANCEL:
                    return

                if new_query.action == InputDialogAction.LOCAL:
                    await self.download_local_artwork(game, client)
                    return

                assert new_query.action == InputDialogAction.OK
                new_query = new_query.value.strip()

                if not new_query:
                    self.notify("Search query cannot be empty.")
                    prompt_for_search = True
                    continue

                query = new_query
                game.steamgriddb["search"] = query
                self.save_game_data()
                continue

            action, selected = await self.push_screen_wait(
                SelectionDialog(
                    title="SteamGridDB Matches",
                    results=results,
                )
            )

            match action:
                case None:
                    return
                case SelectionDialogAction.CANCEL:
                    return
                case SelectionDialogAction.SEARCH:
                    prompt_for_search = True
                    continue
                case SelectionDialogAction.BROWSE:
                    continue  # show SelectionDialog again as shouldn't happen.
                case SelectionDialogAction.LOCAL:
                    await self.download_local_artwork(game, client)
                    return
                case SelectionDialogAction.SELECT:
                    pass

            assert selected is not None
            game.steamgriddb = {
                "search": query,
                # Full SteamGridDB game details.
                "game": None,
                # Steam platform data.
                "steam_platform_data": None,
                # Unix timestamp of the last artwork metadata refresh.
                "cached": None,
                # None = never fetched.
                "art": {
                    ArtworkType.GRID_PORTRAIT: None,
                    ArtworkType.GRID_SQUARE: None,
                    ArtworkType.GRID_LANDSCAPE: None,
                    ArtworkType.HERO: None,
                    ArtworkType.LOGO: None,
                    ArtworkType.ICON: None,
                },
                # User's chosen artwork IDs.
                "selected": {
                    ArtworkType.GRID_PORTRAIT: None,
                    ArtworkType.GRID_SQUARE: None,
                    ArtworkType.GRID_LANDSCAPE: None,
                    ArtworkType.HERO: None,
                    ArtworkType.LOGO: None,
                    ArtworkType.ICON: None,
                },
            }
            game_details = await client.get_game(selected["id"])

            if game_details is not None:
                game.steamgriddb["game"] = game_details

            steam_platform_data = await client.get_steam_platform_data(game)

            if steam_platform_data is not None:
                game.steamgriddb["steam_platform_data"] = steam_platform_data

            # await download_steam_art(game)

            self.save_game_data()

            self.notify(f"Selected {selected['name']}")
            self.refresh_game_row(game)
            await self.manage_steamgriddb(game, client)
            return

    async def manage_steamgriddb(
        self,
        game: Game,
        client: SteamGridDBClient,
    ):
        while True:
            action = await self.push_screen_wait(SteamGridDBDialog(game))

            match action:
                case None:
                    return

                case SteamGridDBAction.CANCEL:
                    return

                case SteamGridDBAction.BROWSE:
                    webbrowser.open(
                        f"https://www.steamgriddb.com/game/{game.steamgriddb['game']['id']}"
                    )

                case SteamGridDBAction.CHANGE:
                    game.steamgriddb.pop("game", None)
                    return await self.link_steamgriddb(game, client)

                case SteamGridDBAction.DOWNLOAD:
                    await self.download_steamgriddb_art(
                        game,
                        client,
                    )

    async def download_local_artwork(
        self,
        game: Game,
        client: SteamGridDBClient,
    ):
        art = {
            ArtworkType.GRID_PORTRAIT: [],
            ArtworkType.GRID_SQUARE: [],
            ArtworkType.GRID_LANDSCAPE: [],
            ArtworkType.HERO: [],
            ArtworkType.LOGO: [],
            ArtworkType.ICON: [],
        }

        game.steamgriddb.setdefault("selected", {})
        game.steamgriddb.setdefault("art", art)
        art = game.steamgriddb["art"]

        local_icons = await to_thread(find_icons, launch_path(game))
        await self.add_detected_icons(game, local_icons)

        await self._choose_artwork(game, client, art)

    async def download_steamgriddb_art(
        self,
        game: Game,
        client: SteamGridDBClient,
    ):
        art_data = await client.get_all_art(game)

        if not art_data["success"]:
            self.notify(art_data["error"])
            return

        art = art_data["data"]
        game.steamgriddb["art"] = art
        local_icons = await to_thread(find_icons, launch_path(game))
        await self.add_detected_icons(game, local_icons)

        self.save_game_data()

        await self._choose_artwork(game, client, art)

    async def _choose_artwork(self, game: Game, client: SteamGridDBClient, art: dict):
        while True:
            result = await self.push_screen_wait(
                ArtworkTypeDialog(
                    art,
                    selected_types={
                        art_type
                        for art_type, info in (
                            game.steamgriddb.get("selected") or {}
                        ).items()
                        if isinstance(info, dict) and info.get("asset") is not None
                    },
                    selected_artwork=game.steamgriddb.get("selected"),
                )
            )

            if result is None:
                return

            art_type = result.artwork_type

            if result.choose_local:
                selected_path = await self.push_screen_wait(
                    FileOpen(
                        location=Path.home(),
                        title=(
                            "Choose Local "
                            f"{art_type.name.replace('_', ' ').title()} Artwork"
                        ),
                    )
                )

                if selected_path is None:
                    continue

                # Add this local file to the candidates for the current type.
                artwork = list(art.get(art_type) or [])
                artwork.append(
                    {
                        "id": str(selected_path),
                        "path": selected_path,
                        "url": selected_path.as_uri(),
                        "name": selected_path.name,
                        "source": "local",
                        "style": "Local",
                        "kind": selected_path.suffix.lower(),
                    }
                )

            else:
                match art_type:
                    case ArtworkType.CANCEL:
                        return
                    case ArtworkType.GRID_PORTRAIT:
                        artwork = art[ArtworkType.GRID_PORTRAIT].copy()
                    case ArtworkType.GRID_SQUARE:
                        artwork = art[ArtworkType.GRID_SQUARE].copy()
                    case ArtworkType.GRID_LANDSCAPE:
                        artwork = art[ArtworkType.GRID_LANDSCAPE].copy()
                    case ArtworkType.HERO:
                        artwork = art[ArtworkType.HERO].copy()
                    case ArtworkType.LOGO:
                        artwork = art[ArtworkType.LOGO].copy()
                    case ArtworkType.ICON:
                        artwork = art[ArtworkType.ICON].copy()
            selected_artwork = (game.steamgriddb.get("selected") or {}).get(
                art_type
            ) or {}
            if selected_artwork.get("asset") is None:
                installed_paths = self.find_installed_artwork(game, art_type)
                appended = False
                for path in installed_paths:
                    installed_asset = self.register_installed_artwork(
                        game,
                        art_type,
                        path,
                    )

                    if not any(
                        item.get("id") == installed_asset["id"]
                        and item.get("source") == installed_asset["source"]
                        for item in artwork
                    ):
                        artwork.append(installed_asset)
                        appended = True

                if installed_paths and appended:
                    self.save_game_data()

            action, selected = await self.push_screen_wait(
                ArtworkSelectionDialog(
                    title=f"{art_type.name.replace('_', ' ').title()} Artwork",
                    artwork=artwork,
                    client=client,
                    selected_artwork=selected_artwork.get("asset"),
                )
            )

            match action:
                case None:
                    continue

                case ArtworkSelectionDialogAction.CANCEL:
                    continue

                case ArtworkSelectionDialogAction.DOWNLOAD:
                    if selected is None:
                        continue

                    selected_info = game.steamgriddb["selected"].get(art_type)
                    if selected_info is None:
                        selected_info = {}
                        game.steamgriddb["selected"][art_type] = selected_info

                    selected_info["asset"] = selected

                    if selected.get("source") == "local":
                        add_selected_artwork_to_art(game, art_type, selected)
                        selected_info["downloaded_path"] = selected["path"]
                    else:
                        selected_info["downloaded_path"] = await get_asset_file(
                            client,
                            selected,
                        )

                    self.save_game_data()

                    if selected_info["downloaded_path"]:
                        await self.install_artwork(
                            game,
                            art_type,
                            selected_info["downloaded_path"],
                        )

    def find_installed_artwork(
        self,
        game: Game,
        art_type: ArtworkType,
    ) -> list[Path]:
        installed: list[Path] = []

        for extension in ARTWORK_EXTENSIONS:
            path = self.get_steam_artwork_path(
                game,
                art_type,
                extension,
            )

            if path is not None and path.is_file():
                installed.append(path)
            elif path is None:
                self.notify(
                    f"{art_type.name} artwork cannot be installed as Steam artwork.",
                    severity="warning",
                )
                return []

        return installed

    def register_installed_artwork(
        self,
        game: Game,
        art_type: ArtworkType,
        path: Path,
    ) -> dict:
        asset = {
            "id": str(path),
            "path": path,
            "url": path.as_uri(),
            "name": path.name,
            "source": "local",
            "style": "Installed in Steam",
            "kind": path.suffix.lower(),
        }

        # Add to the general artwork collection if it isn't already there.
        add_selected_artwork_to_art(game, art_type, asset)

        # Create a selected entry only if one doesn't already exist.
        selected_artwork = game.steamgriddb.setdefault("selected", {})
        selected = selected_artwork.get(art_type)

        if not isinstance(selected, dict):
            selected = {}
            selected_artwork[art_type] = selected

        if selected.get("asset") is None:
            selected["asset"] = asset
            selected["downloaded_path"] = (
                path  # is kinda redundant but it's expected by the code that uses it.
            )

        return asset

    def get_steam_artwork_path(
        self,
        game: Game,
        art_type: ArtworkType,
        extension: str,
        appid: str | int | None = None,
        is_steam_game: bool = False,
    ) -> Path | None:
        if is_steam_game:
            metadata = game.steamgriddb.get("steam_platform_data") or {}
            steam_apps = metadata.get("external_platform_data", {}).get("steam", [])
            appid = steam_apps[0].get("id") if steam_apps else None

            if not appid:
                appid = game_appid(game)
        else:
            appid = appid if appid is not None else game_appid(game)

        if not appid:
            return None

        extension = extension.lower()
        if not extension.startswith("."):
            extension = f".{extension}"

        if art_type == ArtworkType.ICON:
            return STEAM_ICON_DIR / f"{appid}{extension}"

        suffix = suffixes[art_type]
        if suffix is None:
            return None

        user = self.current_steam_user()
        if user is None:
            return None

        return steam_grid_dir(user) / f"{appid}{suffix}{extension}"

    async def add_detected_icons(
        self,
        game: Game,
        candidates: list[IconCandidate],
    ) -> None:
        if not candidates:
            return

        if game.steamgriddb is None:
            game.steamgriddb = {}

        for candidate in candidates:
            asset = icon_candidate_to_artwork(candidate)

            add_selected_artwork_to_art(
                game,
                ArtworkType.ICON,
                asset,
            )
        self.save_game_data()

        # Only initialize and install a selection if one doesn't exist.
        if get_selected_artwork(game, ArtworkType.ICON) is None:
            asset = icon_candidate_to_artwork(candidates[0])

            selected = {
                "asset": asset,
                "downloaded_path": asset["path"],
                "installed_path": None,
            }

            game.steamgriddb.setdefault("selected", {})[ArtworkType.ICON] = selected

            await self.install_artwork(
                game,
                ArtworkType.ICON,
                selected["downloaded_path"],
            )

    async def install_artwork(
        self,
        game: Game,
        art_type: ArtworkType,
        image: Path | str,
    ) -> bool:
        image = Path(image)

        if not image.is_file():
            self.notify(
                f"Artwork file not found: {image}",
                severity="error",
            )
            return False

        if art_type == ArtworkType.ICON:
            if image.suffix.lower() == ImageExtension.EXE:
                extracted = await to_thread(extract_exe_icon, image)

                if extracted is None:
                    debug_log(
                        f"Icon extraction failed: {image} "
                        "(no extractable icon or unsupported executable)"
                    )
                    return False

                image_to_install = extracted
            else:
                image_to_install = image
        else:
            image_to_install = image

        dest = correct_image_extension(
            self.get_steam_artwork_path(
                game,
                art_type,
                image_to_install.suffix,
                is_steam_game=not can_add_to_steam(game),
            )
        )

        if dest is None:
            # self.notify(
            #     f"{art_type.name} artwork cannot be installed as Steam artwork.",
            #     severity="warning",
            # )
            return False

        dest.parent.mkdir(parents=True, exist_ok=True)
        if image_to_install.resolve() != dest.resolve():
            await to_thread(shutil.copy2, image_to_install, dest)

        for old_path in self.find_installed_artwork(game, art_type):
            if old_path.resolve() == dest.resolve():
                continue

            old_path.unlink(missing_ok=True)

        selected = game.steamgriddb["selected"][art_type]
        selected["installed_path"] = dest

        self.save_game_data()
        self.refresh_game_row(game)

        # Steam-managed games: persist the selection without installing artwork.
        if not can_add_to_steam(game):
            return True

        if art_type == ArtworkType.ICON:
            if not steam_running():
                await self.sync_shortcut(game)
            else:
                self.notify("Icon saved. Close Steam and sync shortcuts to apply it.")

        return True

    async def rename_artwork(
        self,
        game: Game,
        old_appid: str,
    ) -> bool:
        new_appid = game_appid(game)

        if old_appid == str(new_appid):
            return False

        changed = False
        icon_changed = False

        for art_type in ArtworkType:
            if art_type in (ArtworkType.CANCEL, ArtworkType.ICON):
                continue

            for ext in ARTWORK_EXTENSIONS:
                old = correct_image_extension(
                    self.get_steam_artwork_path(
                        game,
                        art_type,
                        ext,
                        appid=old_appid,
                    )
                )
                new = correct_image_extension(
                    self.get_steam_artwork_path(
                        game,
                        art_type,
                        ext,
                    )
                )

                if old is None or new is None or not old.is_file():
                    continue

                new.parent.mkdir(parents=True, exist_ok=True)
                await to_thread(old.replace, new)
                selected = get_selected_artwork(game, art_type)

                if selected is not None:
                    installed_path = selected.get("installed_path")

                    if (
                        installed_path is not None
                        and Path(installed_path).resolve() == old.resolve()
                    ):
                        selected["installed_path"] = new
                changed = True

        # Rename the selected ICON artwork, if it has been installed.
        selected = get_selected_artwork(game, ArtworkType.ICON)

        if selected is not None:
            installed_path = selected.get("installed_path")

            if installed_path is not None:
                installed_path = Path(installed_path)

                if installed_path.is_file():
                    new = correct_image_extension(
                        self.get_steam_artwork_path(
                            game,
                            ArtworkType.ICON,
                            installed_path.suffix,
                        )
                    )

                    if new is not None:
                        new.parent.mkdir(parents=True, exist_ok=True)

                        if installed_path.resolve() != new.resolve():
                            await to_thread(installed_path.replace, new)

                            selected["installed_path"] = new
                            changed = True
                            icon_changed = True

        if changed:
            self.save_game_data()

        if icon_changed:
            if not steam_running():
                await self.sync_shortcut(game)
            else:
                self.notify("Icon saved. Close Steam and sync shortcuts to apply it.")

        return changed

    async def get_steamgriddb_api_key(self) -> str | None:
        config = load_config()

        key = config.get("steamgriddb", {}).get("api_key")

        if key:
            return key

        while True:
            choice = await self.push_screen_wait(
                OptionDialog(
                    title="SteamGridDB",
                    message=("SteamGridDB API key is required.\n\nOpen the API page?"),
                    options=[
                        ("Open", "open"),
                        ("Paste Key", "paste"),
                        ("Cancel", "cancel"),
                    ],
                )
            )

            if choice is None or choice == "cancel":
                return None

            if choice == "open":
                webbrowser.open("https://www.steamgriddb.com/profile/preferences/api")
                continue

            if choice == "paste":
                key = await self.push_screen_wait(
                    InputDialog(
                        title="SteamGridDB API Key",
                        prompt="Paste your API key:",
                        placeholder="Paste API key...",
                        password=False,
                    )
                )

                if not key or not key.value or not key.value.strip():
                    self.notify("API key cannot be empty.", severity="warning")
                    continue

                config.setdefault(
                    "steamgriddb",
                    {},
                )["api_key"] = key.value.strip()

                save_config(config)

                return key.value.strip()

    async def apply_steamgriddb_to_all(
        self,
        games: list[Game],
        client: SteamGridDBClient,
    ):
        games.sort(key=lambda game: not has_steamgriddb_match(game))

        source_game = games[0]

        if has_steamgriddb_match(source_game):
            await self.manage_steamgriddb(source_game, client)
        else:
            await self.link_steamgriddb(source_game, client)

        # Check the final state after all dialog switching is finished.
        if not has_steamgriddb_match(source_game):
            self.notify("No SteamGridDB match selected.")
            return

        source_data = source_game.steamgriddb
        selected = source_data.get("selected") or {}

        selections = {
            art_type: deepcopy(info)
            for art_type, info in selected.items()
            if isinstance(info, dict)
            and info.get("asset") is not None
            and info.get("downloaded_path") is not None
        }

        if not selections:
            self.notify("No artwork selected to apply.")
            return

        # Copy the SteamGridDB match and artwork metadata to the other games.
        successful = 0

        for game in games[1:]:
            game.steamgriddb = deepcopy(source_data)

            for art_type, info in selections.items():
                game.steamgriddb["selected"][art_type] = deepcopy(info)

                if await self.install_artwork(
                    game,
                    art_type,
                    info["downloaded_path"],
                ):
                    successful += 1

        self.save_game_data()

        if successful:
            self.notify(f"Applied {successful} artworks to {len(games) - 1} games.")
        else:
            self.notify(
                "Failed to install artwork for all target games.",
                severity="error",
            )

    @work
    async def action_download_steamgriddb(self):
        self.dialog_open = True
        # grab the game here just encase it changes.
        highlighted_game = self.get_highlighted_game()
        games = self.get_games_for_action()

        try:
            if not games:
                self.notify("No games selected or highlighted.")
                return

            key = await self.get_steamgriddb_api_key()
            if key is None:
                return

            client = SteamGridDBClient(key)

            # One game: use the existing workflow directly.
            if len(games) == 1:
                game = games[0]

                if has_steamgriddb_match(game):
                    await self.manage_steamgriddb(game, client)
                else:
                    await self.link_steamgriddb(game, client)

                return

            else:
                choice = await self.push_screen_wait(
                    OptionDialog(
                        title="SteamGridDB",
                        message=f"How would you like to apply artwork to {len(games)} games?",
                        options=[
                            ("Apply to All", "all"),
                            ("Choose for Each", "each"),
                            ("Cancel", "cancel"),
                        ],
                    )
                )

                if choice == "each":
                    for game in games:
                        if has_steamgriddb_match(game):
                            await self.manage_steamgriddb(game, client)
                        else:
                            await self.link_steamgriddb(game, client)

                elif choice == "all":
                    await self.apply_steamgriddb_to_all(games, client)

        finally:
            self.dialog_open = False

            if highlighted_game is not None:
                self.refresh_library_details(highlighted_game)

            for game in games:
                row = self.get_row_for_game(game)
                if row is not None:
                    await row.refresh_icon()


# ============================================================
# STEAM
# ============================================================


@dataclass
class SteamUser:
    userdata_id: str
    persona_name: str | None

    @property
    def display_name(self) -> str:
        if self.persona_name:
            return f"{self.persona_name} ({self.userdata_id})"
        return self.userdata_id


def get_steam_users() -> list[SteamUser]:
    users: list[SteamUser] = []

    userdata = STEAM_DIR / "userdata"
    loginusers = STEAM_DIR / "config" / "loginusers.vdf"

    persona_lookup = {}

    if loginusers.exists():
        try:
            with loginusers.open(encoding="utf-8") as f:
                data = vdf.load(f)
        except (OSError, UnicodeError, vdf.VDFError):
            data = {}

        for steamid64, info in data.get("users", {}).items():
            try:
                userdata_id = str(int(steamid64) - 76561197960265728)
            except (ValueError, TypeError):
                continue

            if isinstance(info, dict):
                persona_lookup[userdata_id] = info.get("PersonaName") or info.get(
                    "AccountName"
                )

    if userdata.exists():
        for p in sorted(userdata.iterdir()):
            if not p.is_dir():
                continue

            if not p.name.isdigit():
                continue

            if p.name == "0":
                continue

            users.append(
                SteamUser(
                    userdata_id=p.name,
                    persona_name=persona_lookup.get(p.name),
                )
            )

    return users


class LauncherDialog(ModalScreen[Path | str | None]):
    DEFAULT_CSS = """
    LauncherDialog {
        align: center middle;
    }

    #dialog {
        width: 80;
        max-width: 95%;
        height: 80%;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }

    #launchers {
        height: 1fr;
        margin-top: 1;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "dismiss", "Cancel", show=False, priority=True),
    ]

    def __init__(
        self,
        game: Game,
        max_depth: int = 5,
    ):
        super().__init__()
        self.root: Path = launch_path(game)
        self.launchers: list[Path | str] = []
        self.current: Path | str | None = game.launcher
        self.game = game
        self._cancel_event = Event()
        self._launcher_worker = None
        self.max_depth = max_depth

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("[b]Select Game Launcher[/b]")

            yield ListView(
                *[self.make_launcher_item(launcher) for launcher in self.launchers],
                id="launchers",
            )

            yield Checkbox(
                label="Enable Gamescope",
                value=self.game.gamescope_enabled if self.game else False,
                id="gamescope-checkbox",
            )

            yield Label("Enter: Select    Escape: Cancel")

    @work(thread=True, exclusive=True)
    def load_launchers(self, game: Game) -> None:
        found = False

        for launcher in gen_launchers(
            game,
            self.max_depth,
            self._cancel_event,
        ):
            if self._cancel_event.is_set():
                return

            found = True
            self.app.call_from_thread(self.add_launcher, launcher)

        if not self._cancel_event.is_set():
            self.app.call_from_thread(self.finish_loading, found)

    def finish_loading(self, found: bool) -> None:
        if self.cancelled():
            return

        if not found:
            self._cancel_event.set()
            self.app.notify(f"No launchers found for {self.game.name}.")
            self.dismiss(None)
            return

        self.sort_launchers()

    def cancelled(self) -> bool:
        return self._cancel_event is not None and self._cancel_event.is_set()

    def add_launcher(self, launcher: Path | str) -> None:
        if self.cancelled():
            return

        if launcher in self.launchers:
            return

        self.launchers.append(launcher)

        list_view = self.query_one("#launchers", ListView)
        list_view.append(self.make_launcher_item(launcher))

    def sort_launchers(self) -> None:
        if self.cancelled():
            return

        def sort_key(p: Path | str):
            if isinstance(p, str):
                return (0, "")

            return (1, launcher_score(p, launch_path(self.game)))

        self.launchers.sort(key=sort_key)

        list_view = self.query_one("#launchers", ListView)
        list_view.clear()
        list_view.extend(self.make_launcher_item(p) for p in self.launchers)

    def on_unmount(self) -> None:
        self._cancel_event.set()

        if self._launcher_worker is not None:
            self._launcher_worker.cancel()

    def make_launcher_item(self, launcher: Path | str) -> ListItem:
        if is_steam_launcher(launcher):
            label = "Launch through Steam"
        else:
            path = Path(launcher)

            try:
                label = str(path.relative_to(self.root))
            except ValueError:
                label = str(path)

        item = ListItem(
            Label(f"{'[🗸] ' if str(launcher) == str(self.current) else '[ ] '}{label}")
        )
        item.tooltip = str(launcher)

        return item

    def on_list_view_selected(
        self,
        event: ListView.Selected,
    ) -> None:
        if self.cancelled():
            return

        if event.index is not None:
            self._cancel_event.set()
            self.dismiss(self.launchers[event.index])

    def on_checkbox_changed(
        self,
        event: Checkbox.Changed,
    ):
        if event.checkbox.id == "gamescope-checkbox" and self.game:
            self.game.gamescope_enabled = event.value

    def action_dismiss(self) -> None:
        if self.cancelled():
            return

        self._cancel_event.set()
        self.dismiss(None)

    def on_mount(self):
        if self.game:
            self._launcher_worker = self.load_launchers(self.game)


# ============================================================
# Option Dialog
# ============================================================
class OptionDialog(ModalScreen[str | None]):
    DEFAULT_CSS = """
    OptionDialog {
        align: center middle;
    }

    #dialog {
        width: 60;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }

    #buttons {
        align-horizontal: center;
        margin-top: 1;
    }

    Button {
        margin: 0 1;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "dismiss", "Cancel", show=False, priority=True),
    ]

    def __init__(
        self,
        title: str,
        message: str,
        options: list[tuple[str, str]],
    ):
        super().__init__()
        self.title = title
        self.message = message
        self.options = options

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"[b]{self.title}[/b]")
            yield Label(self.message)

            with Horizontal(id="buttons"):
                for text, value in self.options:
                    yield Button(
                        text,
                        id=value,
                    )

    def on_button_pressed(
        self,
        event: Button.Pressed,
    ):
        self.dismiss(event.button.id)

    def action_dismiss(self):
        self.dismiss(None)


# ============================================================
# Input Dialog
# ============================================================
class InputDialogAction(StrEnum):
    OK = "ok"
    LOCAL = "local"
    CANCEL = "cancel"


@dataclass
class InputDialogResult:
    action: InputDialogAction
    value: str | None = None


class InputDialog(ModalScreen[InputDialogResult | None]):
    DEFAULT_CSS = """
    InputDialog {
        align: center middle;
    }

    #dialog {
        width: 70;
        height: auto;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }

    #buttons {
        align-horizontal: right;
        margin-top: 1;
    }

    Input {
        margin-top: 1;
    }

    Button {
        margin-left: 1;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "dismiss", "Cancel", show=False, priority=True),
    ]

    def __init__(
        self,
        title: str,
        prompt: str,
        value: str = "",
        placeholder: str = "",
        password: bool = False,
        allow_local_artwork: bool = False,
    ):
        super().__init__()
        self.title = title
        self.prompt = prompt
        self.value = value
        self.placeholder = placeholder
        self.password = password
        self.allow_local_artwork = allow_local_artwork

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"[b]{self.title}[/b]")
            yield Label(self.prompt)
            yield Input(
                value=self.value,
                placeholder=self.placeholder,
                password=self.password,
                id="input",
            )

            with Horizontal(id="buttons"):
                yield Button("Cancel", id=InputDialogAction.CANCEL)

                if self.allow_local_artwork:
                    yield Button(
                        "Browse Local Artwork",
                        id=InputDialogAction.LOCAL,
                    )

                yield Button(
                    "OK",
                    id=InputDialogAction.OK,
                    variant="primary",
                )

    def on_mount(self):
        self.query_one(Input).focus()

    def on_input_submitted(
        self,
        event: Input.Submitted,
    ):
        self.dismiss(InputDialogResult(action=InputDialogAction.OK, value=event.value))

    def on_button_pressed(
        self,
        event: Button.Pressed,
    ):
        match event.button.id:
            case InputDialogAction.OK:
                self.dismiss(
                    InputDialogResult(
                        action=InputDialogAction.OK, value=self.query_one(Input).value
                    )
                )
            case InputDialogAction.LOCAL:
                self.dismiss(InputDialogResult(action=InputDialogAction.LOCAL))
            case _:
                self.dismiss(None)

    def action_dismiss(self):
        self.dismiss(None)


# ============================================================
# Artwork Type Dialog
# ============================================================
class NoArtworkMatchAction(StrEnum):
    LOCAL = "local"
    SEARCH = "search"
    CANCEL = "cancel"


class NoArtworkMatchDialog(ModalScreen[NoArtworkMatchAction | None]):
    DEFAULT_CSS = """
    NoArtworkMatchDialog {
        align: center middle;
    }

    #dialog {
        width: 60;
        height: auto;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }

    #buttons {
        width: 100%;
        height: auto;
        margin-top: 1;
        align-horizontal: right;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "dismiss", "Cancel", show=False, priority=True),
    ]

    def __init__(self, query: str):
        super().__init__()
        self.search_query = query

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("[b]No SteamGridDB matches found[/b]")
            yield Label(f"No results for: {self.search_query}")

            with Horizontal(id="buttons"):
                yield Button("Browse Local Artwork", id="local")
                yield Button("Search Again", id="search")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        match event.button.id:
            case "local":
                self.dismiss(NoArtworkMatchAction.LOCAL)
            case "search":
                self.dismiss(NoArtworkMatchAction.SEARCH)
            case _:
                self.dismiss(NoArtworkMatchAction.CANCEL)


@dataclass
class ArtworkTypeDialogResult:
    artwork_type: ArtworkType
    choose_local: bool = False


class ArtworkTypeDialog(ModalScreen[ArtworkTypeDialogResult | None]):
    DEFAULT_CSS = """
    ArtworkTypeDialog {
        align: center middle;
    }

    #dialog {
        width: 76;
        max-width: 95%;
        height: auto;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }

    #art-grid {
        width: 100%;
        height: auto;
        grid-size: 2;
        grid-columns: 1fr 1fr;
        grid-gutter: 1;
    }

    #art-grid .category-row {
        width: 100%;
        height: auto;
        layout: horizontal;
    }

    #art-grid .category-button {
        width: 1fr;
        min-width: 0;
    }

    #art-grid .local-button {
        width: 5;
        min-width: 5;
    }

    #cancel {
        width: 100%;
        margin-top: 1;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "dismiss", "Cancel", show=False, priority=True),
    ]

    def __init__(
        self,
        art: dict,
        selected_types: set[ArtworkType] | None = None,
        selected_artwork: dict | None = None,
    ):
        super().__init__()
        self.selected_types = selected_types or set()
        self.selected_artwork = selected_artwork or {}
        self.art = {
            ArtworkType.GRID_PORTRAIT: [],
            ArtworkType.GRID_LANDSCAPE: [],
            ArtworkType.GRID_SQUARE: [],
            ArtworkType.HERO: [],
            ArtworkType.LOGO: [],
            ArtworkType.ICON: [],
        }

        self.art[ArtworkType.GRID_PORTRAIT] = art.get(ArtworkType.GRID_PORTRAIT, [])
        self.art[ArtworkType.GRID_SQUARE] = art.get(ArtworkType.GRID_SQUARE, [])
        self.art[ArtworkType.GRID_LANDSCAPE] = art.get(ArtworkType.GRID_LANDSCAPE, [])
        self.art[ArtworkType.HERO] = art.get(ArtworkType.HERO, [])
        self.art[ArtworkType.LOGO] = art.get(ArtworkType.LOGO, [])
        self.art[ArtworkType.ICON] = art.get(ArtworkType.ICON, [])

    def compose(self) -> ComposeResult:
        categories = [
            (ArtworkType.GRID_PORTRAIT, "Grid Portrait"),
            (ArtworkType.GRID_SQUARE, "Grid Square"),
            (ArtworkType.GRID_LANDSCAPE, "Grid Landscape"),
            (ArtworkType.HERO, "Heroes"),
            (ArtworkType.LOGO, "Logos"),
            (ArtworkType.ICON, "Icons"),
        ]

        with Vertical(id="dialog"):
            yield Label("[b]Download Artwork[/b]")

            with Grid(id="art-grid"):
                for art_type, label in categories:
                    count = len(self.art[art_type])

                    selected_info = self.selected_artwork.get(art_type)
                    selected_asset = (
                        selected_info.get("asset")
                        if isinstance(selected_info, dict)
                        else None
                    )

                    if selected_asset and selected_asset.get("source") == "local":
                        selected_id = str(selected_asset.get("id"))

                        existing_ids = {
                            str(item.get("id")) for item in self.art[art_type]
                        }

                        if selected_id not in existing_ids:
                            count += 1

                    selected = "✓ " if art_type in self.selected_types else ""

                    with Horizontal(classes="category-row"):
                        yield Button(
                            f"{selected}{label} ({count})",
                            id=str(art_type),
                            classes="category-button",
                            disabled=count == 0,
                        )
                        yield Button(
                            "📁",
                            id=f"local-{art_type.name}",
                            classes="local-button",
                            tooltip=f"Choose local {label.lower()} artwork",
                        )

            yield Button("Cancel", id=str(ArtworkType.CANCEL))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id

        if button_id is None or button_id == str(ArtworkType.CANCEL):
            self.dismiss(None)
            return

        if button_id.startswith("local-"):
            art_type = ArtworkType[button_id.removeprefix("local-")]
            self.dismiss(
                ArtworkTypeDialogResult(
                    artwork_type=art_type,
                    choose_local=True,
                )
            )
            return

        self.dismiss(
            ArtworkTypeDialogResult(
                artwork_type=ArtworkType(button_id),
            )
        )

    def action_dismiss(self) -> None:
        self.dismiss(None)


# ============================================================
# Artwork Preview
# ============================================================
class ArtworkPreview(Container):
    DEFAULT_CSS = """
    ArtworkPreview {
        width: 100%;
        height: 100%;

        border: round $primary;

        layout: vertical;
        align: center middle;
    }

    #placeholder,
    #loading,
    #error {
        width: 100%;
        height: 100%;
        content-align: center middle;
    }

    #image {
        width: auto;
        height: auto;
    }
    """

    def __init__(
        self,
        client: SteamGridDBClient,
        *,
        id: str | None = None,
        classes: str | None = None,
    ):
        super().__init__(
            id=id,
            classes=classes,
        )

        self.client = client
        self.art: dict | None = None

    def compose(self) -> ComposeResult:
        yield Static(
            "No artwork selected",
            id="placeholder",
        )

    def cache_file(
        self,
        art: dict,
    ) -> Path:
        return THUMB_CACHE / f"{art['id']}{ImageExtension.PNG}"

    async def show_local_art(self, art: dict):
        if art.get("kind") == ImageExtension.EXE:
            path = await to_thread(extract_exe_icon, Path(art["path"]))
        else:
            path = Path(art["path"])

        await self.remove_children()

        if path is None or not Path(path).is_file():
            self.image = None
            await self.mount(
                Static(
                    "Could not load local artwork.\n"
                    "The file may be unsupported, or executable icon extraction may have failed.",
                    id="preview-error",
                )
            )
            return

        await self.mount(
            AutoImage(
                str(path),
                id="image",
            )
        )

    async def show_art(
        self,
        art: dict,
    ):
        # self.notify(
        #     f"show_art: {art.get('id')} "
        #     f"thumb={art.get('thumb')}"
        # )
        # Don't reload the same artwork.
        # if self.art is not None and self.art["id"] == art["id"]:
        #     return

        self.art = art

        path = self.cache_file(art)

        # Show loading state.
        await self.remove_children()

        await self.mount(
            Static(
                "Downloading preview...",
                id="loading",
            )
        )
        # self.notify("Loading placeholder mounted")

        if art.get("source") == "local":
            await self.show_local_art(art)
            return

        if not path.exists():
            urls = art.get("urls", [art["thumb"]])

            result = {"success": False}

            for url in urls:
                result = await self.client.download_file(
                    url,
                    path,
                )

                if result["success"]:
                    break

            if not result["success"]:
                await self.remove_children()

                await self.mount(
                    Static(
                        result["error"],
                        id="error",
                    )
                )
                return

        # self.notify(f"Thumbnail path: {path}")
        # self.notify(f"Exists: {path.exists()}")

        try:
            # Replace loading widget with image.
            await self.remove_children()

            # self.notify("Mounting AutoImage")
            # self.notify(
            #     f"path type: {type(path).__name__}\n"
            #     f"path: {path}"
            # )
            await self.mount(
                AutoImage(
                    str(path),
                    id="image",
                )
            )

            # self.notify("AutoImage mounted")
        except Exception:  # noqa: BLE001, S110 - AutoImage is optional.
            # self.notify(
            #     f"AutoImage error: "
            #     f"{type(e).__name__}: {e}"
            # )
            pass


# ============================================================
# Artwork Selection Dialog
# ============================================================
class ArtworkSelectionDialogAction(StrEnum):
    BROWSE = "browse"
    CANCEL = "cancel"
    DOWNLOAD = "download"


class ArtworkSelectionDialog(
    ModalScreen[tuple[ArtworkSelectionDialogAction, dict | None]]
):
    DEFAULT_CSS = """
    ArtworkSelectionDialog {
        align: center middle;
    }

    #dialog {
        width: 90%;
        height: 80%;
        layout: vertical;

        border: round $primary;
        background: $surface;
        padding: 1;
    }

    #body {
        height: 1fr;
    }

    #left {
        width: 45%;
        height: 100%;
    }

    #right {
        width: 55%;
        height: 100%;
        padding-left: 1;
    }

    #results {
        height: 100%;
    }

    #preview {
        width: 100%;
        height: 100%;
        border: round $primary;
        content-align: center middle;
    }

    #buttons {
        height: auto;
        margin-top: 1;
        align-horizontal: right;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "dismiss", "Cancel", show=False, priority=True),
    ]

    def __init__(
        self,
        title: str,
        artwork: list[dict],
        client: SteamGridDBClient,
        selected_artwork: dict | None = None,
    ):
        super().__init__()

        self.title = title
        self.artwork = deepcopy(artwork)
        self.client = client
        self.selected_artwork = selected_artwork
        self._dismiss_in_progress = False

        if selected_artwork and selected_artwork.get("source") == "local":
            selected_id = str(selected_artwork.get("id"))

            if not any(str(item.get("id")) == selected_id for item in self.artwork):
                self.artwork.append(deepcopy(selected_artwork))

    def build_item(
        self,
        art: dict,
    ) -> ListItem:
        details = []
        saved_id = self.selected_artwork.get("id") if self.selected_artwork else None

        is_selected = saved_id is not None and str(art["id"]) == str(saved_id)
        if art.get("style"):
            details.append(art["style"].title())

        if art.get("width") and art.get("height"):
            details.append(f"{art['width']}×{art['height']}")

        if art.get("score") is not None:
            details.append(f"★ {art['score']}")

        if art.get("nsfw"):
            details.append("NSFW")

        if art.get("lock"):
            details.append("🔒")

        text = art.get("path", art["id"])

        if isinstance(text, Path):
            text = text.name

        text = f"{text}"
        if is_selected:
            details.append("✓ Selected")

        if details:
            text += "\n    " + " • ".join(details)

        return ListItem(Label(text))

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"[b]{self.title}[/b]")

            with Horizontal(id="body"):
                with Vertical(id="left"):
                    yield ListView(
                        *(self.build_item(a) for a in self.artwork),
                        id="results",
                    )

                with Vertical(id="right"):
                    yield ArtworkPreview(
                        self.client,
                        id="preview",
                    )

            with Horizontal(id="buttons"):
                yield Button(
                    "Browse",
                    id=ArtworkSelectionDialogAction.BROWSE,
                )
                yield Button(
                    "Cancel",
                    id=ArtworkSelectionDialogAction.CANCEL,
                )
                yield Button(
                    "Download",
                    id=ArtworkSelectionDialogAction.DOWNLOAD,
                    variant="primary",
                )

    async def on_mount(self):
        self.query_one(ListView).focus()
        await self.update_preview()

    async def update_preview(self):
        index = self.query_one(ListView).index

        if index is None:
            return

        await self.query_one(ArtworkPreview).show_art(self.artwork[index])

    async def on_list_view_highlighted(
        self,
        event: ListView.Highlighted,
    ):
        await self.update_preview()

        index = event.list_view.index
        if index is None:
            return

        art = self.artwork[index]

        self.query_one(
            f"#{ArtworkSelectionDialogAction.BROWSE}",
            Button,
        ).disabled = art.get("source") == "local"

    def on_button_pressed(
        self,
        event: Button.Pressed,
    ):
        index = self.query_one(ListView).index

        if event.button.id == ArtworkSelectionDialogAction.BROWSE:
            if index is not None:
                webbrowser.open(self.artwork[index]["url"])
            return

        if event.button.id == ArtworkSelectionDialogAction.CANCEL:
            self.dismiss(
                (
                    ArtworkSelectionDialogAction.CANCEL,
                    None,
                )
            )
            return

        if index is not None:
            self.dismiss(
                (
                    ArtworkSelectionDialogAction.DOWNLOAD,
                    self.artwork[index],
                )
            )

    def dismiss(self, result=None):
        if getattr(self, "_dismiss_in_progress", False):
            return

        self._dismiss_in_progress = True
        super().dismiss(result)

    def action_dismiss(self):
        self.dismiss(
            (
                ArtworkSelectionDialogAction.CANCEL,
                None,
            )
        )


# ============================================================
# Selection Dialog
# ============================================================
class SelectionDialogAction(StrEnum):
    SEARCH = "search"
    BROWSE = "browse"
    SELECT = "select"
    CANCEL = "cancel"
    LOCAL = "local"


class SelectionDialog(ModalScreen[tuple[SelectionDialogAction, dict | None]]):
    DEFAULT_CSS = """
    SelectionDialog {
        align: center middle;
    }
    #dialog {
        width: 90%;
        height: 80%;
        layout: vertical;

        border: round $primary;
        background: $surface;
        padding: 1;
    }

    #body {
        height: 1fr;
    }

    #results {
        height: 100%;
    }

    #buttons {
        height: auto;
        margin-top: 1;
        align-horizontal: right;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "dismiss", "Cancel", show=False, priority=True),
    ]

    def __init__(
        self,
        title: str,
        results: list[dict],
    ):
        super().__init__()
        self.title = title
        self.results = results

    def build_result_item(self, result: dict) -> ListItem:
        text = result["name"]

        if result.get("verified"):
            text = f"✓ {text}"

        details = [f"ID: {result['id']}"]

        if types := result.get("types"):
            details.append(", ".join(types))

        if release := result.get("release_date"):
            year = datetime.fromtimestamp(release).year
            details.append(str(year))

        text += "\n    " + " • ".join(details)

        return ListItem(Label(text))

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"[b]{self.title}[/b]")

            with Vertical(id="body"):
                yield ListView(
                    *(self.build_result_item(r) for r in self.results),
                    id="results",
                )

            with Horizontal(id="buttons"):
                yield Button(
                    "Search Again",
                    id=SelectionDialogAction.SEARCH,
                )
                yield Button(
                    "Browse",
                    id=SelectionDialogAction.BROWSE,
                )
                yield Button(
                    "Browse Local Artwork",
                    id=SelectionDialogAction.LOCAL,
                )
                yield Button(
                    "Cancel",
                    id=SelectionDialogAction.CANCEL,
                )
                yield Button(
                    "Select",
                    id=SelectionDialogAction.SELECT,
                    variant="primary",
                )

    def on_mount(self):
        self.query_one(ListView).focus()

    # def on_list_view_selected(
    #     self,
    #     event: ListView.Selected,
    # ):
    #     self.dismiss(
    #         self.results[event.list_view.index]
    #     )

    def on_button_pressed(
        self,
        event: Button.Pressed,
    ):
        match event.button.id:
            case SelectionDialogAction.CANCEL:
                self.dismiss((SelectionDialogAction.CANCEL, None))

            case SelectionDialogAction.SEARCH:
                self.dismiss((SelectionDialogAction.SEARCH, None))
            case SelectionDialogAction.LOCAL:
                self.dismiss((SelectionDialogAction.LOCAL, None))
            case SelectionDialogAction.BROWSE | SelectionDialogAction.SELECT:
                index = self.query_one(ListView).index

                if index is None:
                    self.notify("Please select a game first.")
                    return

                match event.button.id:
                    case SelectionDialogAction.BROWSE:
                        webbrowser.open(
                            f"https://www.steamgriddb.com/game/{self.results[index]['id']}"
                        )

                    case SelectionDialogAction.SELECT:
                        self.dismiss(
                            (SelectionDialogAction.SELECT, self.results[index])
                        )

    def action_dismiss(self):
        self.dismiss((SelectionDialogAction.CANCEL, None))


# ============================================================
# SteamGridDB Dialog
# ============================================================
class SteamGridDBDialog(ModalScreen[SteamGridDBAction | None]):
    DEFAULT_CSS = """
    SteamGridDBDialog {
        align: center middle;
    }

    #dialog {
        width: 100;
        height: 90%;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }

    #details {
        height: 1fr;
        margin-top: 1;
        margin-bottom: 1;
    }

    #buttons {
        align-horizontal: right;
    }

    Button {
        margin: 0 1;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "dismiss", "Cancel", show=False, priority=True),
    ]

    def __init__(
        self,
        game: Game,
    ):
        super().__init__()
        self.game = game
        self.game_info_json = ""

    def compose(self) -> ComposeResult:
        sgdb = self.game.steamgriddb

        yield Vertical(
            Label("[b]SteamGridDB[/b]"),
            Label(
                f"Name: {sgdb.get('game', {}).get('name', 'Unknown')}\n"
                f"ID: {sgdb.get('game', {}).get('id', 'Unknown')}\n"
                f"Search: {sgdb.get('search', '')}"
            ),
            TextArea(
                "",
                id="details",
                read_only=True,
            ),
            Horizontal(
                Button(
                    "Browse",
                    id="browse",
                ),
                Button(
                    "Download Artwork",
                    id="download",
                    variant="primary",
                ),
                Button(
                    "Change Match",
                    id="change",
                ),
                Button("Copy JSON", id="copy", disabled=self.game.steamgriddb is None),
                Button(
                    "Cancel",
                    id="cancel",
                ),
                id="buttons",
            ),
            id="dialog",
        )

    def on_mount(self) -> None:
        if self.game.steamgriddb:
            self.game_info_json = json.dumps(
                self.game.steamgriddb,
                indent=2,
                sort_keys=True,
                default=json_default,
            )
            self.query_one("#details", TextArea).text = self.game_info_json

    def on_button_pressed(
        self,
        event: Button.Pressed,
    ):
        match event.button.id:
            case "browse":
                self.dismiss(SteamGridDBAction.BROWSE)

            case "download":
                self.dismiss(SteamGridDBAction.DOWNLOAD)

            case "change":
                self.dismiss(SteamGridDBAction.CHANGE)

            case "copy":
                self.app.copy_to_clipboard(self.game_info_json)

            case "cancel":
                self.dismiss(SteamGridDBAction.CANCEL)

    def action_dismiss(self):
        self.dismiss(SteamGridDBAction.CANCEL)


# ============================================================
# SteamGridDB Client
# ============================================================


def steamgriddb_search_name(game: Game) -> str:
    name = game.name
    if game.steamgriddb and "search" in game.steamgriddb:
        name = game.steamgriddb.get("search") or game.name

    # Strip common suffixes
    name = re.sub(r"\s*-\s*v[\d.]+.*$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s+-\s+PC$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\bR18\b", "", name, flags=re.IGNORECASE)

    return name.strip()


class SteamGridDBArtType(StrEnum):
    GRIDS = "grids"
    HEROES = "heroes"
    LOGOS = "logos"
    ICONS = "icons"


class SteamGridDBClient:
    BASE_URL = "https://www.steamgriddb.com/api/v2"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def get(self, endpoint: str, params: dict | None = None) -> dict:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
        }

        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                f"{self.BASE_URL}/{endpoint}",
                headers=headers,
                params=params,
            )

        try:
            payload = response.json()
        except ValueError:
            return {
                "success": False,
                "error": "SteamGridDB returned an invalid response.",
            }

        if not response.is_success:
            return {
                "success": False,
                "error": payload.get(
                    "errors",
                    [response.reason_phrase],
                )[0],
            }

        if not payload.get("success", False):
            return {
                "success": False,
                "error": payload.get(
                    "errors",
                    ["Unknown SteamGridDB error."],
                )[0],
            }

        return {
            "success": True,
            "data": payload["data"],
        }

    async def search_game(
        self,
        query: str,
    ) -> dict:
        query = quote(query)
        return await self.get(f"search/autocomplete/{query}")

    async def get_game(
        self,
        game_id: str,
    ) -> dict | None:
        result = await self.get(f"games/id/{game_id}")

        if not result["success"]:
            return None

        return result["data"]

    async def get_steam_platform_data(
        self,
        game: Game,
    ) -> dict | None:
        if not game.steamgriddb:
            return None

        game_id = (game.steamgriddb.get("game") or {}).get("id")

        if game_id is None:
            return None

        result = await self.get(
            f"games/id/{game_id}",
            params={
                "platformdata": "steam",
            },
        )

        if not result["success"]:
            return None

        return result["data"]

    async def get_art(
        self,
        kind: SteamGridDBArtType,
        game: Game,
    ) -> dict:
        if not game.steamgriddb:
            return {
                "success": False,
                "error": "Game is not linked to SteamGridDB.",
            }

        game_id = game.steamgriddb.get("game", {}).get("id")

        if game_id is None:
            return {
                "success": False,
                "error": "Game is not linked to SteamGridDB.",
            }
        options = SearchOptions()
        return await self.get(f"{kind}/game/{game_id}", params=options.params())

    async def get_grids(self, game: Game) -> dict:
        return await self.get_art(SteamGridDBArtType.GRIDS, game)

    async def get_heroes(self, game: Game) -> dict:
        return await self.get_art(SteamGridDBArtType.HEROES, game)

    async def get_logos(self, game: Game) -> dict:
        return await self.get_art(SteamGridDBArtType.LOGOS, game)

    async def get_icons(self, game: Game) -> dict:
        return await self.get_art(SteamGridDBArtType.ICONS, game)

    async def get_all_art(
        self,
        game: Game,
    ) -> dict:
        cached = game.steamgriddb.get("cached")
        artwork_cached = game.steamgriddb.get("artwork_cached")

        if (
            cached is not None
            and artwork_cached
            and time.time() - cached < 60 * 60 * 24 * 30
        ):
            return {
                "success": True,
                "data": game.steamgriddb["art"],
            }

        grids = await self.get_grids(game)
        heroes = await self.get_heroes(game)
        logos = await self.get_logos(game)
        icons = await self.get_icons(game)

        for result in (grids, heroes, logos, icons):
            if not result["success"]:
                return result

        steam_art = steam_art_urls(game.steamgriddb.get("steam_platform_data"))

        game.steamgriddb["art"] = {
            ArtworkType.GRID_PORTRAIT: [
                grid
                for grid in grids.get("data", [])
                if classify_grid(grid) is ArtworkType.GRID_PORTRAIT
            ],
            ArtworkType.GRID_SQUARE: [
                grid
                for grid in grids.get("data", [])
                if classify_grid(grid) is ArtworkType.GRID_SQUARE
            ],
            ArtworkType.GRID_LANDSCAPE: [
                grid
                for grid in grids.get("data", [])
                if classify_grid(grid) is ArtworkType.GRID_LANDSCAPE
            ],
            ArtworkType.HERO: heroes.get("data", []),
            ArtworkType.LOGO: logos.get("data", []),
            ArtworkType.ICON: icons.get("data", []),
        }

        for artwork_type, artwork in steam_art.items():
            game.steamgriddb["art"][artwork_type][0:0] = artwork

        game.steamgriddb["cached"] = time.time()

        return {
            "success": True,
            "data": game.steamgriddb["art"],
        }

    async def download_file(
        self,
        url: str,
        path: Path,
    ) -> dict:
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url)

                response.raise_for_status()

            path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            path.write_bytes(response.content)

            return {
                "success": True,
            }

        except (
            httpx.RequestError,
            httpx.HTTPStatusError,
            OSError,
        ) as e:
            return {
                "success": False,
                "error": str(e),
            }


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    init_database()
    # migrate_game_data()
    GameArchiver().run()
