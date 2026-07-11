import threading
import time

from douyin_monitor.pacer import RequestPacer


def test_wait_for_turn_enforces_minimum_spacing():
    stop_event = threading.Event()
    pacer = RequestPacer(min_interval=0.05, max_interval=0.06, stop_event=stop_event)

    timestamps = []
    lock = threading.Lock()

    def worker():
        pacer.wait_for_turn()
        with lock:
            timestamps.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    timestamps.sort()
    assert len(timestamps) == 6
    for earlier, later in zip(timestamps, timestamps[1:]):
        # 允许一点点调度误差，但间隔不应明显小于设定的最小间隔
        assert later - earlier >= 0.05 - 0.01


def test_wait_for_turn_returns_immediately_for_first_call():
    stop_event = threading.Event()
    pacer = RequestPacer(min_interval=1.0, max_interval=2.0, stop_event=stop_event)

    start = time.monotonic()
    pacer.wait_for_turn()
    elapsed = time.monotonic() - start

    assert elapsed < 0.1  # 第一次报到不应该等待
