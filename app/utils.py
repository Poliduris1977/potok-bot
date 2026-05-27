import hashlib

def hash_uid(user_id: str) -> str:
    """
    Хешировать user_id для безопасного логирования.
    Первые 12 символов SHA-256 — достаточно для отладки, не восстановимо.
    """
    return hashlib.sha256(str(user_id).encode()).hexdigest()[:12]