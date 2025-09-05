import ctypes, json, os, platform, shutil, signal, sys, threading, time
import ctypes.wintypes as wt
from datetime import datetime
from pathlib import Path

STD_INPUT_HANDLE = -10
STD_OUTPUT_HANDLE = -11

ENABLE_QUICK_EDIT = 0x0040
ENABLE_INSERT_MODE = 0x0020
ENABLE_EXTENDED_FLAGS = 0x0080

kernel32 = ctypes.windll.kernel32


class CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt._COORD),
        ("dwCursorPosition", wt._COORD),
        ("wAttributes", wt.WORD),
        ("srWindow", wt.SMALL_RECT),
        ("dwMaximumWindowSize", wt._COORD),
    ]


def _get_handle(kind: int):
    h = kernel32.GetStdHandle(kind)
    if h == wt.HANDLE(-1).value:
        raise OSError("Failed to get console handle")
    return h


def harden_console():
    try:
        h_stdin = _get_handle(STD_INPUT_HANDLE)
        mode = wt.DWORD()
        kernel32.GetConsoleMode(h_stdin, ctypes.byref(mode))
        new_mode = mode.value
        new_mode &= ~ENABLE_QUICK_EDIT
        new_mode &= ~ENABLE_INSERT_MODE
        new_mode |= ENABLE_EXTENDED_FLAGS
        kernel32.SetConsoleMode(h_stdin, new_mode)

        h_stdout = _get_handle(STD_OUTPUT_HANDLE)
        csbi = CONSOLE_SCREEN_BUFFER_INFO()
        kernel32.GetConsoleScreenBufferInfo(h_stdout, ctypes.byref(csbi))
        win_width = csbi.srWindow.Right - csbi.srWindow.Left + 1
        win_height = csbi.srWindow.Bottom - csbi.srWindow.Top + 1
        size = wt._COORD(win_width, win_height)
        kernel32.SetConsoleScreenBufferSize(h_stdout, size)
    except Exception:
        pass


def set_title(text: str):
    try:
        kernel32.SetConsoleTitleW(str(text))
    except Exception:
        pass


SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "auto_cleaner.config.json"
LOG_PATH = SCRIPT_DIR / "auto_cleaner.log"

FORBIDDEN_PREFIXES = [
    Path("C:/"),
    Path("C:/Windows"),
    Path("C:/Windows/System32"),
    Path("C:/Program Files"),
    Path("C:/Program Files (x86)"),
    Path(os.path.expandvars("%USERPROFILE%")),
]

SAFE_MIN_DEPTH = 3


def is_windows_10():
    return platform.system() == "Windows" and platform.release() in {"10", "11"}


def eprint(*args):
    print(*args, file=sys.stderr)


def load_config():
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {
                    "folder": Path(data["folder"]).resolve(strict=False),
                    "keep_list": [str(x) for x in data.get("keep_list", [])],
                    "interval": int(data.get("interval", 3600)),
                }
        except Exception:
            return None
    return None


def save_config(folder: Path, keep_list: list, interval: int):
    data = {"folder": str(folder), "keep_list": keep_list, "interval": interval}
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def prompt_config():
    print("\nAuto Config")
    while True:
        folder_in = input("1) Path to folder: ").strip().strip('"')
        p = Path(folder_in).expanduser().resolve(strict=False)
        if validate_folder(p):
            break
        print("Invalid path. Try again.")
    keep_list = []
    while True:
        keep_item = input(
            "2) Item to KEEP (file or folder name, leave blank to stop): "
        ).strip()
        if not keep_item:
            break
        if validate_filename(keep_item):
            keep_list.append(keep_item)
        else:
            print("Invalid name. Try again.")
    while True:
        try:
            interval = int(
                input("3) Delay between cleanups in seconds (default 3600): ") or "3600"
            )
            if interval > 0:
                break
        except Exception:
            pass
        print("Invalid number. Try again.")
    save_config(p, keep_list, interval)
    return {"folder": p, "keep_list": keep_list, "interval": interval}


