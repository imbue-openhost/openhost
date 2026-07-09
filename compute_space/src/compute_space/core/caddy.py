import subprocess
import threading
from pathlib import Path

import attr

from compute_space.core.logging import logger


def generate_caddyfile(tls_enabled: bool, tls_cert_path: Path, tls_key_path: Path, web_server_port: int) -> str:
    """Generate Caddyfile content based on config."""
    if tls_enabled:
        return (
            "{\n"
            "    auto_https off\n"
            "    admin off\n"
            "}\n"
            ":443 {\n"
            f"    tls {tls_cert_path} {tls_key_path}\n"
            "    encode gzip zstd\n"
            f"    reverse_proxy localhost:{web_server_port}\n"
            "}\n"
            ":80 {\n"
            "    redir https://{host}{uri} permanent\n"
            "}\n"
        )
    else:
        return (
            "{\n"
            "    auto_https off\n"
            "    admin off\n"
            "}\n"
            ":80 {\n"
            "    encode gzip zstd\n"
            f"    reverse_proxy localhost:{web_server_port}\n"
            "}\n"
        )


def _spawn_caddy(caddyfile_path: Path) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(
        ["caddy", "run", "--config", str(caddyfile_path), "--adapter", "caddyfile"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    def _stream_caddy_logs(proc: subprocess.Popen[bytes]) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            logger.info(f"[caddy] {line.decode(errors='replace').rstrip()}")
        proc.wait()
        logger.warning(f"Caddy exited with code {proc.returncode}")

    threading.Thread(target=_stream_caddy_logs, args=(proc,), daemon=True).start()
    logger.info(f"Started Caddy (pid {proc.pid})")
    return proc


@attr.s(auto_attribs=True)
class CaddyProcess:
    """Handle to the running Caddy child.  Mutable: restart() replaces proc with a fresh one."""

    proc: subprocess.Popen[bytes]
    caddyfile_path: Path

    def restart(self) -> None:
        """Restart Caddy so it picks up renewed TLS cert files (it runs with `admin off`, so there
        is no live-reload path).  The old process must stop before the new one starts since both
        bind :80/:443.
        """
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                logger.warning(f"Caddy (pid {self.proc.pid}) did not exit after terminate, killing")
                self.proc.kill()
                self.proc.wait()
        self.proc = _spawn_caddy(self.caddyfile_path)


def start_caddy(
    caddyfile_path: Path, tls_enabled: bool, tls_cert_path: Path, tls_key_path: Path, web_server_port: int
) -> CaddyProcess:
    """Generate Caddyfile and start Caddy."""
    caddyfile_path.parent.mkdir(parents=True, exist_ok=True)
    caddyfile_path.write_text(generate_caddyfile(tls_enabled, tls_cert_path, tls_key_path, web_server_port))
    return CaddyProcess(proc=_spawn_caddy(caddyfile_path), caddyfile_path=caddyfile_path)
