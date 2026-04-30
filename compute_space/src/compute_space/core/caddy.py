import subprocess
import threading
from pathlib import Path

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


def start_caddy(
    caddyfile_path: Path, tls_enabled: bool, tls_cert_path: Path, tls_key_path: Path, web_server_port: int
) -> subprocess.Popen[bytes]:
    """Generate Caddyfile and start Caddy."""
    caddyfile_path.parent.mkdir(parents=True, exist_ok=True)
    caddyfile_path.write_text(generate_caddyfile(tls_enabled, tls_cert_path, tls_key_path, web_server_port))
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
