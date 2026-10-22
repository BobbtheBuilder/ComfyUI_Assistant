"""Lossless input splitting for embedding providers without a local tokenizer."""


def embedding_parts(text: str, max_tokens: int) -> list[str]:
    # UTF-8 bytes are a conservative estimate for byte-based tokenizers, unlike
    # four characters/token (which underestimates code and non-English text).
    # Reserve space for provider-added special tokens. This sizes requests; it
    # never limits the total text or number of parts.
    budget = max(1, int(max_tokens * 0.9))
    parts = []
    start = size = 0
    for index, char in enumerate(text):
        width = len(char.encode("utf-8"))
        if width > budget:
            raise ValueError("Embedding context is too small for a Unicode character.")
        if size + width > budget:
            parts.append(text[start:index])
            start, size = index, 0
        size += width
    if start < len(text):
        parts.append(text[start:])
    return parts
