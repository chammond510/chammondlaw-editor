#!/usr/bin/env python3
import os
import platform
import subprocess
import tempfile
from pathlib import Path


CERT_DIR = Path.home() / ".config" / "bia-edge-word-addin" / "tls"
CERT_FILE = CERT_DIR / "localhost-cert.pem"
KEY_FILE = CERT_DIR / "localhost-key.pem"
KEYCHAIN = str(Path.home() / "Library" / "Keychains" / "login.keychain-db")
CERT_COMMON_NAME = "BIA Edge Word Add-in Localhost"


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def _write_openssl_config(path: Path) -> None:
    path.write_text(
        """
[req]
default_bits = 2048
prompt = no
default_md = sha256
x509_extensions = v3_req
distinguished_name = dn

[dn]
CN = BIA Edge Word Add-in Localhost
O = Hammond Law

[v3_req]
subjectAltName = @alt_names
extendedKeyUsage = serverAuth
keyUsage = digitalSignature, keyEncipherment

[alt_names]
DNS.1 = localhost
IP.1 = 127.0.0.1
IP.2 = ::1
""".strip()
        + "\n",
        encoding="utf-8",
    )


def ensure_certificate() -> None:
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    if CERT_FILE.exists() and KEY_FILE.exists():
        return

    with tempfile.TemporaryDirectory(prefix="word-addin-local-tls-") as tmpdir:
        config_path = Path(tmpdir) / "openssl-localhost.cnf"
        _write_openssl_config(config_path)
        _run(
            [
                "openssl",
                "req",
                "-x509",
                "-nodes",
                "-newkey",
                "rsa:2048",
                "-days",
                "3650",
                "-keyout",
                str(KEY_FILE),
                "-out",
                str(CERT_FILE),
                "-config",
                str(config_path),
                "-extensions",
                "v3_req",
            ]
        )


def trust_certificate() -> None:
    if platform.system() != "Darwin":
        print("Generated the localhost certificate. Trust it manually on this platform.")
        return

    existing = subprocess.run(
        [
            "security",
            "find-certificate",
            "-a",
            "-c",
            CERT_COMMON_NAME,
            KEYCHAIN,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if existing.returncode == 0 and CERT_COMMON_NAME in (existing.stdout or ""):
        return

    _run(
        [
            "security",
            "add-trusted-cert",
            "-d",
            "-r",
            "trustRoot",
            "-k",
            KEYCHAIN,
            str(CERT_FILE),
        ]
    )


def main() -> int:
    ensure_certificate()
    trust_certificate()
    print(f"Certificate: {CERT_FILE}")
    print(f"Key: {KEY_FILE}")
    print("")
    print("Start the bridge with:")
    print(
        "WORD_ADDIN_BRIDGE_CERT_FILE={cert} WORD_ADDIN_BRIDGE_KEY_FILE={key} "
        "python scripts/word_addin_codex_bridge.py".format(
            cert=CERT_FILE,
            key=KEY_FILE,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
