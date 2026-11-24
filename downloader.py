import json
import re
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from flask import Flask, Response, render_template_string, request

app = Flask(__name__)
downloads = Path.home() / "Downloads"
qualities = {
    "best": "Best available", "1080": "Up to 1080p", "720": "Up to 720p",
    "480": "Up to 480p", "360": "Up to 360p",
}
progress_type = "application/x-ndjson"
download_prefix = "DOWNLOADER_PROGRESS "
processing_prefix = "DOWNLOADER_PROCESSING "
download_complete = "Download complete. Open your Downloads folder."
download_failed = "Download failed. Check the PowerShell window."

page = r"""
<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>My Video Downloader</title>
<style>
  progress { width: min(100%, 24rem); height: 1.25rem; }
</style>
<h1>My Video Downloader</h1>
<form id="download-form" method="post" action="{{ url_for('index') }}">
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
<div id="download-progress" hidden>
  <progress id="progress-bar" aria-label="Current download progress" max="100"></progress>
  <p id="progress-details"></p>
</div>
<p id="download-status" role="status">{{ message }}</p>
<script>
  const form = document.querySelector('#download-form');
  const progress = document.querySelector('#download-progress');
  const bar = document.querySelector('#progress-bar');
  const details = document.querySelector('#progress-details');
  const status = document.querySelector('#download-status');
  const size = bytes => `${(bytes / 1024 ** 2).toFixed(1)} MiB`;

  function showProgress(event) {
    if (status.textContent !== event.message) status.textContent = event.message;
    if (event.state === 'complete') {
      bar.value = 100;
      details.textContent = '100%';
      return;
    }
    if (event.state === 'error') progress.hidden = true;

    const {downloaded, total, estimated, eta} = event;
    const parts = [];
    if (total && downloaded != null) {
      const percent = Math.min(100, downloaded / total * 100);
      bar.value = percent;
      parts.push(`${Math.floor(percent)}%`, `${size(downloaded)} of ${estimated ? 'about ' : ''}${size(total)}`);
    } else {
      bar.removeAttribute('value');
      if (downloaded != null) parts.push(`${size(downloaded)} downloaded`);
    }
    if (eta > 0) {
      const remaining = Math.ceil(eta);
      const minutes = Math.floor(remaining / 60);
      const seconds = remaining % 60;
      parts.push(`about ${minutes ? `${minutes} min ` : ''}${seconds} sec left`);
    }
    details.textContent = parts.join(' · ');
  }

  form.addEventListener('submit', async event => {
    if (event.submitter?.value !== 'download' || !window.ReadableStream || !window.TextDecoder) return;
    event.preventDefault();
    const data = new FormData(form);
    data.set('action', 'download');
    const controls = [...form.elements].filter(control => !control.disabled);
    controls.forEach(control => { control.disabled = true; });
    progress.hidden = false;
    showProgress({message: 'Starting download…'});

    const controller = new AbortController();
    try {
      const response = await fetch(form.getAttribute('action'), {
        method: 'POST', body: data, headers: {Accept: 'application/x-ndjson'}, signal: controller.signal,
      });
      if (response.status === 400) {
        const {message} = await response.json();
        showProgress({state: 'error', message});
        return;
      }
      if (!response.ok || !response.headers.get('Content-Type')?.includes('application/x-ndjson')) {
        throw new Error('Download request failed');
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let pending = '';
      let finished = false;
      while (true) {
        const {value, done} = await reader.read();
        if (done) break;
        pending += decoder.decode(value, {stream: true});
        const lines = pending.split('\n');
        pending = lines.pop();
        for (const line of lines) {
          const update = JSON.parse(line);
          showProgress(update);
          finished = update.state === 'complete' || update.state === 'error';
        }
      }
      if (!finished) throw new Error('Download connection closed');
    } catch {
      controller.abort();
      showProgress({state: 'error', message: 'Download interrupted. Check the PowerShell window.'});
    } finally {
      controls.forEach(control => { control.disabled = false; });
    }
  });
</script>
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


def download_events(command, url):
    def event(state, message, **details):
        return json.dumps({"state": state, "message": message, **details}) + "\n"

    yield event("starting", "Starting download…")
    try:
        downloads.mkdir(exist_ok=True)
        with subprocess.Popen(
            [
                *command, "--progress", "--newline", "--progress-delta", "0.5",
                "--progress-template",
                f"download:{download_prefix}%(info.vcodec)j\t"
                "%(progress.{status,downloaded_bytes,total_bytes,total_bytes_estimate,eta})j",
                "--progress-template", f"postprocess:{processing_prefix}%(progress.postprocessor)s",
                "-P", str(downloads), "--", url,
            ],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding="utf-8", errors="replace",
        ) as process:
            try:
                for line in process.stdout:
                    if line.startswith(processing_prefix):
                        step = line.removeprefix(processing_prefix).strip()
                        message = {
                            "Merger": "Merging video and audio…",
                            "ExtractAudio": "Converting to MP3…",
                        }.get(step, "Preparing the file…")
                        yield event("processing", message)
                    elif line.startswith(download_prefix):
                        codec, _, payload = line.removeprefix(download_prefix).partition("\t")
                        try:
                            update = json.loads(payload)
                        except ValueError:
                            continue
                        total = update.get("total_bytes")
                        downloaded = update.get("downloaded_bytes")
                        if downloaded is None and update.get("status") == "finished":
                            downloaded = total
                        message = "Downloading audio…" if codec == '"none"' else "Downloading video…"
                        yield event(
                            "downloading", message, downloaded=downloaded,
                            total=total or update.get("total_bytes_estimate"),
                            estimated=not total, eta=update.get("eta"),
                        )
                    else:
                        print(line, end="", flush=True)
                returncode = process.wait()
            finally:
                if process.poll() is None:
                    process.kill()
        if returncode == 0:
            yield event("complete", download_complete)
        else:
            yield event("error", download_failed)
    except OSError as error:
        print(error, flush=True)
        yield event("error", download_failed)


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
    streaming = request.headers.get("Accept") == progress_type
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
                if streaming:
                    return Response(
                        download_events(command, url), mimetype=progress_type,
                        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
                    )
                try:
                    downloads.mkdir(exist_ok=True)
                    subprocess.run([*command, "-P", str(downloads), "--", url], check=True)
                    message = download_complete
                except (OSError, subprocess.CalledProcessError):
                    message = download_failed
            else:
                if streaming:
                    return {"message": "Check the size for these settings before downloading."}, 400
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
    if streaming:
        return {"message": message or "Check the size for these settings before downloading."}, 400
    return render_template_string(
        page, link=link, quality=quality, qualities=qualities, media_type=media_type,
        message=message, checked=checked, size=size,
    )
