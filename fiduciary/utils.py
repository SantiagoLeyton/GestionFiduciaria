import hashlib


def calculate_sha256(file_or_path, *, chunk_size=1024 * 1024):
    digest = hashlib.sha256()
    close_after = False

    if hasattr(file_or_path, "read"):
        stream = file_or_path
        original_position = stream.tell() if hasattr(stream, "tell") else None
    else:
        stream = open(file_or_path, "rb")
        close_after = True
        original_position = None

    try:
        if hasattr(stream, "seek"):
            stream.seek(0)
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        if original_position is not None and hasattr(stream, "seek"):
            stream.seek(original_position)
        if close_after:
            stream.close()

    return digest.hexdigest()
