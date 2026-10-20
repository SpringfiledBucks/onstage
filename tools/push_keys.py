"""Generate a P-256 Web Push Secret outside the checkout; never print private material."""
import argparse
import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def generate(path):
    target = path.resolve()
    if target.is_relative_to(Path(__file__).resolve().parents[1]):
        raise ValueError('Save private keys in a restricted directory outside the checkout.')
    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    # Parent must be an operator-protected directory/Secret volume. O_EXCL
    # preserves an existing release key; POSIX 0600 does not replace Windows ACLs.
    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(private)
        stream.flush()
        os.fsync(stream.fileno())
    public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(public).rstrip(b'=').decode()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', type=Path)
    args = parser.parse_args()
    try:
        print('VAPID public key: ' + generate(args.path))
    except (ValueError, OSError):
        parser.exit(1, 'Key generation refused; check protected parent path and existing file. No existing key overwritten.\n')
