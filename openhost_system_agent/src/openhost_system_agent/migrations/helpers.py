from __future__ import annotations

import os
import pwd
import re
import subprocess
from pathlib import Path


def run(*cmd: str) -> None:
    subprocess.run(cmd, check=True)


def write_file(path: str, content: str, mode: int = 0o644) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    os.chmod(path, mode)


def ensure_line(path: str, line: str) -> None:
    p = Path(path)
    if p.exists():
        text = p.read_text()
        if line in text:
            return
        with open(path, "a") as f:
            f.write(line + "\n")


def set_sshd_option(key: str, value: str) -> None:
    config = Path("/etc/ssh/sshd_config")
    text = config.read_text()
    pattern = re.compile(rf"^#?\s*{re.escape(key)}\b.*$", re.MULTILINE)
    replacement = f"{key} {value}"
    if pattern.search(text):
        text = pattern.sub(replacement, text)
    else:
        text = text.rstrip("\n") + f"\n{replacement}\n"
    config.write_text(text)


def get_host_uid() -> int:
    return pwd.getpwnam("host").pw_uid
