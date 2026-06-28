#!/usr/bin/env python3

from __future__ import annotations

import shutil
import time
import subprocess
import json
from dataclasses import dataclass
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Footer, Header, ListItem, ListView, Static



# ============================================================
# CONFIG
# ============================================================

LOCAL_SHARED_DIR = Path.home() / "Games/shared"
REMOTE_SHARED_DIR = Path("/mnt/media/pool/Games/shared")
REMOTE_ARCHIVED_DIR = Path("/mnt/media/pool/Games/archived")
POOL_DIR = Path("/mnt/media/pool")
CACHE_FILE = Path.home() / ".cache/game_archiver_sizes.json"

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


# ============================================================
# MODEL
# ============================================================

@dataclass
class Game:
    name: str
    path: Path
    size: int
    mtime: float
    selected: bool = False


# ============================================================
# HELPERS
# ============================================================

def launch_path(game: Game) -> Path:
    # Only games in shared can have a local mirror.
    if game.path.parent == SHARED_DIR:
        local = LOCAL_SHARED_DIR / game.name

        if local.is_dir():
            return local

    return game.path

def find_launcher(path: Path) -> Path | None:
    launchers = []

    for p in path.iterdir():
        if not p.is_file():
            continue

        if p.suffix == ".sh":
            launchers.append(p)
        elif p.suffix in {
            ".AppImage",
            ".x86_64",
        }:
            launchers.append(p)

    if not launchers:
        return None

    return launchers[0]

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

    if (
        cached is not None
        and cached["mtime"] == mtime
    ):
        return cached["size"], cached["mtime"]

    size = dir_size(path)

    SIZE_CACHE[key] = {
        "mtime": mtime,
        "size": size,
    }

    save_size_cache(SIZE_CACHE)

    return size, mtime
        

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

    launcher_exts = {
        ".sh",
        ".exe",
        ".AppImage",
        ".x86_64",
    }

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
            if p.suffix in launcher_exts:
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
            )
        )

    return games

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

        return (
            f"{mark} "
            f"{self.game.name:<45.45} "
            f"{size:>8} "
            f"{date}"
        )

    def refresh_row(self):
        self.label.update(
            self.build_line()
        )


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
    """

    BINDINGS = [
        Binding("tab", "switch_pane", "Switch"),
        Binding("space", "toggle", "Toggle"),
        Binding("a", "auto_select", "Auto"),
        Binding("m", "move_selected", "Move"),
        Binding("r", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
        Binding("l", "launch", "Launch"),
    ]


    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        self.computing_sizes = True
        self.game_rows = {}

        self.shared_games: list[Game] = []
        self.archived_games: list[Game] = []

    def compose(self) -> ComposeResult:
        yield Header()

        with Horizontal(id="panes"):
            yield ListView(id="shared")
            yield ListView(id="archived")

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
            game.size, game.mtime = cached_dir_info(game.path)
            self.call_from_thread(self.refresh_game_row, game)

        for game in self.archived_games:
            game.size, game.mtime = cached_dir_info(game.path)
            self.call_from_thread(self.refresh_game_row, game)

        self.call_from_thread(self.sizes_finished)
        self.call_from_thread(self.update_status)

    def sizes_finished(self):
        self.computing_sizes = False


    def start_scan(self):
        self.computing_sizes = True
        self.refresh_bindings()
        self.update_status()

        self.run_worker(
            self.compute_sizes,
            thread=True,
            name="compute-sizes",
        )

    def on_mount(self):
        verify_storage()

        self.shared_games = scan_games(SHARED_DIR)
        self.archived_games = scan_games(ARCHIVED_DIR)

        self.shared_view = self.query_one("#shared", ListView)
        self.archived_view = self.query_one("#archived", ListView)

        self.refresh_views()

        self.shared_view.focus()

        self.start_scan()

    # ========================================================

    def refresh_views(self):

        self.shared_view.clear()
        self.archived_view.clear()

        self.shared_view.extend(
            [GameRow(g) for g in self.shared_games]
        )

        self.archived_view.extend(
            [GameRow(g) for g in self.archived_games]
        )

        self.update_status()

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


        scan_text = "    Computing sizes..." if self.computing_sizes else ""


        sync = local_sync_status()

        text = (
            f"Shared: {fmt_size(shared_size)} / {LIMIT_GB} GB    "
            f"To Archive: {fmt_size(selected_archive)}    "
            f"To Restore: {fmt_size(selected_restore)}    "
            f"Sync: {sync}"
            f"{scan_text}"
        )

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

        self.refresh_views()

    def action_move_selected(self):
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
        self.refresh_views()
        self.start_scan()

    def move_games(self, games: list[Game], target: Path):

        moving = [g for g in games if g.selected]

        target.mkdir(
            parents=True,
            exist_ok=True
        )

        for game in moving:

            self.notify(f"Moving {game.name}")

            shutil.move(
                str(game.path),
                str(target / game.path.name)
            )


    def action_refresh(self):
        if self.computing_sizes:
            self.notify(
                "Please wait for size scan to finish."
            )
            return

        self.shared_games = scan_games(SHARED_DIR)
        self.archived_games = scan_games(ARCHIVED_DIR)

        self.refresh_views()
        self.start_scan()

    def action_launch(self):
        view = self.current_view()

        if view.index is None:
            return

        row = view.children[view.index]
        game = row.game

        launch_dir = launch_path(game)
        launcher = find_launcher(launch_dir)

        if launcher is None:
            self.notify("No launcher found")
            return
        source = (
            "local"
            if launch_dir.parent == LOCAL_SHARED_DIR
            else "remote"
        )

        self.notify(
            f"Launching {game.name} ({source})"
        )

        if launcher.suffix == ".sh":
            cmd = ["bash", str(launcher)]
        else:
            cmd = [str(launcher)]
        subprocess.Popen(
            cmd,
            cwd=game.path,
            start_new_session=True,
        )

# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    GameArchiver().run()
