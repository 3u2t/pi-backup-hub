from collections import deque
from pathlib import Path
import threading
import time

from .core import now


def transmitted_bytes():
    counters = [path for path in Path('/sys/class/net').glob('*/statistics/tx_bytes') if path.parts[-3] != 'lo']
    if not counters:
        return None
    return sum(int(path.read_text()) for path in counters)


class UploadMeter:
    def __init__(self, reader=transmitted_bytes, clock=time.monotonic):
        self.reader = reader
        self.clock = clock
        self.samples = deque()
        self.lock = threading.Lock()
        self.measured_at = None

    def sample(self):
        try:
            counter = self.reader()
        except (OSError, ValueError):
            counter = None
        moment = self.clock()
        with self.lock:
            if counter is None:
                self.samples.clear()
                self.measured_at = None
                return
            if self.samples and counter < self.samples[-1][1]:
                self.samples.clear()
            self.samples.append((moment, counter))
            while len(self.samples) > 2 and self.samples[1][0] < moment - 10:
                self.samples.popleft()
            self.measured_at = now()

    def snapshot(self):
        with self.lock:
            if len(self.samples) < 2 or self.clock() - self.samples[-1][0] > 6:
                return {'bytes_per_second': None, 'measured_at': self.measured_at, 'scope': 'app'}
            elapsed = self.samples[-1][0] - self.samples[0][0]
            speed = (self.samples[-1][1] - self.samples[0][1]) / elapsed if elapsed > 0 else None
            return {'bytes_per_second': speed, 'measured_at': self.measured_at, 'scope': 'app'}