def validate_folder(p: Path):
    if not p.exists() or not p.is_dir():
        return False
    if p.anchor and p == Path(p.anchor):
        return False
    parts = [part for part in p.resolve().parts if part not in ("\\", "/")]
    if len(parts) < SAFE_MIN_DEPTH:
        return False
    rp = p.resolve()
    for bad in FORBIDDEN_PREFIXES:
        try:
            if str(rp).lower() == str(bad.resolve()).lower():
                return False
        except Exception:
            continue
    return True


def validate_filename(name: str):
    if not name or len(name) > 255:
        return False
    bad = set('<>:"/\\|?*')
    return not any(ch in bad for ch in name)


class Spinner:
    def __init__(self, text: str = "", interval: float = 0.1):
        self.text = text
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def start(self):
        self._stop.clear()
        if not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def _run(self):
        i = 0
        while not self._stop.is_set():
            frame = self.frames[i % len(self.frames)]
            sys.stdout.write(f"\r{frame} {self.text}")
            sys.stdout.flush()
            time.sleep(self.interval)
            i += 1
        sys.stdout.write("\r" + " " * (len(self.text) + 4) + "\r")
        sys.stdout.flush()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=1)


def safe_delete_all(folder: Path, keep_list: list) -> dict:
    keep_lower = {name.lower() for name in keep_list}
    deleted, skipped, errors = [], [], []
    for entry in folder.iterdir():
        try:
            if entry.name.lower() in keep_lower:
                skipped.append(entry.name)
                continue
            if entry.is_file():
                if try_delete(entry):
                    deleted.append(entry.name)
                else:
                    errors.append((entry.name, "access denied"))
            elif entry.is_dir():
                try:
                    shutil.rmtree(entry)
                    deleted.append(entry.name + "/")
                except Exception as ex:
                    errors.append((entry.name, str(ex)))
            else:
                skipped.append(entry.name)
        except Exception as ex:
            errors.append((entry.name, str(ex)))
    return {"deleted": deleted, "skipped": skipped, "errors": errors}


def try_delete(path: Path, retries=3, delay=0.25) -> bool:
    for _ in range(retries):
        try:
            os.remove(path)
            return True
        except (PermissionError, FileNotFoundError):
            time.sleep(delay)
    return False


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}\n"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


STOP = threading.Event()


def handle_signal(signum, frame):
    STOP.set()


for s in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", signal.SIGINT)):
    try:
        signal.signal(s, handle_signal)
    except Exception:
        pass


def cleanup_loop(folder: Path, keep_list: list, interval: int):
    while not STOP.is_set():
        banner()
        spinner = Spinner("Cleaning")
        spinner.start()
        stats = safe_delete_all(folder, keep_list)
        spinner.stop()
        print_summary(stats)
        log_summary(stats, folder)
        for _ in range(interval):
            if STOP.is_set():
                break
            time.sleep(1)


def print_summary(stats: dict):
    print(
        f"Deleted {len(stats['deleted'])}, Skipped {len(stats['skipped'])}, Errors {len(stats['errors'])}."
    )
    if stats["errors"]:
        for name, err in stats["errors"][:5]:
            print(f" - {name}: {err}")


def log_summary(stats: dict, folder: Path):
    log(
        f"Folder={folder} deleted={len(stats['deleted'])} errors={len(stats['errors'])}"
    )


def banner():
    cols = 80
    try:
        cols = os.get_terminal_size().columns
    except Exception:
        pass
    title = "DelShop"
    bar = "=" * max(10, min(cols, len(title) + 10))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("\n" + bar)
    print(title)
    print(now)
    print(bar)


def main():
    if not is_windows_10():
        eprint("Windows 10 or 11 required.")
        sys.exit(1)

    harden_console()
    set_title("DelShop")

    cfg = load_config() or prompt_config()
    folder, keep_list, interval = cfg["folder"], cfg["keep_list"], cfg["interval"]

    print("\nFolder:", folder)
    print("Keep:", keep_list or "none")
    print("Interval:", interval, "seconds")

    try:
        cleanup_loop(folder, keep_list, interval)
    finally:
        print("Exiting.")


if __name__ == "__main__":
    main()
