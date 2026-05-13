"""Pure-Python web terminal: bridges a WebSocket to a PTY running bash."""

import asyncio
import fcntl
import json
import os
import pty
import signal
import struct
import subprocess
import termios
from typing import Protocol

from compute_space.core.logging import logger
from compute_space.core.updates import wait_for_shutdown


class TerminalWebsocket(Protocol):
    """Framework-neutral websocket interface used by the terminal bridge."""

    async def send(self, data: bytes) -> None: ...

    async def receive(self) -> bytes | str: ...


# Track active sessions for cleanup on shutdown.
_active_sessions: dict[int, tuple[subprocess.Popen[bytes], int]] = {}


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    """Set the terminal window size on a PTY file descriptor."""
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


async def handle_terminal_ws(ws: TerminalWebsocket) -> None:
    """Handle a WebSocket connection by bridging it to a PTY running bash.

    Wire protocol:
      Client → Server:
        0x00 + bytes  = terminal input (keystrokes)
        0x01 + JSON   = control message (e.g. {"type":"resize","cols":80,"rows":24})
      Server → Client:
        raw bytes     = terminal output
    """
    master_fd, slave_fd = pty.openpty()
    _set_winsize(master_fd, 24, 80)

    proc = subprocess.Popen(
        ["bash", "-l"],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        preexec_fn=os.setsid,
        env={**os.environ, "TERM": "xterm-256color"},
    )
    os.close(slave_fd)

    session_id = id(ws)
    _active_sessions[session_id] = (proc, master_fd)

    loop = asyncio.get_event_loop()

    async def pty_to_ws() -> None:
        """Read from PTY master fd, send to WebSocket as binary."""
        try:
            while True:
                data = await loop.run_in_executor(None, os.read, master_fd, 4096)
                if not data:
                    break
                await ws.send(data)
        except Exception as e:
            logger.info(f"pty_to_ws ended: {e}")

    async def ws_to_pty() -> None:
        """Read from WebSocket, write to PTY master fd."""
        try:
            while True:
                msg = await ws.receive()
                if isinstance(msg, bytes) and len(msg) > 0:
                    kind = msg[0]
                    payload = msg[1:]
                    if kind == 0x00:
                        os.write(master_fd, payload)
                    elif kind == 0x01:
                        ctrl = json.loads(payload)
                        if ctrl.get("type") == "resize":
                            _set_winsize(master_fd, ctrl["rows"], ctrl["cols"])
                elif isinstance(msg, str):
                    os.write(master_fd, msg.encode())
        except Exception as e:
            logger.info(f"ws_to_pty ended: {e}")

    def _cleanup() -> None:
        _active_sessions.pop(session_id, None)
        try:
            os.kill(proc.pid, signal.SIGHUP)
        except OSError:
            pass
        try:
            os.close(master_fd)
        except OSError:
            pass
        proc.wait()

    try:
        logger.info(f"Terminal session started (pid {proc.pid})")
        tasks = [asyncio.create_task(t) for t in [pty_to_ws(), ws_to_pty(), wait_for_shutdown()]]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        # Kill process and close fd first — this unblocks any executor thread stuck in os.read
        _cleanup()
        for t in pending:
            t.cancel()
    finally:
        _cleanup()


def cleanup_all() -> None:
    """Kill any remaining PTY sessions. Called at shutdown."""
    logger.info("Cleaning up terminal sessions...")
    for _sid, (proc, master_fd) in list(_active_sessions.items()):
        try:
            os.kill(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            os.close(master_fd)
        except OSError:
            pass
        proc.wait()
    _active_sessions.clear()
    logger.info("Terminal cleanup complete.")
