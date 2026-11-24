import json
import re
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from flask import Flask, render_template_string, request

app = Flask(__name__)
downloads = Path.home() / "Downloads"
qualities = {
    "best": "Best available", "1080": "Up to 1080p", "720": "Up to 720p",
    "480": "Up to 480p", "360": "Up to 360p",
}

page = """
<!doctype html>
<html lang="en">
<meta charset="utf-8">
<title>My Video Downloader</title>
<h1>My Video Downloader</h1>
<form method="post">
  <label for="url">YouTube video link</label>
  <input id="url" name="url" type="url" value="{{ link }}" required>
  <label for="quality">Video quality</label>
  <select id="quality" name="quality">
    {% for value, label in qualities.items() %}
      <option value="{{ value }}" {% if value == quality %}selected{% endif %}>{{ label }}</option>
    {% endfor %}
  </select>
  <label for="format">Format</label>
  <select id="format" name="format">
    <option value="mp4">MP4 video with sound</option>
    <option value="mp3" {% if media_type == 'mp3' %}selected{% endif %}>MP3 audio only</option>
  </select>
  <button type="submit" name="action" value="check">Check size</button>
  {% if checked %}
    <p role="status">{{ size }}</p>
    <input type="hidden" name="checked" value="{{ checked }}">
    <button type="submit" name="action" value="download">Download</button>
  {% endif %}
</form>
<p role="status">{{ message }}</p>
</html>
"""

def video_id(link):
    try:
        url = urlsplit(link)
    except ValueError:
        return ""
    if url.scheme != "https":
        return ""
    if url.hostname == "youtu.be":
        return url.path.strip("/")
    if url.hostname in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        if url.path == "/watch":
            return parse_qs(url.query).get("v", [""])[0]
        if url.path.startswith("/shorts/"):
            return url.path.removeprefix("/shorts/").strip("/")
    return ""


def size_message(video, media_type):
    duration = video.get("duration")
    if media_type == "mp3":
        if duration:
            # FFmpeg's default MP3 conversion here uses variable bitrate.
            size = duration * 130_000 / 8
            return f"Estimated MP3 size: {size / 1024**2:.2f} MiB (actual size may vary)."
    else:
        tracks = video.get("requested_formats")
        if not tracks and video.get("ext") == "mp4" and video.get("filesize"):
            return f"File size: {video['filesize'] / 1024**2:.2f} MiB"
        sizes = []
        for track in tracks or [video]:
            size = track.get("filesize") or track.get("filesize_approx")
            if not size and duration and track.get("tbr"):
                size = duration * track["tbr"] * 1000 / 8
            if not size:
                break
            sizes.append(size)
        else:
            return f"Estimated MP4 size: {sum(sizes) / 1024**2:.2f} MiB"
    return "File size is unavailable before downloading."


@app.route("/", methods=["GET", "POST"])
def index():
    link = request.form.get("url", "").strip()
    quality = request.form.get("quality", "720")
    if quality not in qualities:
        quality = "720"
    media_type = "mp3" if request.form.get("format") == "mp3" else "mp4"
    message = ""
    checked = ""
    size = ""
    if request.method == "POST":
        key = video_id(link)
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", key):
            message = "Paste a YouTube video or Shorts link."
        else:
            options = ["-t", "mp4"]

            if media_type == "mp3":
                options = ["-x", "--audio-format", "mp3"]
            elif quality != "best":
                options += [
                    "-f", f"bv*[height<={quality}]+ba/b[height<={quality}]"
                ]

            command = ["yt-dlp", "--ignore-config", "--no-playlist", *options]
            url = f"https://www.youtube.com/watch?v={key}"
            selection = f"{key}:{quality}:{media_type}"
            if request.form.get("action") == "download" and request.form.get("checked") == selection:
                try:
                    downloads.mkdir(exist_ok=True)
                    subprocess.run([*command, "-P", str(downloads), "--", url], check=True)
                    message = "Download complete. Open your Downloads folder."
                except (OSError, subprocess.CalledProcessError):
                    message = "Download failed. Check the PowerShell window."
            else:
                try:
                    result = subprocess.run(
                        [*command, "--skip-download", "--dump-single-json", "--", url],
                        check=True,
                        stdout=subprocess.PIPE,
                        encoding="utf-8",
                        timeout=30,
                    )
                    size = size_message(json.loads(result.stdout), media_type)
                    checked = selection
                except subprocess.TimeoutExpired:
                    message = "The size check timed out. Try again."
                except (OSError, subprocess.CalledProcessError, ValueError):
                    message = "Could not check the size. Check the PowerShell window."
    return render_template_string(
        page, link=link, quality=quality, qualities=qualities, media_type=media_type,
        message=message, checked=checked, size=size,
    )
