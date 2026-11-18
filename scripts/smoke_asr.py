r"""Real ASR on Chinese Windows SAPI speech; no real match footage or API calls.

Run from the project root:
  .venv\Scripts\python.exe scripts/smoke_asr.py --allow-cpu --ffmpeg PATH --ffprobe PATH

The official faster-whisper tiny checkpoint is downloaded only into this
project's cache/models/tiny. --offline refuses downloads and reuses that folder.
This validates actual audio decoding/model inference/timestamps, not accuracy on
live commentary. The synthetic reference and output are retained for inspection.
"""
from __future__ import annotations

import argparse
import base64
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import load_config
from core.transcriber import transcribe_audio, validate_transcript
from core.utils import atomic_json, file_signature

REFERENCE = ("这是一段人工合成的足球解说测试语音，不是真实比赛录像。"
             "曼城的这次换人非常关键，瓜迪奥拉需要调整中场站位。"
             "哈兰德正在等待队友传球。我们只讨论战术，没有确认任何进球。")


def _powershell_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def run_checked(command, timeout):
    """Keep encoded scripts out of errors and reports; retain useful diagnostics."""
    try:
        completed = subprocess.run(command, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{Path(command[0]).name} exceeded {timeout}s timeout") from None
    if completed.returncode:
        stderr = completed.stderr.decode("utf-8", errors="replace")[-2000:]
        raise RuntimeError(f"{Path(command[0]).name} exited {completed.returncode}: {stderr}")
    return completed


def synthesize(destination: Path, ffmpeg: str):
    if os.name != "nt":
        raise RuntimeError("本测试使用 Windows SAPI，需要 Windows 和中文 Huihui 语音")
    script = f"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
try {{
$testVoice = New-Object -ComObject SAPI.SpVoice
$selectedVoice = $testVoice.GetVoices() | Where-Object {{ $_.GetDescription() -match 'Huihui|Chinese' }} | Select-Object -First 1
if ($null -eq $selectedVoice) {{ throw 'Chinese SAPI voice missing; install Microsoft Huihui Desktop.' }}
$testVoice.Voice = $selectedVoice
$testVoice.Rate = 0
$testStream = New-Object -ComObject SAPI.SpFileStream
$testVoice.AllowAudioOutputFormatChangesOnNextSet = $true
try {{
    $testStream.Open({_powershell_literal(destination)}, 3, $false)
    $testVoice.AudioOutputStream = $testStream
    [void]$testVoice.Speak({_powershell_literal(REFERENCE)})
}} finally {{
    $testStream.Close()
    [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($testStream)
    [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($testVoice)
}}
}} catch {{
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}}
"""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    run_checked(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded], 90)
    # Let SAPI negotiate the voice's native PCM format, then normalize with FFmpeg.
    normalized = destination.with_name(destination.stem + ".normalized.wav")
    run_checked([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(destination),
                 "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(normalized)], 60)
    normalized.replace(destination)
    with wave.open(str(destination), "rb") as audio:
        if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) != (16000, 1, 2):
            raise RuntimeError("SAPI 没有输出期望的 16 kHz 单声道 PCM 音频")
        return audio.getnframes() / audio.getframerate()


def create_video(wav: Path, destination: Path, ffmpeg: str):
    # The title is explicitly synthetic. There are no football images in this video.
    font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "arial.ttf"
    font_argument = str(font).replace("\\", "/").replace(":", "\\:")
    caption = (f"drawtext=fontfile='{font_argument}':text='SYNTHETIC SPEECH - ASR TEST':"
               "fontcolor=white:fontsize=26:x=(w-tw)/2:y=(h-th)/2")
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
               "color=c=0x10212f:s=640x360:r=25", "-i", str(wav), "-vf", caption,
               "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-c:a", "aac",
               "-b:a", "96k", "-shortest", "-movflags", "+faststart", "-metadata",
               "title=Synthetic Chinese speech ASR test - not real football footage", str(destination)]
    run_checked(command, 120)
    run_checked([ffmpeg, "-v", "error", "-i", str(destination), "-f", "null", "-"], 60)


def normalized_text(text):
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", text).lower()


def character_error_rate(reference, observed):
    reference, observed = normalized_text(reference), normalized_text(observed)
    previous = list(range(len(observed) + 1))
    for row, left in enumerate(reference, 1):
        current = [row]
        for column, right in enumerate(observed, 1):
            current.append(min(current[-1] + 1, previous[column] + 1, previous[column - 1] + (left != right)))
        previous = current
    return previous[-1] / max(1, len(reference))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-cpu", action="store_true", help="明确允许使用 CPU 运行实际 tiny Whisper 推理")
    parser.add_argument("--offline", action="store_true", help="仅使用已下载的本项目模型")
    parser.add_argument("--ffmpeg", help="覆盖 .env / settings.yaml 中的 FFmpeg 路径")
    parser.add_argument("--ffprobe", help="覆盖 .env / settings.yaml 中的 FFprobe 路径")
    args = parser.parse_args(argv)
    if not args.allow_cpu:
        parser.error("本脚本使用真实 CPU 推理；请显式传入 --allow-cpu")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    directory = ROOT / "output" / "asr_smoke"
    directory.mkdir(parents=True, exist_ok=True)
    model_directory = ROOT / "cache" / "models" / "tiny"
    # Keep all Hugging Face files and optional transfer caches project-local.
    os.environ["HF_HOME"] = str(model_directory / ".hf_home")
    os.environ["HF_HUB_CACHE"] = str(model_directory / ".hub_cache")
    os.environ["HF_XET_CACHE"] = str(model_directory / ".xet_cache")
    os.environ["HF_HUB_DISABLE_XET"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    os.environ["HF_HUB_ETAG_TIMEOUT"] = "15"
    os.environ["HF_HUB_DOWNLOAD_TIMEOUT"] = "30"
    report = {"status": "started", "synthetic": True, "real_footage": False,
              "reference": REFERENCE, "model": "Systran/faster-whisper-tiny", "device": "cpu",
              "model_directory": str(model_directory), "checks": {}}
    started = time.perf_counter()
    try:
        cfg = load_config()
        report["versions"] = {package: importlib.metadata.version(package)
                              for package in ("faster-whisper", "ctranslate2", "av")}
        args.ffmpeg = args.ffmpeg or shutil.which(cfg["ffmpeg"])
        args.ffprobe = args.ffprobe or shutil.which(cfg["ffprobe"])
        if not args.ffmpeg:
            raise RuntimeError("找不到 FFmpeg；请配置 FFMPEG_PATH 或传入 --ffmpeg")
        reference_path = directory / "reference.txt"
        wav = directory / "synthetic_zh.wav"
        if wav.exists() and reference_path.exists() and reference_path.read_text(encoding="utf-8") == REFERENCE:
            with wave.open(str(wav), "rb") as audio:
                duration = audio.getnframes() / audio.getframerate()
        else:
            logging.info("SAPI 中文语音写入 WAV（不播放）")
            duration = synthesize(wav, args.ffmpeg)
            reference_path.write_text(REFERENCE, encoding="utf-8")
        report.update(wav=str(wav), duration=duration)
        report["checks"]["synthetic_pcm_audio"] = True
        logging.info("合成语音 %.2f 秒", duration)
        if args.ffmpeg:
            video = directory / "synthetic_zh.mp4"
            create_video(wav, video, args.ffmpeg)
            report["video"] = str(video)
            report["checks"]["video_full_decode"] = True
            if args.ffprobe:
                from core.video_input import probe_video
                probe = probe_video(video, {"ffmpeg": args.ffmpeg, "ffprobe": args.ffprobe})
                report["video_probe"] = {k: probe[k] for k in ("duration", "width", "height", "fps")}
        else:
            report["checks"]["video_full_decode"] = False
            report["video_note"] = "未找到 FFmpeg；已保留 PCM WAV，可传 --ffmpeg 生成标注为合成语音的测试视频。"
        model_directory.mkdir(parents=True, exist_ok=True)
        if not all((model_directory / name).is_file() for name in ("model.bin", "config.json", "tokenizer.json")):
            logging.info("下载官方 tiny 模型到 %s（约 75 MB）", model_directory)
            from faster_whisper.utils import download_model
            download_model("tiny", output_dir=str(model_directory), cache_dir=str(model_directory / ".hub_cache"),
                           local_files_only=args.offline, use_auth_token=False)
        report["checks"]["model_files_local"] = True
        cfg["asr"].update(model=str(model_directory), device="cpu", compute_type="int8", language="zh",
                          chunk_seconds=10, overlap_seconds=2, vad_filter=True, beam_size=5)
        meta = {"duration": duration, "source": str(wav), "source_signature": file_signature(wav),
                "selected_audio": 0, "timeline_origin": 0}
        logging.info("运行真实 faster-whisper CPU/int8 分块转录")
        transcript = transcribe_audio(wav, meta, cfg, directory, allow_cpu=args.allow_cpu)
        validate_transcript(transcript, duration)
        observed = "".join(segment["text"] for segment in transcript["segments"])
        words = [word for segment in transcript["segments"] for word in segment.get("words", [])]
        if len(re.findall(r"[\u4e00-\u9fff]", observed)) < 10 or not words:
            raise RuntimeError("真实推理没有生成足够的中文及词级时间戳，请检查报告和模型")
        report.update(status="passed", transcript=str(directory / "transcript.json"), recognized_text=observed,
                      segment_count=len(transcript["segments"]), word_count=len(words),
                      character_error_rate=round(character_error_rate(REFERENCE, observed), 4),
                      caveat="这是合成中文语音的功能测试；不代表真实足球直播、多人争论、背景噪声或 medium 模型质量。")
        report["checks"].update(actual_whisper_inference=True, chinese_text=True, word_timestamps=True,
                                 timestamps_within_audio=True)
        report["elapsed_seconds"] = round(time.perf_counter() - started, 2)
        atomic_json(directory / "report.json", report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        report.update(status="blocked", error=f"{type(error).__name__}: {error}",
                      elapsed_seconds=round(time.perf_counter() - started, 2))
        report["recovery"] = ("确认 Windows 已安装中文 Huihui Desktop 语音且运行账户可访问；"
                              "配置 FFMPEG_PATH/FFPROBE_PATH；首次模型下载需要网络，"
                              "下载完成后可用 --offline 重试。进度和模型均保存在本项目内。")
        atomic_json(directory / "report.json", report)
        logging.error("真实 ASR 烟雾测试未完成：%s；报告已保存", error)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
