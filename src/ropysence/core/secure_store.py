"""
Encrypted local storage for anything sensitive: the Roblox .ROBLOSECURITY
cookie and the Discord OAuth token pair.

Key handling, in priority order:
  1. OS keyring (macOS Keychain / Windows Credential Locker / Linux Secret
     Service via libsecret) -- the encryption key itself never touches disk.
  2. Local key file at ~/.config/ropysence/secret.key, chmod 600.

Caveat, stated plainly: option 2 protects against casual exposure (accidental
sharing, committing the config dir to git, a screenshot) but NOT against
another process with full read access to this user account. If your platform
supports a keyring backend, install/enable it for real protection.

The encrypted blob itself lives in ~/.config/ropysence/store.enc and
is never plaintext on disk either way.
"""

import json
import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from src.ropysence.core.logging_setup import get_logger

log = get_logger("secure_store")

try:
    import keyring
    _HAS_KEYRING = True
except ImportError:
    _HAS_KEYRING = False

APP_DIR = Path(os.environ.get("RDP_CONFIG_DIR", Path.home() / ".config" / "ropysence"))
STORE_FILE = APP_DIR / "store.enc"
KEYFILE = APP_DIR / "secret.key"
KEYRING_SERVICE = "ropysence"
KEYRING_USER = "encryption-key"


class SecureStore:
    def __init__(self):
        APP_DIR.mkdir(parents=True, exist_ok=True)
        self._fernet = Fernet(self._get_or_create_key())

    def _get_or_create_key(self) -> bytes:
        if _HAS_KEYRING:
            try:
                existing = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
                if existing:
                    log.info("encryption key loaded from OS keyring")
                    return existing.encode()
                new_key = Fernet.generate_key()
                keyring.set_password(KEYRING_SERVICE, KEYRING_USER, new_key.decode())
                log.info("new encryption key generated and stored in OS keyring")
                return new_key
            except Exception as e:
                log.warning(
                    "OS keyring unavailable (%s) -- falling back to a local key file. "
                    "This protects against casual exposure only, not a fully compromised machine.",
                    e,
                )
        else:
            log.warning(
                "`keyring` package not installed -- falling back to a local key file "
                "(pip install keyring for OS-backed key storage)"
            )

        if KEYFILE.exists():
            log.debug("loading existing local key file at %s", KEYFILE)
            return KEYFILE.read_bytes()

        log.info("generating new local key file at %s", KEYFILE)
        key = Fernet.generate_key()
        KEYFILE.write_bytes(key)
        try:
            os.chmod(KEYFILE, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        except OSError as e:
            log.warning("could not chmod key file to 600: %s", e)
        return key

    def load(self) -> dict:
        if not STORE_FILE.exists():
            log.debug("no existing store file at %s, returning empty store", STORE_FILE)
            return {}
        try:
            blob = STORE_FILE.read_bytes()
            plaintext = self._fernet.decrypt(blob)
            data = json.loads(plaintext)
            log.debug("secure store loaded (%d key(s))", len(data))
            return data
        except InvalidToken:
            log.error("failed to decrypt secure store -- wrong/rotated key or corrupted file, treating as empty")
            return {}
        except Exception as e:
            log.error("unexpected error loading secure store: %s", e)
            return {}

    def save(self, data: dict) -> None:
        try:
            plaintext = json.dumps(data).encode()
            blob = self._fernet.encrypt(plaintext)
            STORE_FILE.write_bytes(blob)
            try:
                os.chmod(STORE_FILE, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
            log.debug("secure store saved (%d key(s))", len(data))
        except Exception as e:
            log.error("failed to save secure store: %s", e)
            raise

    def get(self, key, default=None):
        return self.load().get(key, default)

    def set(self, key, value) -> None:
        data = self.load()
        data[key] = value
        self.save(data)
        log.debug("secure store key '%s' updated", key)

    def delete(self, key) -> None:
        data = self.load()
        if key in data:
            del data[key]
            self.save(data)
            log.info("secure store key '%s' removed", key)
