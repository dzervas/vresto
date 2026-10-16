"""Headful image entry point: own Xvfb, then run the normal service in PID 1.

No desktop/VNC or TCP X11 listener. Local X11 access is trusted only inside this
private container; never share its /tmp X11 sockets with other containers.
The service retains its own SIGTERM handling and closes the browser before Xvfb.
"""
from collections.abc import Iterator
from contextlib import contextmanager
import os
import select
import signal
import subprocess


_RUNTIME_ENV = ('PATH', 'HOME', 'TMPDIR', 'LD_LIBRARY_PATH', 'LANG',
                'FONTCONFIG_FILE', 'FONTCONFIG_PATH')


@contextmanager
def virtual_display() -> Iterator[None]:
    """Own a private display for the service or the opt-in browser diagnostic."""
    def stop(signum, frame):
        raise SystemExit(128 + signum)

    previous = {s: signal.signal(s, stop) for s in (signal.SIGTERM, signal.SIGINT)}
    environment = {k: os.environ.get(k) for k in ('DISPLAY', 'VRESTO_HEADFUL')}
    read_fd, write_fd = os.pipe()
    display = None
    try:
        # -displayfd chooses a free display and reports it only once ready.
        # Do not pass service/discovery secrets to the X server or its children.
        display = subprocess.Popen(['Xvfb', '-displayfd', str(write_fd),
            '-screen', '0', '1280x720x24', '-nolisten', 'tcp', '-ac'],
            pass_fds=(write_fd,), env={k: os.environ[k] for k in _RUNTIME_ENV if k in os.environ},
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        os.close(write_fd)
        write_fd = None
        if not select.select([read_fd], [], [], 5)[0]:
            raise SystemExit('Xvfb startup failed')
        number = os.read(read_fd, 32).strip()
        if not number.isdigit() or display.poll() is not None:
            raise SystemExit('Xvfb startup failed')
        os.environ['DISPLAY'] = ':' + number.decode('ascii')
        os.environ['VRESTO_HEADFUL'] = '1'
        yield
    finally:
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)
        if display is not None:
            if display.poll() is None:
                display.terminate()
            try:
                display.wait(timeout=1)
            except subprocess.TimeoutExpired:
                display.kill()
                display.wait()
        for key, value in environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main() -> None:
    with virtual_display():
        from .server import main as serve
        serve()


if __name__ == '__main__':
    main()
