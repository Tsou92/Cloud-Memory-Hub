"""Accepted HNSW allocation slack never exempts used bytes or unknown layouts."""

import struct

import pytest
from test_governance_execution import hnsw_unused_start, scan

MARKER = "synthetic-residue-marker"
CAPACITY, COUNT, STRIDE = 64, 16, 156
HEADER = (1, 0, CAPACITY, COUNT, STRIDE, 148, 132, 0, 0, 16, 32, 16, 0.36, 100)


def segment(root):
    directory = root / "segment"
    directory.mkdir()
    (directory / "header.bin").write_bytes(struct.pack("<IQQQQQQiIQQQdQ", *HEADER))
    (directory / "data_level0.bin").write_bytes(bytes(CAPACITY * STRIDE))
    (directory / "length.bin").write_bytes(bytes(CAPACITY * 4))
    return directory


def place(path, position):
    data = bytearray(path.read_bytes())
    data[position : position + len(MARKER)] = MARKER.encode()
    path.write_bytes(data)


@pytest.mark.parametrize("filename,width", [("data_level0.bin", STRIDE), ("length.bin", 4)])
@pytest.mark.parametrize("position", ["used", "crossing", "unused", "both"])
def test_scan_distinguishes_used_and_unused_bytes(tmp_path, filename, width, position):
    path = segment(tmp_path) / filename
    boundary = COUNT * width
    place(path, boundary if position in {"unused", "both"} else boundary - 1)
    if position in {"used", "both"}:
        place(path, 0)
    assert hnsw_unused_start(path) == boundary
    assert scan(tmp_path, MARKER) == (
        [] if position == "unused" else [str(path.relative_to(tmp_path))]
    )


@pytest.mark.parametrize("filename", ["data_level0.bin", "length.bin"])
@pytest.mark.parametrize(
    "damage",
    [
        "missing_header",
        "short_header",
        "extra_header",
        "version",
        "count",
        "stride",
        "capacity",
        "data_offset",
        "label_offset",
        "level",
        "entrypoint",
        "m",
        "multiplier",
        "construction",
        "short_level0",
        "short_length",
    ],
)
def test_scan_keeps_unknown_or_inconsistent_layouts(tmp_path, filename, damage):
    directory = segment(tmp_path)
    path = directory / filename
    place(path, COUNT * (4 if filename == "length.bin" else STRIDE))
    header = directory / "header.bin"
    if damage == "missing_header":
        header.unlink()
    elif damage == "short_header":
        header.write_bytes(header.read_bytes()[:-1])
    elif damage == "extra_header":
        header.write_bytes(header.read_bytes() + b"\0")
    elif damage.startswith("short_"):
        sibling = directory / ("length.bin" if damage == "short_length" else "data_level0.bin")
        sibling.write_bytes(sibling.read_bytes()[:-1])
    else:
        index, value = {
            "version": (0, 2),
            "capacity": (2, CAPACITY + 1),
            "count": (3, CAPACITY + 1),
            "stride": (4, STRIDE - 1),
            "label_offset": (5, 132),
            "data_offset": (6, 128),
            "level": (7, -1),
            "entrypoint": (8, COUNT),
            "m": (11, 12),
            "multiplier": (12, float("nan")),
            "construction": (13, 1),
        }[damage]
        values = list(HEADER)
        values[index] = value
        header.write_bytes(struct.pack("<IQQQQQQiIQQQdQ", *values))
    assert hnsw_unused_start(path) is None
    assert str(path.relative_to(tmp_path)) in scan(tmp_path, MARKER)


@pytest.mark.parametrize("filename", ["length.bin", "data_level0.bin", "link_lists.bin"])
def test_same_named_unrelated_files_are_not_ignored(tmp_path, filename):
    path = tmp_path / filename
    path.write_text(MARKER)
    assert scan(tmp_path, MARKER) == [filename]


def test_empty_persisted_index_has_only_unused_allocated_bytes(tmp_path):
    directory = segment(tmp_path)
    values = list(HEADER)
    values[3], values[7], values[8] = 0, -1, 2**32 - 1
    (directory / "header.bin").write_bytes(struct.pack("<IQQQQQQiIQQQdQ", *values))
    for name in ("data_level0.bin", "length.bin"):
        path = directory / name
        place(path, 30)
        assert hnsw_unused_start(path) == 0
    assert scan(tmp_path, MARKER) == []
