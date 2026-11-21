from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from connectors.stage.client import connect_stage
from connectors.stage.daemon import StageAgent
from connectors.stage.obs import Program, StageError


def _connect(args: argparse.Namespace):
    return connect_stage(
        Program(args.scene, args.audio_source, args.caption_source, args.avatar_source),
        obs_port=args.obs_port,
        obs_password=os.environ.get("LING_STAGE_OBS_PASSWORD", ""),
        vts_port=args.vts_port,
        vts_model=args.vts_model,
        vts_token_file=args.vts_token_file,
        mouth_parameter=args.mouth_parameter,
    )


async def _preview(args: argparse.Namespace) -> None:
    async with _connect(args) as stage:
        with TemporaryDirectory(prefix="ling-stage-") as path:
            clip = Path(path) / args.audio.name
            shutil.copyfile(args.audio, clip)
            played = await stage.preview(clip, args.caption)
            print(f"{played.state}: {played.played_ms} ms")


async def _daemon(args: argparse.Namespace) -> None:
    import httpx
    from filelock import FileLock, Timeout

    token = os.environ.get("LING_STAGE_TOKEN")
    if not token:
        raise StageError("请设置 LING_STAGE_TOKEN")
    if not args.instance or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for c in args.instance):
        raise StageError("请指定接入器 ID")
    args.state_dir.mkdir(parents=True, exist_ok=True)
    lock = FileLock(args.state_dir / f"{args.instance}.lock")
    try:
        with lock.acquire(timeout=0):
            async with httpx.AsyncClient(
                base_url=args.server, headers={"Authorization": f"Bearer {token}"}, timeout=4,
            ) as http:
                agent = StageAgent(
                    http, instance=args.instance, state_dir=args.state_dir, stream_server=args.stream_server,
                )
                while True:
                    try:
                        async with _connect(args) as stage:
                            await agent.run(stage)
                    except Exception as exc:
                        logging.warning("舞台连接中断，类型=%s，将重试", type(exc).__name__)
                        agent.ready = False
                        await asyncio.sleep(2)
    except Timeout as exc:
        raise StageError("这个接入器已在本机运行") from exc


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
    parser.add_argument("--server", help="Ling API 地址，指定后进入常驻模式")
    parser.add_argument("--instance", help="注册的自定义接入器 ID")
    parser.add_argument("--stream-server", help="允许本舞台启动的 OBS 推流服务地址")
    parser.add_argument("--state-dir", type=Path, default=Path.home() / ".ling-stage")
    parser.add_argument("audio", type=Path, nargs="?")
    parser.add_argument("caption", nargs="?")
    args = parser.parse_args()
    if args.server is None and (args.audio is None or args.caption is None):
        parser.error("请提供预览音频和字幕，或指定 --server 连接 Ling")
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(_daemon(args) if args.server else _preview(args))
    except KeyboardInterrupt:
        return
    except (StageError, OSError, TimeoutError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
