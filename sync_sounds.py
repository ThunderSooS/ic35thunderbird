"""Short, asynchronous Windows notification sounds; no extra dependencies."""
from pathlib import Path

try:
    import winsound
except ImportError:
    winsound = None

SOUNDS = {"start": "start.wav", "press_again": "press_again.wav", "complete": "complete.wav"}


def play(event):
    if winsound is None:
        return
    try:
        path = Path(__file__).parent / "sounds" / SOUNDS[event]
        winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except (OSError, RuntimeError, KeyError):
        # Audio must never interrupt the backup or the synchronization.
        pass
