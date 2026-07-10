import subprocess
import threading
from pathlib import Path

import attr

from compute_space.config import Config
from compute_space.core.logging import logger


def generate_caddyfile(config: Config) -> str:
    """Generate Caddyfile content based on config."""
    if config.tls_enabled:
        zone = config.zone_domain_no_port
        email_line = f"    email {config.acme_email}\n" if config.acme_email else ""
        return (
            "{\n"
            "    admin off\n"
            # ACME must stay enabled for on-demand issuance; we own the :80
            # redirect ourselves via the http:// site below.
            "    auto_https disable_redirects\n"
            "    storage file_system {\n"
            f"        root {config.caddy_storage_dir}\n"
            "    }\n"
            f"{email_line}"
            "    on_demand_tls {\n"
            f"        ask http://127.0.0.1:{config.port}/api/tls/on_demand_check\n"
            "    }\n"
            "}\n"
            # Zone + app subdomains: served from the wildcard cert on disk
            # (DNS-01 acquired); the explicit cert/key means Caddy never
            # attempts ACME for these hosts.
            f"https://{zone}, https://*.{zone} {{\n"
            f"    tls {config.tls_cert_path} {config.tls_key_path}\n"
            "    encode gzip zstd\n"
            f"    reverse_proxy localhost:{config.port}\n"
            "}\n"
            # Any other hostname is a custom (alternate) app domain: Caddy
            # obtains a Let's Encrypt cert on first handshake, gated by the
            # ask endpoint above so only registered domains get certs.
            "https:// {\n"
            "    tls {\n"
            "        on_demand\n"
            "    }\n"
            "    encode gzip zstd\n"
            f"    reverse_proxy localhost:{config.port}\n"
            "}\n"
            # Caddy serves /.well-known/acme-challenge/ on :80 ahead of this site.
            "http:// {\n"
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
            f"    reverse_proxy localhost:{config.port}\n"
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


def start_caddy(config: Config) -> CaddyProcess:
    """Generate Caddyfile and start Caddy."""
    caddyfile_path = config.caddyfile_path
    caddyfile_path.parent.mkdir(parents=True, exist_ok=True)
    caddyfile_path.write_text(generate_caddyfile(config))
    return CaddyProcess(proc=_spawn_caddy(caddyfile_path), caddyfile_path=caddyfile_path)
