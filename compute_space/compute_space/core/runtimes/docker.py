"""Docker implementation of ContainerRuntime.

Shells out to the ``docker`` CLI via ``subprocess``.  Behavior is preserved
verbatim from the pre-runtime-abstraction ``containers.py`` module; this file
is a mechanical extraction with no semantic changes.
"""

from __future__ import annotations

import os
import re
import subprocess

from compute_space.core.logging import logger
from compute_space.core.manifest import AppManifest
from compute_space.core.manifest import PortMapping

# Container mount root — all data dirs live under this prefix so the
# container filesystem stays clean (no data dirs mixed with /bin, /etc, etc).
CONTAINER_ROOT = "/data"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\([AB0-9]|\x1b[=>]|\x0f|\r")
_CACHE_CORRUPT_RE = re.compile(r"content digest sha256:[0-9a-f]+: not found")

# Prefix on RuntimeError messages that signals a corrupted build cache.
# Callers strip this prefix before showing the error to users, but the
# HTTP API uses it to surface a "drop cache" remediation action.  Kept
# in sync with ``compute_space.core.containers.BUILD_CACHE_CORRUPT_MARKER``
# (duplicated here to avoid an import cycle).
_BUILD_CACHE_CORRUPT_MARKER = "[BUILD_CACHE_CORRUPT]"


def _build_log_path(app_name: str, temp_data_dir: str) -> str:
    """Path to the build/deploy log file for an app (in temp data)."""
    # NOTE: historical filename is "docker.log"; kept for backward compatibility
    # with already-deployed instances.  Phase 1 of the runtime migration will
    # rename to "build.log" with a read-side fallback.
    return os.path.join(temp_data_dir, "app_temp_data", app_name, "docker.log")


def _append_log(app_name: str, temp_data_dir: str, text: str) -> None:
    log_file = _build_log_path(app_name, temp_data_dir)
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    with open(log_file, "a") as f:
        f.write(text)


class DockerRuntime:
    """``ContainerRuntime`` backed by the Docker CLI."""

    name = "docker"

    # ------------------------------------------------------------------ build

    def build_image(
        self,
        app_name: str,
        repo_path: str,
        dockerfile_rel_path: str,
        temp_data_dir: str | None = None,
    ) -> str:
        image_tag = f"openhost-{app_name}:latest"
        dockerfile_path = os.path.join(repo_path, dockerfile_rel_path)
        cmd = [
            "docker",
            "build",
            "--progress=plain",
            "-t",
            image_tag,
            "-f",
            dockerfile_path,
            repo_path,
        ]
        logger.info("Building Docker image: %s", " ".join(cmd))

        if temp_data_dir:
            _append_log(app_name, temp_data_dir, f"=== Building image: {image_tag} ===\n")

        if temp_data_dir:
            # Stream build output line-by-line so the frontend can show progress
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            build_output = ""
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    build_output += line
                    _append_log(app_name, temp_data_dir, line)
                proc.wait(timeout=300)
            except subprocess.TimeoutExpired:
                proc.kill()
                raise
            if proc.returncode != 0:
                if _CACHE_CORRUPT_RE.search(build_output):
                    raise RuntimeError(f"{_BUILD_CACHE_CORRUPT_MARKER} Docker build cache is corrupted.")
                # Include the tail of build output so the error is visible in
                # the main router log (the full output is already in the app log).
                tail = build_output[-2000:] if len(build_output) > 2000 else build_output
                raise RuntimeError(f"Docker build failed (exit code {proc.returncode}):\n{tail}")
        else:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                if _CACHE_CORRUPT_RE.search(result.stderr):
                    raise RuntimeError(f"{_BUILD_CACHE_CORRUPT_MARKER} Docker build cache is corrupted.")
                raise RuntimeError(f"Docker build failed:\n{result.stderr}")

        if temp_data_dir:
            _append_log(app_name, temp_data_dir, "=== Build complete ===\n\n")

        return image_tag

    # -------------------------------------------------------------------- run

    def run_container(
        self,
        app_name: str,
        image_tag: str,
        manifest: AppManifest,
        local_port: int,
        env_vars: dict[str, str],
        data_dir: str,
        temp_data_dir: str,
        port_mappings: list[PortMapping] | None = None,
    ) -> str:
        app_data_dir = os.path.join(data_dir, "app_data", app_name)
        app_temp_dir = os.path.join(temp_data_dir, "app_temp_data", app_name)
        vm_data_dir = os.path.join(data_dir, "vm_data")
        container_name = f"openhost-{app_name}"

        # Check which data access the app has (sqlite implies app_data)
        has_app_data = manifest.app_data or manifest.sqlite_dbs or manifest.access_all_data
        has_app_temp = manifest.app_temp_data or manifest.access_all_data
        has_vm_data = manifest.access_vm_data or manifest.access_all_data

        # Container paths follow the logical structure under CONTAINER_ROOT
        c_app_data = f"{CONTAINER_ROOT}/app_data/{app_name}"
        c_app_temp = f"{CONTAINER_ROOT}/app_temp_data/{app_name}"
        c_vm_data = f"{CONTAINER_ROOT}/vm_data"

        # Translate host paths to container paths in env vars
        container_env = {}
        for key, value in env_vars.items():
            if key.startswith("OPENHOST_SQLITE_"):
                rel_path = os.path.relpath(value, app_data_dir)
                container_env[key] = os.path.join(c_app_data, rel_path)
            elif key == "OPENHOST_APP_DATA_DIR":
                container_env[key] = c_app_data
            elif key == "OPENHOST_APP_TEMP_DIR":
                container_env[key] = c_app_temp
            else:
                container_env[key] = value

        cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            container_name,
            "-p",
            f"127.0.0.1:{local_port}:{manifest.container_port}",
            f"--memory={manifest.memory_mb}m",
            f"--cpus={manifest.cpu_millicores / 1000.0}",
            "--restart=unless-stopped",
            "--add-host=host.docker.internal:host-gateway",
        ]

        # Mount data volumes following the logical structure from docs/data.md
        if manifest.access_all_data:
            # Full access: mount parent dirs so the app sees all apps' data
            cmd.extend(["-v", f"{os.path.join(data_dir, 'app_data')}:{CONTAINER_ROOT}/app_data"])
            cmd.extend(
                [
                    "-v",
                    f"{os.path.join(temp_data_dir, 'app_temp_data')}:{CONTAINER_ROOT}/app_temp_data",
                ]
            )
            os.makedirs(vm_data_dir, exist_ok=True)
            cmd.extend(["-v", f"{vm_data_dir}:{c_vm_data}"])
        else:
            if has_app_data:
                cmd.extend(["-v", f"{app_data_dir}:{c_app_data}"])
            if has_app_temp:
                cmd.extend(["-v", f"{app_temp_dir}:{c_app_temp}"])
            if has_vm_data:
                os.makedirs(vm_data_dir, exist_ok=True)
                cmd.extend(["-v", f"{vm_data_dir}:{c_vm_data}:ro"])

        # Structured port mappings: bind TCP+UDP on 0.0.0.0
        if port_mappings:
            for pm in port_mappings:
                cmd.extend(["-p", f"0.0.0.0:{pm.host_port}:{pm.container_port}/tcp"])
                cmd.extend(["-p", f"0.0.0.0:{pm.host_port}:{pm.container_port}/udp"])

        for cap in manifest.capabilities:
            cmd.extend(["--cap-add", cap])

        for device in manifest.devices:
            cmd.extend(["--device", device])

        for key, value in container_env.items():
            cmd.extend(["-e", f"{key}={value}"])

        if manifest.container_command:
            cmd.append(image_tag)
            cmd.extend(manifest.container_command.split())
        else:
            cmd.append(image_tag)

        logger.info("Running container: %s", " ".join(cmd))
        _append_log(app_name, temp_data_dir, f"=== Starting container: {container_name} ===\n")

        # Remove any stale container with the same name (e.g. from a previous run
        # or crash) so docker run doesn't fail with a name conflict.
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            capture_output=True,
            timeout=30,
        )

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            _append_log(app_name, temp_data_dir, f"ERROR: {result.stderr}\n")
            raise RuntimeError(f"Docker run failed:\n{result.stderr}")

        container_id = result.stdout.strip()
        _append_log(app_name, temp_data_dir, f"Container started: {container_id[:12]}\n\n")
        return container_id

    # ------------------------------------------------------------- lifecycle

    def stop_container(self, container_id: str) -> None:
        subprocess.run(["docker", "stop", container_id], capture_output=True, timeout=30)
        subprocess.run(["docker", "rm", "-f", container_id], capture_output=True, timeout=30)

    def remove_image(self, app_name: str) -> None:
        image_tag = f"openhost-{app_name}:latest"
        subprocess.run(["docker", "rmi", image_tag], capture_output=True, timeout=30)

    def get_container_status(self, container_id: str) -> str:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Status}}", container_id],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return "unknown"
        return result.stdout.strip()

    def get_container_logs(
        self,
        container_id: str,
        tail: int = 10000,
    ) -> str:
        try:
            result = subprocess.run(
                ["docker", "logs", "--tail", str(tail), container_id],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError):
            return ""
        combined = (result.stdout + result.stderr).strip()
        if not combined:
            return ""
        return _ANSI_RE.sub("", combined)

    # ----------------------------------------------------------------- cache

    def drop_build_cache(self) -> str:
        cmd = ["docker", "builder", "prune", "--all", "--force"]
        logger.info("Dropping Docker build cache: %s", " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        output = (result.stdout + result.stderr).strip()
        if result.returncode != 0:
            raise RuntimeError(output or "Docker builder prune failed")
        return output
