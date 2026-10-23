"""Hash text with common digest algorithms."""

import base64
import hashlib
import zlib

from taskdeck.core import Field, JobPlugin, Pipeline, Stage, step
from taskdeck.shared import steps

ALGORITHMS = ("sha256", "sha1", "md5", "sha512", "blake2b", "crc32")


@step(Stage.EXECUTE)
def digest(ctx):
    data = ctx["text"].encode(ctx["encoding"])
    algo = ctx["algorithm"]
    if algo == "crc32":
        raw = zlib.crc32(data).to_bytes(4, "big")
    else:
        raw = hashlib.new(algo, data).digest()
    ctx["digest"] = {
        "Algorithm": algo,
        "Hex": raw.hex(),
        "Base64": base64.b64encode(raw).decode("ascii"),
        "Input bytes": len(data),
    }


class HashText(JobPlugin):
    id = "hash_text"
    title = "Hash / Checksum"
    description = "Compute SHA-256, MD5, CRC32 and other digests of text."
    category = "Developer"
    icon = "TAG"
    tags = ("sha", "md5", "checksum", "digest", "crc")
    fields = (
        Field.multiline("text", "Text", required=True),
        Field.choice("algorithm", "Algorithm", ALGORITHMS),
        Field.choice("encoding", "Encoding", ("utf-8", "latin-1", "ascii")),
    )

    def build_pipeline(self):
        # Hashing must see the exact bytes, so no normalize_text here.
        return Pipeline([steps.require("text"), digest, steps.present_rows("digest")])


PLUGIN = HashText()
