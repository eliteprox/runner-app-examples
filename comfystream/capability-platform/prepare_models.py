"""Operator-only checkpoint preparation; inference remains offline."""

import argparse
import fnmatch
import json
import shutil
from pathlib import Path

from huggingface_hub import HfApi, snapshot_download, try_to_load_from_cache
from stream_pack.catalog import load_catalog

PATTERNS = (
    "*.json",
    "*.txt",
    "*.model",
    "*.tiktoken",
    "*.safetensors",
    "*.py",
    "*.yaml",
    "*.yml",
    "*.ckpt",
    "LICENSE*",
    "README*",
)


def prepare(args):
    catalog = load_catalog()
    cache = Path(args.cache)
    cache.mkdir(parents=True, exist_ok=True)
    for name in args.capability:
        descriptor = catalog[name].document
        execution = [
            part
            for part in descriptor.get("components", [])
            if part.get("role") == "execution"
        ]
        sources = (
            descriptor["components"]
            if name == "ace-music"
            else execution or [descriptor["model"]]
        )
        if name == "triposr":
            sources = [descriptor["model"], *descriptor.get("components", [])]
        for component in sources:
            repo, revision = component["source"], component["revision"]
            info = HfApi().model_info(repo, revision=revision, files_metadata=True)
            assert info.sha == revision, "Checkpoint revision changed"
            patterns = [component["file"]] if "file" in component else PATTERNS
            selected = [
                entry
                for entry in info.siblings
                if any(
                    fnmatch.fnmatch(entry.rfilename, pattern) for pattern in patterns
                )
                and not entry.rfilename.startswith(("onnx/", "openvino/", "tflite/"))
            ]
            missing = sum(
                entry.size or 0
                for entry in selected
                if not isinstance(
                    try_to_load_from_cache(
                        repo, entry.rfilename, cache_dir=cache, revision=revision
                    ),
                    str,
                )
            )
            print(
                json.dumps(
                    {
                        "capability": name,
                        "source": repo,
                        "revision": revision,
                        "missing_bytes": missing,
                    }
                ),
                flush=True,
            )
            if args.dry_run:
                continue
            if missing + args.reserve_gib * 1024**3 > shutil.disk_usage(cache).free:
                raise RuntimeError(
                    "Insufficient disk space after the configured reserve"
                )
            location = snapshot_download(
                repo,
                revision=revision,
                cache_dir=cache,
                allow_patterns=[entry.rfilename for entry in selected],
                max_workers=4,
            )
            if name == "ace-music":
                target = (
                    Path(args.workspace)
                    / "models/checkpoints"
                    / Path(component["file"]).name
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    target.symlink_to(Path(location) / component["file"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("capability", nargs="+", choices=list(load_catalog()))
    parser.add_argument("--cache", default="/model-cache")
    parser.add_argument("--workspace", default="/workspace/ComfyUI")
    parser.add_argument("--reserve-gib", type=int, default=32)
    parser.add_argument("--dry-run", action="store_true")
    prepare(parser.parse_args())
