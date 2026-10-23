"""Exercise the shipped upstream encoder in a read-only, network-disabled container."""

import math

from one_memory.embedding_assets import BUILTIN_EMBEDDING_DIMENSIONS, verify_assets
from one_memory.local_embedding import LocalEmbedder


def probe() -> None:
    verify_assets()
    encoder = LocalEmbedder()
    samples = ["记忆服务支持自动备份", "Please answer in Chinese", "记忆服务支持自动备份"]
    vectors = encoder.encode(samples)
    assert len(vectors) == len(samples)
    for vector in vectors:
        assert len(vector) == BUILTIN_EMBEDDING_DIMENSIONS
        assert all(math.isfinite(value) for value in vector)
        assert math.isclose(sum(value * value for value in vector), 1, abs_tol=1e-5)
    assert vectors[0] == vectors[2]
    assert any(abs(a - b) > 1e-3 for a, b in zip(vectors[0], vectors[1], strict=True))
    assert encoder.tokenize_with_offsets(samples[0]).token_ids
    print("Shipped model digests, offline encoding and tokenization passed")


if __name__ == "__main__":
    probe()
