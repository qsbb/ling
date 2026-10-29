"""Speech 域：VAD/ASR/TTS 分段与播放回执（唯一写入方）。"""

from ling.domains.speech.service import PlaybackReceipt, PlaybackTracker, SegmentView, SpeechService

__all__ = ["PlaybackReceipt", "PlaybackTracker", "SegmentView", "SpeechService"]
