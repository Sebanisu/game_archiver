#!/usr/bin/env python3

from __future__ import annotations

import binascii
import ctypes
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import tomllib
import webbrowser
import httpx
import datetime
from enum import Enum, auto, StrEnum
from urllib.parse import quote
from dataclasses import dataclass
from pathlib import Path
from hashlib import sha1

from PIL import Image

import vdf
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, Grid, Container
from textual_image.widget import AutoImage
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    TextArea,
    ListItem,
    ListView,
    Static,
)

try:
    import tomli_w
except ImportError:
    tomli_w = None


# ============================================================
# CONFIG
# ============================================================
CONFIG_DIR = Path.home() / ".config/game_archiver"
CONFIG_FILE = CONFIG_DIR / "config.toml"

CONFIG_DIR.mkdir(parents=True, exist_ok=True)
LOCAL_SHARED_DIR = Path.home() / "Games/shared"
REMOTE_SHARED_DIR = Path("/mnt/media/pool/Games/shared")
REMOTE_ARCHIVED_DIR = Path("/mnt/media/pool/Games/archived")
POOL_DIR = Path("/mnt/media/pool")
CACHE_FILE = Path.home() / ".cache/game_archiver_sizes.json"
PROTON = (
    Path.home()
    / ".local/share/Steam/steamapps/common/Proton - Experimental/proton"
)
ICON_FALLBACK = Path(
    "/usr/share/icons/hicolor/256x256/apps/steam.png"
)
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
)

SHARED_DIR = REMOTE_SHARED_DIR
ARCHIVED_DIR = REMOTE_ARCHIVED_DIR

LIMIT_GB = 256
LIMIT_BYTES = LIMIT_GB * 1024 * 1024 * 1024

IGNORE_DIRS = {
    "shadercache",
    "compatdata",
    "cache",
    ".cache",
}

LAUNCHER_EXTS = (
    ".sh",
    ".AppImage",
    ".x86_64",
    ".exe",
    ".swf",
    ".apk"
)

ICON_CACHE = (
    Path.home()
    / ".cache/game_archiver/icons"
)
ICON_CACHE.mkdir(parents=True, exist_ok=True)

STEAM_ART_CACHE = (
    Path.home()
    / ".cache/game_archiver/steam"
)

STEAM_STORE_CDN = "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/"
STEAM_ICON_CDN = "https://shared.fastly.steamstatic.com/community_assets/images/apps/"
STEAM_CLIENTICON_CDN = "https://cdn.cloudflare.steamstatic.com/steamcommunity/public/images/apps/"

# ============================================================
# MODEL
# ============================================================

@dataclass
class LaunchInfo:
    cmd: list[str]
    cwd: Path
    env: dict[str, str]

