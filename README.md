# Game Archiver

A Linux terminal application for managing games across a synced game library and a remote archive.

I built this because I use **Syncthing** to keep games synchronized between my PC, NAS, and Steam Deck. The problem is that the Steam Deck has limited storage, so keeping every game synchronized everywhere isn't practical.

The idea behind Game Archiver is simple: games in the shared directory are synchronized normally, while games that I don't currently need on my PC or Steam Deck can be moved into an archive directory on the NAS.

Archived games remain available on the server and can still be launched remotely, but they are no longer inside the Syncthing directory and therefore aren't synchronized to my other devices.

This is primarily a personal project that I work on here and there. It isn't bug-free, and things may change as I continue working on it.

I also used AI to help build parts of this project. I didn't know about [Textual](https://textual.textualize.io/) before starting this project, and it ended up being a good way to build the terminal interface. The application also supports displaying images directly in the terminal.

<img width="1706" height="922" alt="image" src="https://github.com/user-attachments/assets/8768cbac-e2e7-458b-a500-82ac538f4868" />

>**Note:** Screenshot sanitized for privacy; game names and Steam account information have been replaced with fictional data.

## How It Works

Basic layout:

```text
NAS
└── Games
    ├── shared
    │   ├── Game A
    │   ├── Game B
    │   └── Game C
    └── archived
        ├── Game D
        ├── Game E
        └── Game F
```

Synced folder:

```text
PC ─────────────┐
                │
Steam Deck ─────┼── Syncthing ── NAS/shared
                │
Other devices ──┘
```

Archive folder:

```text
NAS/archived
```

Games in `archived` are not synced, but can still be launched from the NAS.

Moving a game back into `shared` makes it available on all synced devices again.

---

## Features

### Game Archiving

- View games in shared and archive folders
- Move games between shared and archive
- Select multiple games for batch moves
- Auto-select games when storage limit is exceeded
- Show total shared library size
- Show selected archive/restore size
- Detect sync status
- Periodically rescan game folders
- Cache game data to improve performance

---

### Running Games

Game Archiver detects launchers inside game folders.

Supported types:

```text
.sh
.AppImage
.x86_64
.exe
.swf
.apk
```

It can also detect executables without extensions.

Examples:

- `.sh` → run directly
- `.exe` → run via Proton
- `.swf` → run via Ruffle
- `.apk` → install via Waydroid
- AppImages/native binaries → run directly

For Windows games, it can create separate Proton compatibility data outside Steam.

---

### Steam Integration

Game Archiver reads Steam non-Steam shortcuts and can:

- Detect existing shortcuts
- Find outdated paths
- Detect duplicates
- Create missing shortcuts
- Update existing shortcuts

This is useful when moving games between shared and archive folders.

Steam must be closed when modifying shortcuts.

---

### SteamGridDB

Game Archiver can download artwork from [SteamGridDB](https://www.steamgriddb.com/).

Supported artwork:

- Grid / portrait
- Square
- Landscape
- Hero
- Logos
- Icons

Downloaded artwork is cached locally.

A SteamGridDB API key is required. The app can prompt for it when needed.

---

### Terminal Images

The UI uses [Textual](https://textual.textualize.io/) and `textual-image` to display artwork in the terminal.

For best results, use **kitty**:

```bash
kitty
```

Other terminals work, but may show lower-quality or fallback images.

---

## Interface

The application uses a two-pane layout:

```text
┌──────────────────────────────────┬──────────────────────────────────┐
│        Shared / Synced           │        Archived / Remote         │
│                                  │                                  │
│ [ ] Game A                       │ [ ] Game D                       │
│ [ ] Game B                       │ [ ] Game E                       │
│ [ ] Game C                       │ [ ] Game F                       │
│                                  │                                  │
└──────────────────────────────────┴──────────────────────────────────┘
```

The left pane represents games that are in the Syncthing-managed shared directory.

The right pane represents games that have been archived on the remote server.

The status bar provides information such as:

```text
Shared: 218.4 GB / 256 GB
To Archive: 42.7 GB
To Restore: 18.3 GB
Sync: Synced
Steam: <user>
```

Game rows can also show status indicators for Steam and SteamGridDB integration.

## Keyboard Shortcuts

| Key    | Action                    |
| ------ | ------------------------- |
| Tab    | Switch panes              |
| Space  | Select game               |
| A      | Auto-select for archiving |
| M      | Move selected games       |
| R      | Refresh                   |
| Q      | Quit                      |
| L      | Launch game               |
| O      | Open local folder         |
| P      | Open remote folder        |
| T      | Open terminal             |
| S      | Open SSH terminal         |
| U      | Sync Steam shortcut       |
| Ctrl+A | Select all                |
| G      | SteamGridDB               |

---

## Automatic Archiving

The shared library has a storage limit (default: **256 GB**).

If the limit is exceeded, the app automatically selects older or less-used games until enough space is freed.

This is a convenience feature, not a perfect system.

---

## Configuration

Stored in:

```text
~/.config/game_archiver/
```

Main config:

```text
~/.config/game_archiver/config.toml
```

Game data:

```text
~/.config/game_archiver/game_data.json
```

Cache:

```text
~/.cache/game_archiver/
```

Default paths used by this project:

```text
~/Games/shared
/mnt/media/pool/Games/shared
/mnt/media/pool/Games/archived
```

You may need to change these for your setup.

---

## Requirements

Linux only.

Python dependencies:

- Textual
- textual-image
- Pillow
- httpx
- vdf
- tomli-w

Optional external tools:

- Steam
- Proton
- kitty
- Ruffle
- Waydroid
- wrestool
- icotool

---

## Running

```bash
./game_archiver.py
```

Or:

```bash
python game_archiver.py
```

Best experience (images enabled):

```bash
kitty ./game_archiver.py
```

---

## Syncthing Setup

Game Archiver does not replace Syncthing.

Syncthing handles syncing. Game Archiver moves games between shared and archive folders.

Important:

> The archive folder must NOT be inside the Syncthing shared directory.

Otherwise, archiving won’t actually remove games from synced devices.

---

## Moving Games

When moving a game to archive:

1. Move folder
2. Update cached path
3. Re-detect launcher
4. Re-detect icon
5. Update Steam shortcut (if needed)

Moving back reverses the process.

---

## Steam Shortcut Handling

Game Archiver edits Steam’s `shortcuts.vdf`.

It matches shortcuts by:

- Executable path
- Game name
- Launcher signature

Steam must be closed during changes.

---

## Artwork

- Uses Steam artwork when available
- Falls back to SteamGridDB
- Can extract icons from Windows executables

All artwork is cached locally.

---

## Current State

This is not finished software.

It works for my setup, but:

- bugs exist
- paths may need adjustment
- features may change

---

## AI-Assisted Development

AI was used during development.

I also used this project to learn Textual and terminal UI development.

---

## Linux Only

This project is built for Linux and assumes:

- Linux filesystem layout
- Proton/Steam Linux environment
- Syncthing usage
- terminal-based tools

Windows support would require significant changes.

---

## License

This project is released into the public domain under the Unlicense. See the [LICENSE](LICENSE) file for full details.
