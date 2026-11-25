"""What the app keeps for its background loops: whether they go on (stopped), and their wake-up (kick)."""
import threading


class IdleEngine:
    stopped = False

    def __init__(self):
        self.kick = threading.Event()
