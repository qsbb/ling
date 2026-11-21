from __future__ import annotations

import argparse
import asyncio
import os
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from connectors.stage.client import connect_stage
from connectors.stage.obs import Program, StageError


async def _preview(args: argparse.Namespace) -> None:
    async with connect_stage(
        Program(args.scene, args.audio_source, args.caption_source, args.avatar_source),
        obs_port=args.obs_port,
        obs_password=os.environ.get("LING_STAGE_OBS_PASSWORD", ""),
        vts_port=args.vts_port,
        vts_model=args.vts_model,
        vts_token_file=args.vts_token_file,
        mouth_parameter=args.mouth_parameter,
    ) as stage:
        with TemporaryDirectory(prefix="ling-stage-") as path:
            clip = Path(path) / args.audio.name
            shutil.copyfile(args.audio, clip)
            played = await stage.preview(clip, args.caption)
            print(f"{played.state}: {played.played_ms} ms")


def main() -> None:
    parser = argparse.ArgumentParser(description="Ling 舞台端")
    parser.add_argument("--scene", required=True)
    parser.add_argument("--audio-source", required=True)
    parser.add_argument("--caption-source", required=True)
    parser.add_argument("--avatar-source")
    parser.add_argument("--obs-port", type=int, default=4455)
    parser.add_argument("--vts-port", type=int, default=8001)
    parser.add_argument("--vts-model")
    parser.add_argument("--vts-token-file", type=Path, default=Path.home() / ".ling-stage" / "vts-token")
    parser.add_argument("--mouth-parameter", default="MouthOpen")
    parser.add_argument("audio", type=Path)
    parser.add_argument("caption")
    args = parser.parse_args()
    try:
        asyncio.run(_preview(args))
    except (StageError, OSError, TimeoutError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
