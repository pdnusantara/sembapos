"""
Rate limit sederhana per proses (in-memory). Untuk multi-worker gunakan Redis nanti.
"""
import hashlib
import time
from collections import defaultdict
from typing import DefaultDict, List

_buckets: DefaultDict[str, List[float]] = defaultdict(list)


def _hash_ip(remote_addr: str, secret: str = '') -> str:
    raw = f"{remote_addr}|{secret}"
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]


def allow_request(bucket_key: str, limit: int, window_seconds: int) -> bool:
    now = time.time()
    bucket = _buckets[bucket_key]
    bucket[:] = [t for t in bucket if now - t < window_seconds]
    if len(bucket) >= limit:
        return False
    bucket.append(now)
    return True


def client_key(prefix: str, remote_addr: str, extra: str = '') -> str:
    h = _hash_ip(remote_addr or 'unknown')
    return f'{prefix}:{h}:{extra}'
