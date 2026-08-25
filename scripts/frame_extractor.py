import json
from pathlib import Path
import shutil
import subprocess
import tempfile

from highlight_policy import select_highlight_frames


class FrameExtractionError(RuntimeError):
    pass


def _ffmpeg_binary():
    binary = shutil.which("ffmpeg")
    if binary:
        return binary
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError) as exc:
        raise FrameExtractionError("FFmpeg is unavailable") from exc


def _extract_frame(video_path, timestamp, destination):
    result = subprocess.run(
        [_ffmpeg_binary(), "-y", "-i", str(video_path), "-ss", str(timestamp), "-frames:v", "1", str(destination)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0 or not Path(destination).is_file() or Path(destination).stat().st_size == 0:
        raise FrameExtractionError("FFmpeg could not extract the frame at %s seconds" % timestamp)


def _write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".%s." % path.name, suffix=".tmp", dir=str(path.parent))
    try:
        with open(descriptor, "w", encoding="utf-8", closefd=True) as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        Path(temporary).replace(path)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def _frame_name(index, timestamp):
    token = ("%.6f" % timestamp).rstrip("0").rstrip(".").replace("-", "m").replace(".", "p") or "0"
    return "%02d_%ss.jpg" % (index, token)


def extract_highlight_frames(records, policy, video_path, output_dir, extractor=None):
    selection = select_highlight_frames(records, policy)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    extractor = extractor or _extract_frame
    frames = []
    for index, selected in enumerate(selection["frames"], start=1):
        timestamp = selected["durationSec"]
        path = output_dir / _frame_name(index, timestamp)
        extractor(video_path, timestamp, path)
        if not path.is_file() or path.stat().st_size == 0:
            raise FrameExtractionError("frame extractor did not create %s" % path.name)
        frames.append({
            "framePath": str(path),
            "durationSec": timestamp,
            "selectionEvidence": selected["selectionEvidence"],
        })
    summary = {
        "schemaVersion": 1,
        "status": "succeeded",
        "highlightPolicy": selection["policy"],
        "unavailableMetrics": selection["unavailableMetrics"],
        "frames": frames,
    }
    _write_json(output_dir / "summary.json", summary)
    return summary