def get_launch_info(game: Game,*, for_steam: bool = False) -> LaunchInfo:
    launcher = game.launcher
    if launcher is None:
        raise ValueError("Game has no launcher")

    env = os.environ.copy()

    if launcher.suffix == ".sh":
        cmd = (
            [str(launcher)]
            if for_steam
            else ["bash", str(launcher)]
        )

    elif launcher.suffix == ".swf":
        cmd = ["ruffle", str(launcher)]
        
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
            compat = (
                Path.home()
                / ".local/share/game_archiver/compatdata"
                / game.name
            )

            compat.mkdir(parents=True, exist_ok=True)

            env["STEAM_COMPAT_DATA_PATH"] = str(compat)
            env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(
                Path.home() / ".local/share/Steam"
            )

            #env["WINE_FULLSCREEN_FSR"] = "1"
            #env["DXVK_HUD"] = "full"

            cmd = [
                str(PROTON),
                "run",
                str(launcher),
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
    name: str
    path: Path
    size: int
    mtime: float
    last_played: float = 0.0
    selected: bool = False
    launcher: Path | None = None
    icon: Path | None = None
    steam_key: str | None = None
    steam_entry: dict | None = None
    in_steam: bool = False
    steam_broken: bool = False
    duplicate_steam: bool = False
    steamgriddb: dict | None = None


# ============================================================
# HELPERS
# ============================================================
class ArtworkType(StrEnum):
    GRID_PORTRAIT = "grid_portrait"
    GRID_SQUARE = "grid_square"
    GRID_LANDSCAPE = "grid_landscape"
    HERO = "hero"
    LOGO = "logo"
    ICON = "icon"
    CANCEL = "cancel"

def classify_grid(grid: dict) -> ArtworkType:
    width = grid["width"]
    height = grid["height"]

    ratio = width / height

    if ratio < 0.9:
        return ArtworkType. GRID_PORTRAIT

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


FALLBACKS = {
    "library_capsule": {
        "thumb": "library_600x900.jpg",
        "full": "library_600x900_2x.jpg",
    },
    "header_image": {
        "thumb": "header.jpg",
        "full": "header.jpg",
    },
    "library_hero": {
        "thumb": "library_hero.jpg",
        "full": "library_hero_2x.jpg",
    },
    "library_logo": {
        "thumb": "logo.png",
        "full": "logo_2x.png",
    },
}

def steam_art_urls(metadata: dict | None) -> dict[str, tuple[str, str]]:
    """
    Returns:
    {
        "grid": ("english", "https://..."),
        "hero": ("english", "https://..."),
        "logo": ("english", "https://...")
    }
    """

    art = {
        ArtworkType.GRID_PORTRAIT: None, 
        ArtworkType.GRID_SQUARE: None, 
        ArtworkType.GRID_LANDSCAPE: None, 
        ArtworkType.HERO: None, 
        ArtworkType.LOGO: None, 
        ArtworkType.ICON: None,
    }

    if not metadata:
        return art

    app = (
        metadata.get("external_platform_data", {})
        .get("steam", [{}])[0]
    )

    appid = app.get("id")
    meta = app.get("metadata", {})

    if not appid:
        return art

    def build(steam_type: str) -> dict | None:
        data = meta.get(f"{steam_type}_full")
        icon = meta.get("icon")
        clienticon = meta.get("clienticon")

        language = "english"

        if steam_type == "icon":
            if clienticon:
                thumb_url = full_url = (
                    f"{STEAM_CLIENTICON_CDN}{appid}/{clienticon}.ico"
                )
            elif icon:
                thumb_url = full_url = (
                    f"{STEAM_ICON_CDN}{appid}/{icon}.jpg"
                )
            else:
                return None

        else:
            if isinstance(data, dict):
                thumb = preferred_language(data.get("image", data))
                full = preferred_language(
                    data.get("image2x", data.get("image", data))
                )

                if thumb is None:
                    return None

                language, thumb_file = thumb

                if full is None:
                    full_file = thumb_file
                else:
                    _, full_file = full
            else:
                fallback = FALLBACKS.get(steam_type)
                if fallback is None:
                    return None

                thumb_file = fallback["thumb"]
                full_file = fallback["full"]

            thumb_url = f"{STEAM_STORE_CDN}{appid}/{thumb_file}"
            full_url = f"{STEAM_STORE_CDN}{appid}/{full_file}"

        mtime = meta.get("store_asset_mtime")
        if mtime:
            thumb_url += f"?t={mtime}"
            full_url += f"?t={mtime}"

        return {
            "id": sha1(full_url.encode()).hexdigest(),
            "language": language,
            "thumb": thumb_url,
            "url": full_url,
            "author": {
                "name": "Steam",
                "avatar": None,
                "steam64": None,
            },
            "style": "official",
            "source": "steam",
        }

    # Add these here
    # icon = meta.get("icon")
    # if icon:
    #     urls["icon"] = (
    #         "steam",
    #         f"{STEAM_STORE_CDN}/steam/apps/{appid}/{icon}.ico",
    #     )

    # clienticon = meta.get("clienticon")
    # if clienticon:
    #     urls["clienticon"] = (
    #         "steam",
    #         f"{STEAM_STORE_CDN}/steam/apps/{appid}/{clienticon}.ico",
    #     )

    art[ArtworkType.GRID_PORTRAIT] = build("library_capsule")
    art[ArtworkType.GRID_LANDSCAPE] = build("header_image")
    art[ArtworkType.HERO] = build("library_hero")
    art[ArtworkType.LOGO] = build("library_logo")
    art[ArtworkType.ICON] = build("icon") # uses clienticon and falls back to icon
    return art


# async def download_steam_art(game: Game) -> None:
#     data = game.steamgriddb.get("steam_platform_data")
#     if not data:
#         return

#     urls = steam_art_urls(data)
#     if not urls:
#         return

#     appid = data["external_platform_data"]["steam"][0]["id"]

#     cache_dir = STEAM_ART_CACHE / appid
#     cache_dir.mkdir(parents=True, exist_ok=True)

#     async with httpx.AsyncClient(follow_redirects=True) as client:
#         for art_type, (_, url) in urls.items():
#             ext = Path(url).suffix or ".jpg"
#             dest = cache_dir / f"{art_type}{ext}"

#             if dest.exists():
#                 continue

#             try:
#                 response = await client.get(url)
#                 response.raise_for_status()
#             except httpx.HTTPError as e:
#                 print(f"Failed to download {art_type}: {e}")
#                 continue

#             dest.write_bytes(response.content)

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

def move_cache(
    old_path: Path,
    new_path: Path,
):
    old_key = str(old_path)
    new_key = str(new_path)

    entry = SIZE_CACHE.pop(old_key, None)
    if entry is None:
        return

    entry["mtime"] = new_path.stat().st_mtime
    SIZE_CACHE[new_key] = entry

    save_cache()

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


async def get_steamgriddb_api_key(app) -> str | None:
    config = load_config()

    key = (
        config.get("steamgriddb", {})
        .get("api_key")
    )

    if key:
        return key

    while True:
        choice = await app.push_screen_wait(
            OptionDialog(
                title="SteamGridDB",
                message=(
                    "SteamGridDB API key is required.\n\n"
                    "Open the API page?"
                ),
                options=[
                    ("Open", "open"),
                    ("Paste Key", "paste"),
                    ("Cancel", "cancel"),
                ],
            )
        )

        if choice == "cancel":
            return None

        if choice == "open":
            webbrowser.open(
                "https://www.steamgriddb.com/profile/preferences/api"
            )
            continue

        if choice == "paste":
            key = await app.push_screen_wait(
                InputDialog(
                    title="SteamGridDB API Key",
                    prompt="Paste your API key:",
                    placeholder="Paste API key...",
                    password=False,
                )
            )

            if not key:
                continue

            config.setdefault(
                "steamgriddb",
                {},
            )["api_key"] = key.strip()

            save_config(config)

            return key.strip()


def extract_exe_icon(exe: Path) -> Path | None:
    key = hashlib.sha1(
        f"{exe.resolve()}:{exe.stat().st_mtime_ns}".encode()
    ).hexdigest()

    out_dir = ICON_CACHE / key
    out_dir.mkdir(parents=True, exist_ok=True)

    pngs = list(out_dir.glob("*.png"))
    if pngs:
        return max(
            pngs,
            key=lambda p: Image.open(p).width,
        )

    try:
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

        ico_files = list(out_dir.glob("*.ico"))

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

        pngs = list(out_dir.glob("*.png"))

        if not pngs:
            return None

        return max(
            pngs,
            key=lambda p: Image.open(p).width,
        )

    except (
        subprocess.CalledProcessError,
        FileNotFoundError,
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
    kind: Literal["png", "ico", "exe"]


def find_icons(root: Path) -> list[IconCandidate]:
    icons: list[IconCandidate] = []
    best_exe: Path | None = None

    for current, dirs, files in os.walk(root):
        depth = len(Path(current).relative_to(root).parts)

        if depth > 3:
            dirs[:] = []
            continue

        for file in sorted(files):
            path = Path(current) / file
            name = file.casefold()

            if path.suffix.casefold() == ".exe":
                if (
                    best_exe is None
                    or launcher_score(path, root)
                    < launcher_score(best_exe, root)
                ):
                    best_exe = path
                continue

            # Best possible icon
            if name == "window_icon.png":
                icons.append(
                    IconCandidate(
                        score=(0, 0),
                        path=path,
                        kind="png",
                    )
                )
                continue

            # icon256.png / icon-512.png / icon_128.png
            if match := re.search(r"icon[-_]?(\d+)", name):
                size = int(match.group(1))
                icons.append(
                    IconCandidate(
                        score=(1, -size),
                        path=path,
                        kind="png",
                    )
                )
                continue

            # Fallbacks
            if name == "icon.png":
                icons.append(
                    IconCandidate(
                        score=(2,0),
                        path=path,
                        kind="png",
                    )
                )
                continue

            if name == "android-icon_foreground.png":
                icons.append(
                    IconCandidate(
                        score=(3,0),
                        path=path,
                        kind="png",
                    )
                )
                continue

            if path.suffix.casefold() == ".ico":
                icons.append(
                    IconCandidate(
                        score=(4,0),
                        path=path,
                        kind="ico",
                    )
                )

    if best_exe is not None:
        icons.append(
            IconCandidate(
                score=(5,) + launcher_score(best_exe, root),
                path=best_exe,
                kind="exe",
            )
        )

    icons.sort()
    return icons


def find_icon(root: Path) -> Path:
    icons = find_icons(root)

    if not icons:
        return ICON_FALLBACK

    best = icons[0]

    if best.kind == "exe":
        if icon := extract_exe_icon(best.path):
            return icon
        return ICON_FALLBACK

    return best.path

def launcher_score(path: Path, root: Path):
    name = path.stem.casefold()

    score = 0

    if any(bad in name for bad in BAD_LAUNCHER_NAMES):
        score += 1000

    return (
        score,
        len(path.relative_to(root).parts),
        LAUNCHER_EXTS.index(path.suffix),
        natural_key(path.stem),
    )


def make_shortcut(game: Game) -> dict:
    launcher = game.launcher
    assert launcher is not None
    info = get_launch_info(game, for_steam=True);


    return {
        "appid": generate_appid(launcher, game.name),
        "AppName": game.name,
        "Exe": f'"{info.cmd[0]}"',
        "StartDir": f'"{info.cwd}"',
        "icon": str(game.icon) if game.icon is not None else "",
        "ShortcutPath": "",
        "LaunchOptions": " ".join(
            shlex.quote(arg)
            for arg in info.cmd[1:]
        ),
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

def shortcuts_path(user: SteamUser) -> Path:
    return (
        STEAM_DIR
        / "userdata"
        / user.userdata_id
        / "config"
        / "shortcuts.vdf"
    )

def load_shortcuts(user: SteamUser) -> dict:
    path = shortcuts_path(user)

    if not path.exists():
        return {"shortcuts": {}}

    try:
        with path.open("rb") as f:
            return vdf.binary_load(f)
    except Exception:
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
    if (
        len(exe) >= 2
        and exe[0] == exe[-1]
        and exe[0] in "\"'"
    ):
        exe = exe[1:-1]

    if exe.startswith("env "):
        parts = shlex.split(exe)

        for part in parts[1:]:
            if "=" not in part:
                return part

        return ""

    return exe
def shortcut_target(entry: dict) -> str:
    exe = normalize_exe(
        entry.get("Exe")
        or entry.get("exe")
        or ""
    )

    launch_options = (
        entry.get("LaunchOptions")
        or entry.get("launchoptions")
        or ""
    )

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


def find_existing_shortcut(
    entries: dict,
    game: Game,
):
    target_name = game.name.casefold()
    target_exe = (
        normalize_exe(str(game.launcher))
        if game.launcher
        else ""
    )

    # First look for matching launcher
    for key, entry in entries.items():
        exe = shortcut_target(entry)

        if exe == target_exe:
            return key

    # Then fall back to matching launcher signature
    target = launcher_signature(target_exe)

    for key, entry in entries.items():
        exe = shortcut_target(entry)

        if launcher_signature(exe) == target:
            return key

    # Finally fall back to matching game name
    for key, entry in entries.items():
        name = (
            entry.get("AppName")
            or entry.get("appname")
            or ""
        ).casefold()

        if name == target_name:
            return key

    return None

def generate_appid(exe: Path, stable_id: str) -> str:
    crc = (
        binascii.crc32(
            (str(exe) + stable_id).encode("utf-8")
        )
        | 0x80000000
    )

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
        int(part)
        if part.isdigit()
        else part.casefold()
        for part in re.split(r"(\d+)", s)
    ]

def launch_path(game: Game) -> Path:
    # Only games in shared can have a local mirror.
    if game.path.parent == SHARED_DIR:
        local = LOCAL_SHARED_DIR / game.name

        if local.is_dir():
            return local

    return game.path

def find_launcher(path: Path) -> Path | None:
    launchers: list[Path] = []

    for p in path.iterdir():
        if p.is_file() and p.suffix in LAUNCHER_EXTS:
            launchers.append(p)

        if p.is_dir():
            for child in p.iterdir():
                if child.is_file() and child.suffix in LAUNCHER_EXTS:
                    launchers.append(child)

    if not launchers:
        return None

    return min(
        launchers,
        key=lambda p: launcher_score(p, path),
    )

def load_size_cache() -> dict:
    try:
        with CACHE_FILE.open() as f:
            return json.load(f)
    except Exception:
        return {}


def save_size_cache(cache: dict):
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)

    with CACHE_FILE.open("w") as f:
        json.dump(cache, f)


SIZE_CACHE = load_size_cache()


def cached_dir_info(path: Path) -> tuple[int, float]:
    key = str(path)

    try:
        mtime = path.stat().st_mtime
    except Exception:
        return 0, 0.0

    cached = SIZE_CACHE.get(key)
    
    last_played = (
        cached.get("last_played", 0.0)
        if cached
        else 0.0
    )

    steamgriddb = (
        cached.get("steamgriddb", {})
        if cached
        else {}
    )

    if (
        cached is not None
        and cached["mtime"] == mtime
    ):
        return (        
            cached["size"],
            cached["mtime"],
            last_played,
            steamgriddb,
        )
    size = dir_size(path)

    

    SIZE_CACHE[key] = {
        "mtime": mtime,
        "size": size,
        "last_played": last_played,
        "steamgriddb": steamgriddb,
    }

    save_size_cache(SIZE_CACHE)

    return size, mtime, last_played, steamgriddb


def game_list_changed(
        path: Path,
        games: list[Game],
    ) -> bool:
    cached = {
        (g.name, g.mtime)
        for g in games
    }

    current = {
        (
            p.name,
            p.stat().st_mtime,
        )
        for p in path.iterdir()
        if p.is_dir()
    }
    

    return current != cached

def local_sync_status():
    if not LOCAL_SHARED_DIR.exists():
        return "Local copy missing"

    remote = {p.name for p in REMOTE_SHARED_DIR.iterdir() if p.is_dir()}
    local = {p.name for p in LOCAL_SHARED_DIR.iterdir() if p.is_dir()}

    if remote == local:
        return "Synced"

    missing = remote - local
    extra = local - remote

    parts = []
    if missing:
        parts.append(f"{len(missing)} missing locally")
    if extra:
        parts.append(f"{len(extra)} extra locally")

    return ", ".join(parts)

def fmt_size(num: int) -> str:
    gb = num / (1024 ** 3)

    if gb >= 1:
        return f"{gb:.1f} GB"

    return f"{num / (1024 ** 2):.0f} MB"


def dir_size(path: Path) -> int:
    try:
        result = subprocess.run(
            ["du", "-sb", str(path)],
            capture_output=True,
            text=True,
            check=True,
        )
        return int(result.stdout.split()[0])
    except Exception:
        return 0

def latest_activity(path: Path) -> float:
    newest = 0.0

    try:
        for p in path.rglob("*"):

            if not p.is_file():
                continue

            if any(part in IGNORE_DIRS for part in p.parts):
                continue

            try:
                mtime = p.stat().st_mtime
            except Exception:
                continue

            # prioritize launcher-like files
            if p.suffix in LAUNCHER_EXTS:
                newest = max(newest, mtime + 10_000_000)

            newest = max(newest, mtime)

    except Exception:
        pass

    if newest > 0:
        return newest

    return path.stat().st_mtime

def scan_games(base: Path) -> list[Game]:
    games = []

    if not base.exists():
        return games

    for item in sorted(base.iterdir()):

        if not item.is_dir():
            continue

        # ignore hidden dirs
        if item.name.startswith("."):
            continue

        print(f"Scanning {item.name}")

        games.append(
            Game(
                name=item.name,
                path=item,
                size=0,
                mtime=0,#latest_activity(item),
                last_played=0.0,
            )
        )

    sort_games(games)
    return games

def sort_games(games: list[Game]):
    games.sort(
        key=lambda g: (
            -g.last_played,
            natural_key(g.name),
        )
    )

def verify_storage():
    if not POOL_DIR.is_mount():
        raise RuntimeError(f"{POOL_DIR} is not mounted!")

    if not SHARED_DIR.exists():
        raise RuntimeError(f"{SHARED_DIR} is missing!")

    if not ARCHIVED_DIR.exists():
        raise RuntimeError(f"{ARCHIVED_DIR} is missing!")

# ============================================================
# UI
# ============================================================

class GameRow(ListItem):

    def __init__(self, game: Game):
        self.game = game
        self.label = Static()

        super().__init__(self.label)

        self.refresh_row()

    def build_line(self) -> str:

        mark = "[*]" if self.game.selected else "[ ]"

        date = time.strftime(
            "%Y-%m-%d",
            time.localtime(self.game.mtime)
        )

        size = fmt_size(self.game.size)

        if self.game.duplicate_steam:
            steam = "D"
        elif self.game.steam_broken:
            steam = "!"
        elif self.game.in_steam:
            steam = "S"
        else:
            steam = " "

        if self.game.steamgriddb and self.game.steamgriddb.get("game", {}).get("id"):
            sgdb = "G"        
        elif self.game.steamgriddb and self.game.steamgriddb.get("id"): #lowercase g needs to be refreshed
            sgdb = "g"
        else:
            sgdb = " "

        return (
            f"{mark} "
            f"{self.game.name:<45.45} "
            f"{size:>8} "
            f"{date} "
            f"{steam} "
            f"{sgdb} "
        )

    def refresh_row(self):
        self.label.update(
            self.build_line()
        )
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
            parts.append(
                f"Steam Exe: {self.game.steam_entry['Exe']}")
            launch_options = self.game.steam_entry.get("LaunchOptions", "")
            if launch_options:
                parts.append(
                    f"Steam LaunchOptions: {launch_options}"
                )
        if self.game.steamgriddb:
            if name := self.game.steamgriddb.get("name"):
                parts.append(f"SteamGridDB: {name}")

            if search := self.game.steamgriddb.get("search"):
                parts.append(f"SteamGridDB Search: {search}")

            if game_id := self.game.steamgriddb.get("id"):
                parts.append(f"SteamGridDB ID: {game_id}")
            # if game_details := self.game.steamgriddb.get("game"):
            #     parts.append("")
            #     parts.append("SteamGridDB Game Details")

            #     add_dict(parts, game_details)
            # if steam_platform_data := self.game.steamgriddb.get("steam_platform_data"):
            #     parts.append("")
            #     parts.append("SteamGridDB Steam Platform Data")

            #     add_dict(parts, steam_platform_data)
        self.tooltip = "\n".join(part for part in parts if part)


# ============================================================
# APP
# ============================================================

class GameArchiver(App):

    CSS = """
    Screen {
        layout: vertical;
    }

    #panes {
        height: 1fr;
    }

    ListView {
        width: 1fr;
        border: solid white;
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
    """

    BINDINGS = [
        Binding("tab", "switch_pane", "Switch"),
        Binding("space", "toggle", "Toggle"),
        Binding("a", "auto_select", "Auto"),
        Binding("m", "move_selected", "Move"),
        Binding("r", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
        Binding("l", "launch", "Launch"),
        Binding("o", "open_folder", "Open"),
        Binding("p", "open_remote_folder", "Open Remote"),
        Binding("t", "terminal", "Terminal"),
        Binding("s", "remote_terminal", "SSH"),
        Binding("u", "sync_shortcut", "Sync Steam"),
        Binding("ctrl+a", "select_all", "Select All"),
        Binding("g", "download_steamgriddb", "SteamGridDB"),
    ]


    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        self.computing_sizes = True
        self.detecting_steam = True
        self.dialog_open = False
        self.game_rows = {}
        self.sync_status = "Unknown"

        self.shared_games: list[Game] = []
        self.archived_games: list[Game] = []
        self.steam_users = get_steam_users()
        self.selected_steam_user = 0

    def sync_shortcut(self, game: Game) -> bool:
        if game.duplicate_steam:
            return False
        user = self.current_steam_user()

        if user is None:
            return False

        if game.launcher is None:
            return False

        shortcuts = load_shortcuts(user)
        entries = shortcuts["shortcuts"]

        key = game.steam_key

        desired = make_shortcut(game)

        changed = False

        if key is None:
            index = (
                max((int(k) for k in entries), default=-1)
                + 1
            )

            entries[str(index)] = desired
            changed = True

        else:
            existing = entries[key]

            for field, value in desired.items():
                if existing.get(field) != value:
                    existing[field] = value
                    changed = True

        if changed:
            save_shortcuts(
                user,
                shortcuts,
            )

        game.steam_key = key
        game.steam_entry = entries.get(key) if key else None
        game.in_steam = key is not None

        return changed
   
    def can_modify_steam(self) -> bool:
        if self.detecting_steam:
            self.notify(
                "Please wait for the Steam scan to finish."
            )
            return False
        if steam_running():
            self.notify(
                "Steam is running. Close Steam before modifying shortcuts."
            )
            return False

        return True

    def update_game_steam_status(
        self,
        game: Game,
        entries: dict,
    ):
        if game.launcher is None:
            game.launcher = find_launcher(launch_path(game))

        if game.icon is None:
            game.icon = find_icon(launch_path(game))

        game.in_steam = False
        game.steam_broken = False
        game.duplicate_steam = False
        game.steam_key = None
        game.steam_entry = None

        if game.launcher is None:
            return

        key = find_existing_shortcut(
            entries,
            game,
        )

        if key is None:
            return

        game.in_steam = True

        entry = entries[key]
        exe = shortcut_target(entry)
        expected = normalize_exe(str(game.launcher))

        if exe != expected:
            if Path(exe).exists():
                game.duplicate_steam = True
                self.notify(
                    f"{repr(exe)} == {repr(expected)}"
                )
                self.notify(
                    f"{len(exe)} == {len(expected)}"
                )
            else:
                game.steam_broken = True

        game.steam_key = key
        game.steam_entry = entry

    def compute_steam_status(self):
        user = self.current_steam_user()

        if user is None:
            return

        shortcuts = load_shortcuts(user)
        entries = shortcuts["shortcuts"]

        for game in self.shared_games + self.archived_games:
            self.update_game_steam_status(
                game,
                entries,
            )

            self.call_from_thread(
                self.refresh_game_row,
                game,
            )

        self.call_from_thread(self.steam_scan_finished)
        self.call_from_thread(self.update_status)

    def start_steam_scan(self):
        self.run_worker(
            self.compute_steam_status,
            thread=True,
            name="compute-steam",
        )

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
        if (
            game_list_changed(
                SHARED_DIR,
                self.shared_games,
            )
            or game_list_changed(
                ARCHIVED_DIR,
                self.archived_games,
            )
        ):
            self.action_refresh()
            return
        self.refresh_launchers()

    def refresh_launchers(self):
        if not self.can_modify_steam():
            return

        for game in self.shared_games + self.archived_games:
            launcher = find_launcher(launch_path(game))

            if (launcher != game.launcher):
                game.launcher = launcher
                game.icon = find_icon(launch_path(game))

                if game.in_steam:
                    self.sync_shortcut(game)

                self.refresh_game_row(game)

    def compose(self) -> ComposeResult:
        yield Header()

        with Horizontal(id="panes"):
            self.shared_view = ListView(id="shared")
            yield self.shared_view

            self.archived_view = ListView(id="archived")
            yield self.archived_view

        yield Static(id="status")

        yield Footer()
        
    def refresh_game_row(self, game: Game):
        for row in self.shared_view.children:
            if row.game is game:
                row.refresh_row()
                self.update_status()
                return

        for row in self.archived_view.children:
            if row.game is game:
                row.refresh_row()
                self.update_status()
                return

    def compute_sizes(self):
        for game in self.shared_games:
            game.size, game.mtime, game.last_played, game.steamgriddb = cached_dir_info(game.path)
            self.call_from_thread(self.refresh_game_row, game)

        for game in self.archived_games:
            game.size, game.mtime, game.last_played, game.steamgriddb = cached_dir_info(game.path)
            self.call_from_thread(self.refresh_game_row, game)

        self.call_from_thread(self.sizes_finished)
        self.call_from_thread(self.update_status)
        self.call_from_thread(self.resort_games)

    def resort_games(self):
        game = self.get_highlighted_game()
        sort_games(self.shared_games)
        sort_games(self.archived_games)
        self.refresh_views(game)

    def sizes_finished(self):
        self.computing_sizes = False

    def steam_scan_finished(self):
        self.detecting_steam = False

    def start_scan(self):
        self.computing_sizes = True
        self.detecting_steam = True
        self.refresh_bindings()
        self.update_status()

        self.run_worker(
            self.compute_sizes,
            thread=True,
            name="compute-sizes",
        )

    def on_mount(self):
        self.shared_view.border_title = " Shared / Synced "
        self.archived_view.border_title = " Archived / Remote "
        verify_storage()

        self.shared_games = scan_games(SHARED_DIR)
        self.archived_games = scan_games(ARCHIVED_DIR)

        self.shared_view = self.query_one("#shared", ListView)
        self.archived_view = self.query_one("#archived", ListView)

        self.refresh_views()

        self.shared_view.focus()

        self.start_scan()
        self.start_steam_scan()
        
        self.set_interval(
            5,
            self.refresh_sync_status,
        )
        self.set_interval(
            60,
            self.refresh_if_changed,
        )
        if len(self.steam_users) > 1:
            self.bind("[", "prev_steam_user", "Steam-")
            self.bind("]", "next_steam_user", "Steam+")
        self.refresh_bindings()

    # ========================================================

    def get_highlighted_game(self) -> Game | None:
        view = self.current_view()

        if view.index is None:
            return None

        row = view.children[view.index]
        game = row.game
        return game

    def refresh_views(
            self,
            highlighted_game: Game | None = None,
        ):
        self.shared_view.clear()
        self.archived_view.clear()

        self.shared_view.extend(
            [GameRow(g) for g in self.shared_games]
        )

        self.archived_view.extend(
            [GameRow(g) for g in self.archived_games]
        )

        self.update_status()

        if highlighted_game is not None:
            for view in (
                self.shared_view,
                self.archived_view,
            ):
                for i, row in enumerate(view.children):
                    if row.game.path == highlighted_game.path:
                        view.focus()
                        view.index = i
                        return

    def update_status(self):

        shared_size = sum(g.size for g in self.shared_games)

        selected_archive = sum(
            g.size for g in self.shared_games
            if g.selected
        )

        selected_restore = sum(
            g.size for g in self.archived_games
            if g.selected
        )

        steam = self.current_steam_user()

        if steam is None:
            steam_text = "None"
        else:
            steam_text = steam.display_name


        scan_text = "Computing sizes..." if self.computing_sizes else ""
        steam_scan_text = "Detecting Steam..." if self.detecting_steam else ""

        parts = [
            f"Shared: {fmt_size(shared_size)} / {LIMIT_GB} GB",
            f"To Archive: {fmt_size(selected_archive)}",
            f"To Restore: {fmt_size(selected_restore)}",
            f"Sync: {self.sync_status}",
            f"Steam: {steam_text}",
            f"{scan_text}",
            f"{steam_scan_text}"
        ]

        text = "   ".join(part for part in parts if part)
        self.query_one("#status", Static).update(text)

    def current_view(self) -> ListView:

        if self.shared_view.has_focus:
            return self.shared_view

        return self.archived_view

    # ========================================================

    def action_switch_pane(self):

        if self.shared_view.has_focus:
            self.archived_view.focus()
        else:
            self.shared_view.focus()

    def action_toggle(self):

        view = self.current_view()

        if view.index is None:
            return

        row = view.children[view.index]

        row.game.selected = not row.game.selected

        row.refresh_row()

        self.update_status()

    def action_auto_select(self):
        game_saved = self.get_highlighted_game()
        if self.computing_sizes:
            self.notify(
                "Please wait for size scan to finish."
            )
            return

        for g in self.shared_games:
            g.selected = False

        current = sum(g.size for g in self.shared_games)

        excess = current - LIMIT_BYTES

        if excess <= 0:
            self.notify("Already under limit")
            return

        freed = 0

        for game in sorted(
            self.shared_games,
            key=lambda g: g.mtime
        ):

            if freed >= excess:
                break

            game.selected = True
            freed += game.size

        self.refresh_views(game_saved)

    def action_move_selected(self):
        if not self.can_modify_steam():
            return
        game_saved = self.get_highlighted_game()
        if self.computing_sizes:
            self.notify(
                "Please wait for size scan to finish."
            )
            return

        if self.shared_view.has_focus:
            moving = [g for g in self.shared_games if g.selected]

            if not moving:
                self.notify("No games selected")
                return

            self.move_games(
                self.shared_games,
                ARCHIVED_DIR
            )

        else:
            moving = [g for g in self.archived_games if g.selected]

            if not moving:
                self.notify("No games selected")
                return

            self.move_games(
                self.archived_games,
                SHARED_DIR
            )

        for g in self.shared_games:
            g.selected = False

        for g in self.archived_games:
            g.selected = False

        self.shared_games = scan_games(SHARED_DIR)
        self.archived_games = scan_games(ARCHIVED_DIR)
        self.refresh_views(game_saved)
        self.start_scan()
        self.start_steam_scan()

    def move_games(self, games: list[Game], target: Path):

        moving = [g for g in games if g.selected]

        target.mkdir(
            parents=True,
            exist_ok=True
        )

        for game in moving:
            self.notify(f"Moving {game.name}")
            old_path = game.path

            new_path = target / game.name

            shutil.move(old_path, new_path)
            move_cache(old_path, new_path)

            game.path = new_path
            game.launcher = find_launcher(launch_path(game))
            game.icon = find_icon(launch_path(game))

            self.sync_shortcut(game)


    def action_refresh(self):
        game_saved = self.get_highlighted_game()
        if self.computing_sizes:
            self.notify(
                "Please wait for size scan to finish."
            )
            return

        self.shared_games = scan_games(SHARED_DIR)
        self.archived_games = scan_games(ARCHIVED_DIR)

        self.refresh_views(game_saved)
        self.start_scan()
        self.start_steam_scan()

    def action_launch(self):
        game = self.get_highlighted_game()
        if game is None:
            return

        key = str(game.path)

        SIZE_CACHE.setdefault(key, {})

        SIZE_CACHE[key]["last_played"] = time.time()
        game.last_played = SIZE_CACHE[key]["last_played"]

        save_size_cache(SIZE_CACHE)

        if game in self.shared_games:
            sort_games(self.shared_games)
        else:
            sort_games(self.archived_games)

        self.refresh_views(game)

        info = get_launch_info(game)

        subprocess.Popen(
            info.cmd,
            cwd=info.cwd,
            env=info.env,
            start_new_session=True,
        )

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
        except Exception as e:
            self.notify(f"Open failed: {e}")


    def action_open_remote_folder(self):
        view = self.current_view()

        if view.index is None:
            return

        row = view.children[view.index]
        game = row.game

        path = row.game.path

        try:
            subprocess.Popen(
                ["xdg-open", str(path)],
                start_new_session=True,
            )
        except Exception as e:
            self.notify(f"Open failed: {e}")

    def action_terminal(self):
        view = self.current_view()

        if view.index is None:
            return

        row = view.children[view.index]
        game = row.game

        path = launch_path(game)

        self.notify(
            f"Opening terminal in {game.name}"
        )

        subprocess.Popen(
            [
                "alacritty",
                "--working-directory",
                str(path),
            ],
            start_new_session=True,
        )

    def action_remote_terminal(self):
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

        self.selected_steam_user = (
            self.selected_steam_user + 1
        ) % len(self.steam_users)

        self.update_status()


    def action_prev_steam_user(self):
        if len(self.steam_users) <= 1:
            return

        self.selected_steam_user = (
            self.selected_steam_user - 1
        ) % len(self.steam_users)

        self.update_status()

    def action_sync_shortcut(self):
        if not self.can_modify_steam():
            return

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
            if self.sync_shortcut(game):
                updated += 1

        # Reload after any changes
        shortcuts = load_shortcuts(self.current_steam_user())
        entries = shortcuts["shortcuts"]

        for game in games:
            self.update_game_steam_status(game, entries)
            self.refresh_game_row(game)

        self.notify(
            f"Updated {updated} Steam shortcut{'s' if updated != 1 else ''}."
        )

    def action_select_all(self):
        if self.shared_view.has_focus:
            games = self.shared_games
        else:
            games = self.archived_games

        if not games:
            self.notify("No games.")
            return

        for game in games:
            if not game.selected:
                game.selected = True
                self.refresh_game_row(game)

        self.update_status()

    async def link_steamgriddb(
        self,
        game: Game,
        client: SteamGridDBClient,
    ):
        query = steamgriddb_search_name(game)
        results = []
        prompt_for_search = False

        cache_key = str(game.path)
        SIZE_CACHE.setdefault(cache_key, {})
        while True:
            if not prompt_for_search:
                result = await client.search_game(query)

                if not result["success"]:
                    self.notify(result["error"])
                    return

                results = result["data"]

            if not results or prompt_for_search:
                prompt_for_search = False
                query = await self.push_screen_wait(
                    InputDialog(
                        title="SteamGridDB Search",
                        prompt="No matches found. Search for:",
                        value=query,
                    )
                )

                if query is None:
                    return

                query = query.strip()

                game.steamgriddb["search"] = query
                SIZE_CACHE[cache_key]["steamgriddb"] = game.steamgriddb
                save_size_cache(SIZE_CACHE)

                continue

            action, selected = await self.push_screen_wait(
                SelectionDialog(
                    title="SteamGridDB Matches",
                    results=results,
                )
            )

            match action:
                case SelectionDialogAction.CANCEL:
                    return
                case SelectionDialogAction.SEARCH:
                    prompt_for_search = True
                    continue
                case SelectionDialogAction.BROWSE:
                    continue  # show SelectionDialog again as shouldn't happen.                
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

            SIZE_CACHE[cache_key]["steamgriddb"] = game.steamgriddb
            save_size_cache(SIZE_CACHE)

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
            action = await self.push_screen_wait(
                SteamGridDBDialog(game)
            )

            match action:
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
    async def download_steamgriddb_art(
        self,
        game: Game,
        client: SteamGridDBClient,
    ):
        art_data = await client.get_all_art(game)
        local_icons = find_icons(launch_path(game))

        if not art_data["success"]:
            self.notify(art["error"])
            return

        art = art_data["data"]

        while True:
            art_type = await self.push_screen_wait(
                ArtworkTypeDialog(art, local_icons)
            )

            match art_type:
                case ArtworkType.CANCEL:
                    return

                case ArtworkType.GRID_PORTRAIT:
                    artwork = art[ArtworkType.GRID_PORTRAIT]

                case ArtworkType.GRID_SQUARE:
                    artwork = art[ArtworkType.GRID_SQUARE]

                case ArtworkType.GRID_LANDSCAPE:
                    artwork = art[ArtworkType.GRID_LANDSCAPE]

                case ArtworkType.HERO:
                    artwork = art[ArtworkType.HERO]

                case ArtworkType.LOGO:
                    artwork = art[ArtworkType.LOGO]

                case ArtworkType.ICON:
                    artwork = art[ArtworkType.ICON]
            action, selected = await self.push_screen_wait(
                ArtworkSelectionDialog(
                    title=f"SteamGridDB {art_type.name.title()}",
                    artwork=artwork,
                    client=client,
                    local_icons = local_icons if art_type == ArtworkType.ICON else None,
                )
            )
            if action is ArtworkSelectionDialogAction.CANCEL:
                continue  # show ArtworkTypeDialog again
            break
    @work
    async def action_download_steamgriddb(self):
        self.dialog_open = True
        try:
            key = await get_steamgriddb_api_key(self)

            if key is None:
                return

            # Create the client
            client = SteamGridDBClient(key)
            game = self.get_highlighted_game()
            if game is None:
                self.notify("No game highlighted.")
                return

            
            if game.steamgriddb.get("game"):
                await self.manage_steamgriddb(game, client)
            else:
                await self.link_steamgriddb(game, client)
            
        finally:
            self.dialog_open = False


# ============================================================
# STEAM
# ============================================================

STEAM_DIR = Path.home() / ".steam/steam"


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
    loginusers = STEAM_DIR / "config/loginusers.vdf"

    persona_lookup = {}

    if loginusers.exists():
        try:
            with loginusers.open(encoding="utf-8") as f:
                data = vdf.load(f)

            for steamid64, info in data.get("users", {}).items():
                userdata_id = str(int(steamid64) - 76561197960265728)

                persona_lookup[userdata_id] = (
                    info.get("PersonaName")
                    or info.get("AccountName")
                )
        except Exception:
            pass

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
class InputDialog(ModalScreen[str | None]):
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

    def __init__(
        self,
        title: str,
        prompt: str,
        value: str = "",
        placeholder: str = "",
        password: bool = False,
    ):
        super().__init__()
        self.title = title
        self.prompt = prompt
        self.value = value
        self.placeholder = placeholder
        self.password = password

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
                yield Button("Cancel", id="cancel")
                yield Button(
                    "OK",
                    id="ok",
                    variant="primary",
                )

    def on_mount(self):
        self.query_one(Input).focus()

    def on_input_submitted(
        self,
        event: Input.Submitted,
    ):
        self.dismiss(event.value)

    def on_button_pressed(
        self,
        event: Button.Pressed,
    ):
        if event.button.id == "ok":
            self.dismiss(
                self.query_one(Input).value
            )
        else:
            self.dismiss(None)

    def action_dismiss(self):
        self.dismiss(None)

# ============================================================
# Artwork Type Dialog
# ============================================================

class ArtworkTypeDialog(ModalScreen[ArtworkType]):
    DEFAULT_CSS = """
    ArtworkTypeDialog {
        align: center middle;
    }

    #dialog {
        width: 60;
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
    #art-grid Button {
        width: 100%;
    }

    #cancel {
        width: 100%;
        margin-top: 1;
    }   
    """

    def __init__(
        self,
        art: dict,
        local_icons: list[IconCandidate]
    ):
        super().__init__()
        self.art = {
            ArtworkType.GRID_PORTRAIT: [],
            ArtworkType.GRID_LANDSCAPE: [],
            ArtworkType.GRID_SQUARE: [],
            ArtworkType.HERO: [],
            ArtworkType.LOGO: [],
            ArtworkType.ICON: [],
        }

        self.art[ArtworkType.GRID_PORTRAIT]   = art.get(ArtworkType.GRID_PORTRAIT, [])
        self.art[ArtworkType.GRID_SQUARE]     = art.get(ArtworkType.GRID_SQUARE, [])
        self.art[ArtworkType.GRID_LANDSCAPE]  = art.get(ArtworkType.GRID_LANDSCAPE, [])
        self.art[ArtworkType.HERO]   = art.get(ArtworkType.HERO, [])
        self.art[ArtworkType.LOGO]   = art.get(ArtworkType.LOGO, [])
        self.art[ArtworkType.ICON]   = art.get(ArtworkType.ICON, [])

        self.local_icons = local_icons



    def compose(self) -> ComposeResult:
        grids_portrait = len(self.art[ArtworkType.GRID_PORTRAIT])
        grids_square = len(self.art[ArtworkType.GRID_SQUARE])
        grids_landscape = len(self.art[ArtworkType.GRID_LANDSCAPE])
        heroes = len(self.art[ArtworkType.HERO])
        logos = len(self.art[ArtworkType.LOGO])
        icons = len(self.art[ArtworkType.ICON]) + len(self.local_icons)
    
        with Vertical(id="dialog"):
            yield Label("[b]Download Artwork[/b]")
            
            with Grid(id="art-grid"):
                yield Button(
                    f"Grid Portrait ({grids_portrait})",
                    id=ArtworkType.GRID_PORTRAIT,
                    disabled=grids_portrait == 0,
                )

                yield Button(
                    f"Grid Square ({grids_square})",
                    id=ArtworkType.GRID_SQUARE,
                    disabled=grids_square == 0,
                )

                yield Button(
                    f"Grid Landscape ({grids_landscape})",
                    id=ArtworkType.GRID_LANDSCAPE,
                    disabled=grids_landscape == 0,
                )

                yield Button(
                    f"Heroes ({heroes})",
                    id=ArtworkType.HERO,
                    disabled=heroes == 0,
                )

                yield Button(
                    f"Logos ({logos})",
                    id=ArtworkType.LOGO,
                    disabled=logos == 0,
                )

                yield Button(
                    f"Icons ({icons})",
                    id=ArtworkType.ICON,
                    disabled=icons == 0,
                )

            yield Button(
                "Cancel",
                id=ArtworkType.CANCEL,
            )

    def on_button_pressed(
        self,
        event: Button.Pressed,
    ):
        match event.button.id:
            case ArtworkType.GRID_PORTRAIT:
                self.dismiss(ArtworkType.GRID_PORTRAIT)

            case ArtworkType.GRID_SQUARE:
                self.dismiss(ArtworkType.GRID_SQUARE)

            case ArtworkType.GRID_LANDSCAPE:
                self.dismiss(ArtworkType.GRID_LANDSCAPE)

            case ArtworkType.HERO:
                self.dismiss(ArtworkType.HERO)

            case ArtworkType.LOGO:
                self.dismiss(ArtworkType.LOGO)

            case ArtworkType.ICON:
                self.dismiss(ArtworkType.ICON)

            case _:
                self.dismiss(ArtworkType.CANCEL)

    def action_dismiss(self):
        self.dismiss(ArtworkType.CANCEL)


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

    def cache_dir(self) -> Path:
        path = (
            Path.home()
            / ".cache"
            / "game_archiver"
            / "steamgriddb"
            / "thumbs"
        )

        path.mkdir(
            parents=True,
            exist_ok=True,
        )

        return path

    def cache_file(
        self,
        art: dict,
    ) -> Path:
        return self.cache_dir() / f"{art['id']}.png"

    async def show_local_art(self, art: dict):
        if art["kind"] == "exe":
            path = extract_exe_icon(art["path"])
        else:
            path = art["path"]

        if path is None or not path.exists():
            self.image = None
            return

        # load/display image from path
        await self.remove_children()

        await self.mount(
            AutoImage(
                path,
                id="image",
            )
        )


    async def show_art(
        self,
        art: dict,
    ):
        # Don't reload the same artwork.
        if (
            self.art is not None
            and self.art["id"] == art["id"]
        ):
            return

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

        if art.get("source") == "local":
            await self.show_local_art(art)
            return

        if not path.exists():
            result = await self.client.download_file(
                art["thumb"],
                path,
            )

            if not result["success"]:
                await self.remove_children()

                await self.mount(
                    Static(
                        result["error"],
                        id="error",
                    )
                )
                return

        # Replace loading widget with image.
        await self.remove_children()

        await self.mount(
            AutoImage(
                path,
                id="image",
            )
        )

# ============================================================
# Artwork Selection Dialog
# ============================================================
class ArtworkSelectionDialogAction(StrEnum):
    BROWSE = "browse"
    CANCEL = "cancel"
    DOWNLOAD = "download"

class ArtworkSelectionDialog(ModalScreen[tuple[ArtworkSelectionDialogAction, dict | None]]):
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

    def __init__(
        self,
        title: str,
        artwork: list[dict],
        client: SteamGridDBClient,
        local_icons: list[IconCandidate] | None
    ):
        super().__init__()

        self.title = title
        self.artwork = artwork
        self.client = client

        if local_icons:
            for icon in local_icons:
                self.artwork.append({
                    "id": icon.path.name,
                    "path": icon.path,
                    "url": icon.path.as_uri(),      # optional
                    "score": icon.score,
                    "style": "Local",
                    "source": "local",
                    "kind": icon.kind,
                })

    def build_item(
        self,
        art: dict,
    ) -> ListItem:
        details = []

        if art.get("style"):
            details.append(art["style"].title())

        if art.get("width") and art.get("height"):
            details.append(
                f"{art['width']}×{art['height']}"
            )

        if art.get("score") is not None:
            details.append(
                f"★ {art['score']}"
            )

        if art.get("nsfw"):
            details.append("NSFW")

        if art.get("lock"):
            details.append("🔒")

        text = art.get("path", art["id"])

        if isinstance(text, Path):
            text = text.name

        text = f"{text}"

        if details:
            text += "\n    " + " • ".join(details)

        return ListItem(
            Label(text)
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(
                f"[b]{self.title}[/b]"
            )

            with Horizontal(id="body"):

                with Vertical(id="left"):
                    yield ListView(
                        *(
                            self.build_item(a)
                            for a in self.artwork
                        ),
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

        await self.query_one(ArtworkPreview).show_art(
            self.artwork[index]
        )
   
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
        index = self.query_one(
            ListView
        ).index

        if event.button.id == ArtworkSelectionDialogAction.BROWSE:
            if index is not None:
                webbrowser.open(
                    self.artwork[index]["url"]
                )
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

class SelectionDialog(
    ModalScreen[tuple[SelectionDialogAction, dict | None]]
):
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
            year = datetime.datetime.fromtimestamp(release).year
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
class SteamGridDBDialog(ModalScreen[SteamGridDBAction]):
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

    def __init__(
        self,
        game: Game,
    ):
        super().__init__()
        self.game = game

    def compose(self) -> ComposeResult:
        sgdb = self.game.steamgriddb

        yield Vertical(
            Label("[b]SteamGridDB[/b]"),
            Label(
                f"Name: {sgdb.get('name', 'Unknown')}\n"
                f"ID: {sgdb.get('id', 'Unknown')}\n"
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
                Button(
                    "Copy JSON", 
                    id="copy",
                    disabled = self.game.steamgriddb != None
                ),
                Button(
                    "Cancel",
                    id="cancel",
                ),
                id="buttons",
            ),
            id="dialog",
        )

    def on_mount(self) -> None:

        def json_default(obj):
            if isinstance(obj, Path):
                return str(obj)
            raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")
        game_info_json = ""
        if self.game.steamgriddb:
            game_info_json = json.dumps(
                self.game.steamgriddb,
                indent=2,
                sort_keys=True,
                default=json_default,
            )
            self.query_one("#details", TextArea).text = game_info_json

    def on_button_pressed(
        self,
        event: Button.Pressed,
    ):
        match event.button.id:
            case "browse":
                self.dismiss(
                    SteamGridDBAction.BROWSE
                )

            case "download":
                self.dismiss(
                    SteamGridDBAction.DOWNLOAD
                )

            case "change":
                self.dismiss(
                    SteamGridDBAction.CHANGE
                )

            case "copy":
                self.app.copy_to_clipboard(
                    game_info_json
                )

            case "cancel":
                self.dismiss(
                    SteamGridDBAction.CANCEL
                )

    def action_dismiss(self):
        self.dismiss(
            SteamGridDBAction.CANCEL
        )

# ============================================================
# SteamGridDB Client
# ============================================================

def steamgriddb_search_name(game: Game) -> str:
    name = game.name
    if game.steamgriddb and "search" in game.steamgriddb:
        name = game.steamgriddb["search"]

    # Strip common suffixes
    name = re.sub(r"\s*-\s*v[\d.]+.*$", "", name, flags=re.I)
    name = re.sub(r"\s+-\s+PC$", "", name, flags=re.I)
    name = re.sub(r"\bR18\b", "", name, flags=re.I)

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

        game_id = game.steamgriddb.get("game", {}).get("id")

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

        return await self.get(f"{kind}/game/{game_id}")
    
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
        if (
            cached is not None
            and time.time() - cached < 60 * 60 * 24 * 30
        ):
            return {
                "success": True,
                "data": game.steamgriddb["art"],
            }
        cache_key = str(game.path)
        SIZE_CACHE.setdefault(cache_key, {})

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
            if artwork is not None:
                game.steamgriddb["art"][artwork_type].insert(0, artwork)

        game.steamgriddb["cached"] = time.time()

        SIZE_CACHE[cache_key]["steamgriddb"] = game.steamgriddb
        save_size_cache(SIZE_CACHE)

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

        except Exception as e:
            return {
                "success": False,
                "error": str(e),
            }
        

# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    GameArchiver().run()
