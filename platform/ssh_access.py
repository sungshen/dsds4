"""SSH authentication using local credentials and the user's known host keys."""
import re
from pathlib import Path

import paramiko


def connect(settings):
    host = settings.get('HETZNER_SSH_HOST', '')
    user = settings.get('HETZNER_SSH_USER', '')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.:-]*', host) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*', user):
        raise ValueError('Invalid SSH host or username')
    port = int(settings.get('HETZNER_SSH_PORT') or 22)
    if not 1 <= port <= 65535:
        raise ValueError('Invalid SSH port')
    password = settings.get('HETZNER_SSH_PASSWORD') or None
    key_path = settings.get('HETZNER_SSH_KEY_PATH')
    key = str(Path(key_path).expanduser()) if key_path else None
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect(host, port=port, username=user, password=password, key_filename=key,
                       allow_agent=not bool(password), look_for_keys=not bool(password),
                       timeout=8, auth_timeout=8, banner_timeout=8)
        return client
    except Exception:
        client.close()
        raise
