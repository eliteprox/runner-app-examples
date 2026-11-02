"""Install the packaged node exports into the managed workspace."""

from importlib.resources import files
from pathlib import Path

workspace = Path("/workspace/ComfyUI")
node = workspace / "custom_nodes" / "stream_pack_service"
node.mkdir(parents=True, exist_ok=True)
node.joinpath("__init__.py").write_text(
    "from stream_pack.nodes import NODE_CLASS_MAPPINGS\n"
    "from stream_pack.legacy.load_tensor import LoadTensor\n"
    "from stream_pack.legacy.save_tensor import SaveTensor\n"
    "from stream_pack.legacy.load_audio_tensor import LoadAudioTensor\n"
    "from stream_pack.legacy.save_audio_tensor import SaveAudioTensor\n"
    "from stream_pack.legacy.save_text_tensor import SaveTextTensor\n"
    "from stream_pack.legacy.invert_image import InvertImage\n"
    "from stream_pack.legacy.pitch_shift import PitchShifter\n"
    "from stream_pack.legacy.flux_klein_stream import FluxKleinStream\n"
    "NODE_CLASS_MAPPINGS = dict(NODE_CLASS_MAPPINGS)\n"
    "from stream_pack.video_tools.nodes import NODE_CLASS_MAPPINGS as VIDEO_TOOLS\n"
    "NODE_CLASS_MAPPINGS.update(VIDEO_TOOLS)\n"
    "NODE_CLASS_MAPPINGS.update({c.__name__: c for c in "
    "(LoadTensor, SaveTensor, LoadAudioTensor, SaveAudioTensor, SaveTextTensor, "
    "InvertImage, PitchShifter, FluxKleinStream)})\n",
    encoding="utf-8",
)
for name in ("input", "output", "temp", "models"):
    (workspace / name).mkdir(exist_ok=True)
for name in ("inputs", "outputs", "artifacts"):
    (Path("/state") / name).mkdir(parents=True, exist_ok=True)
assert files("comfystream.assets").joinpath("configs", "fal", "index.json").is_file()
