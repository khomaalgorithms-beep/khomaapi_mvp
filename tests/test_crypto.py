"""Token encryption must be resilient: a token encrypted with a different/rotated
key must NOT raise (Fernet's InvalidToken has a blank message, which used to surface
as an empty 'REJECTED'). It should read as 'no usable token' so the caller asks the
user to reconnect."""

import os
import tempfile

os.environ["KHOMA_DB_PATH"] = tempfile.mktemp(suffix=".db")
os.environ.pop("DATABASE_URL", None)
os.environ.pop("DATABASE_PUBLIC_URL", None)
os.environ["KHOMA_DISABLE_WATCHDOG"] = "1"

from cryptography.fernet import Fernet  # noqa: E402
from app import main as appmod  # noqa: E402


def test_enc_dec_roundtrip():
    assert appmod.dec(appmod.enc("hello-token")) == "hello-token"
    assert appmod.enc("") == ""
    assert appmod.dec("") == "" and appmod.dec(None) == ""


def test_dec_returns_empty_on_key_mismatch():
    # Simulate a token written by a container with a DIFFERENT (rotated) key.
    foreign = Fernet(Fernet.generate_key()).encrypt(b"some-broker-token").decode()
    assert appmod.dec(foreign) == ""        # no raise, no blank-message exception
    assert appmod.dec("not-even-a-token") == ""


def test_fernet_prefers_env_key(monkeypatch):
    # The stability fix: when KHOMA_ENC_KEY is set, it is the source of truth so the
    # key survives ephemeral-filesystem restarts.
    k = Fernet.generate_key().decode()
    monkeypatch.setenv("KHOMA_ENC_KEY", k)
    fernet, source = appmod._load_fernet()
    assert source == "env"
    assert fernet.decrypt(Fernet(k.encode()).encrypt(b"x")) == b"x"
    # An invalid env key falls back to the file rather than crashing the app.
    monkeypatch.setenv("KHOMA_ENC_KEY", "not-a-valid-fernet-key")
    _, source2 = appmod._load_fernet()
    assert source2 == "file"
