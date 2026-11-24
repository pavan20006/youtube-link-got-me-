import json
import os
import re
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from flask import Flask, Response, render_template_string, request, url_for

app = Flask(__name__)
downloads = Path.home() / "Downloads"
qualities = {
    "best": "Best available", "1080": "Up to 1080p", "720": "Up to 720p",
    "480": "Up to 480p", "360": "Up to 360p",
}
video_quality_limits = ("360", "480", "720", "1080")
yt_dlp_command = ["yt-dlp", "--ignore-config", "--no-playlist"]
progress_type = "application/x-ndjson"
download_prefix = "DOWNLOADER_PROGRESS "
processing_prefix = "DOWNLOADER_PROCESSING "
download_complete = "Download complete. Open your Downloads folder."
download_failed = "Download failed. Check the PowerShell window."


@dataclass
class ActiveDownload:
    process: subprocess.Popen
    cancelled: bool = False


active_downloads = {}
active_downloads_lock = Lock()

page = r"""
<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>My Video Downloader</title>
<style>
  progress { width: min(100%, 24rem); height: 1.25rem; }
  .video-preview {
    display: flex;
    flex-wrap: wrap;
    gap: 1rem;
    align-items: center;
    max-width: 36rem;
    margin: 1rem 0;
    padding: 0.75rem;
    border: 1px solid #b8bdc6;
    border-radius: 0.5rem;
  }
  .video-preview img { max-width: 100%; height: auto; border-radius: 0.25rem; }
  .video-preview div { flex: 1 1 12rem; min-width: 0; overflow-wrap: anywhere; }
  .video-preview h2 { font-size: 1.125rem; margin: 0; }
  .video-preview p { margin: 0.375rem 0 0; }
  .quality-comparison { margin: 0 0 1rem; min-width: 0; max-width: 37.5rem; }
  .quality-comparison table { width: 100%; border-collapse: collapse; }
  .quality-comparison th, .quality-comparison td { text-align: left; padding: 0.5rem 0.375rem; border-bottom: 1px solid #b8bdc6; }
  .quality-comparison label { cursor: pointer; }
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
    <div id="checked-video">
      <article class="video-preview" aria-labelledby="video-title">
        <img src="https://i.ytimg.com/vi/{{ preview.id }}/hqdefault.jpg" alt=""
             width="192" height="144" referrerpolicy="no-referrer">
        <div>
          <h2 id="video-title">{{ preview.title }}</h2>
          <p>Duration: {{ preview.duration }}</p>
          <p id="preview-size" role="status">{{ size }}</p>
        </div>
      </article>
      {% if comparisons %}
        <fieldset class="quality-comparison">
          <legend>Compare video qualities</legend>
          <table>
            <thead><tr><th scope="col">Quality</th><th scope="col">Resolution</th><th scope="col">File size</th></tr></thead>
            <tbody>
              {% for option in comparisons %}
                <tr>
                  <td>
                    {% if option.available %}
                      <label><input type="radio" name="compared_quality" value="{{ option.quality }}"
                                    data-size="{{ option.message }}" {% if option.quality == quality %}checked{% endif %}>
                        {{ qualities[option.quality] }}</label>
                    {% else %}
                      {{ qualities[option.quality] }}
                    {% endif %}
                  </td>
                  <td>{{ option.resolution }}</td>
                  <td>{{ option.size }}</td>
                </tr>
              {% endfor %}
            </tbody>
          </table>
        </fieldset>
      {% endif %}
      {% for selection in checked %}
        <input type="hidden" name="checked" value="{{ selection }}">
      {% endfor %}
      <button type="submit" name="action" value="download">Download</button>
    </div>
  {% endif %}
</form>
<div id="download-progress" hidden>
  <progress id="progress-bar" aria-label="Current download progress" max="100"></progress>
  <p id="progress-details"></p>
  <button id="cancel-download" type="button" hidden>Cancel download</button>
</div>
<p id="download-status" role="status">{{ message }}</p>
<script>
  const form = document.querySelector('#download-form');
  const progress = document.querySelector('#download-progress');
  const bar = document.querySelector('#progress-bar');
  const details = document.querySelector('#progress-details');
  const status = document.querySelector('#download-status');
  const cancel = document.querySelector('#cancel-download');
  const checkedVideo = document.querySelector('#checked-video');
  const size = bytes => `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
  let cancelUrl = null;
  let cancelling = false;

  form.addEventListener('input', event => {
    if (event.target.name === 'compared_quality') {
      form.elements.quality.value = event.target.value;
      document.querySelector('#preview-size').textContent = event.target.dataset.size;
      return;
    }
    if (checkedVideo) checkedVideo.hidden = true;
  });

  cancel.addEventListener('click', async () => {
    if (!cancelUrl || cancelling) return;
    const url = cancelUrl;
    cancelling = true;
    cancel.disabled = true;
    status.textContent = 'Cancelling download…';
    try {
      const response = await fetch(url, {method: 'POST'});
      // The download may have finished before the cancellation request arrived.
      if (!response.ok && response.status !== 404 && response.status !== 409) {
        throw new Error('Cancellation failed');
      }
    } catch {
      if (cancelUrl === url) {
        cancelling = false;
        cancel.disabled = false;
        status.textContent = 'Could not cancel. Try again.';
      }
    }
  });

  function showProgress(event) {
    if (status.textContent !== event.message) status.textContent = event.message;
    if (event.state === 'complete') {
      bar.value = 100;
      details.textContent = '100%';
      return;
    }
    if (event.state === 'error' || event.state === 'cancelled') progress.hidden = true;

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
          if (update.cancel_url) {
            cancelUrl = update.cancel_url;
            cancel.hidden = false;
            cancel.disabled = false;
          }
          finished = ['complete', 'error', 'cancelled'].includes(update.state);
          if (finished) cancel.hidden = true;
          if (!cancelling || finished) showProgress(update);
        }
      }
      if (!finished) throw new Error('Download connection closed');
    } catch {
      controller.abort();
      showProgress({state: 'error', message: 'Download interrupted. Check the PowerShell window.'});
    } finally {
      cancelUrl = null;
      cancelling = false;
      cancel.hidden = true;
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


def media_size(video, media_type):
    duration = video.get("duration")
    if media_type == "mp3":
        if duration:
            # FFmpeg's default MP3 conversion here uses variable bitrate.
            size = duration * 130_000 / 8
            return f"{size / 1024**2:.2f} MiB", True
    else:
        tracks = video.get("requested_formats")
        if not tracks and video.get("ext") == "mp4" and video.get("filesize"):
            return f"{video['filesize'] / 1024**2:.2f} MiB", False
        sizes = []
        for track in tracks or [video]:
            size = track.get("filesize") or track.get("filesize_approx")
            if not size and duration and track.get("tbr"):
                size = duration * track["tbr"] * 1000 / 8
            if not size:
                break
            sizes.append(size)
        else:
            return f"{sum(sizes) / 1024**2:.2f} MiB", True
    return None, False


def size_message(size, approximate, media_type):
    if size is None:
        return "File size is unavailable before downloading."
    if approximate:
        note = " (actual size may vary)." if media_type == "mp3" else ""
        return f"Estimated {media_type.upper()} size: {size}{note}"
    return f"File size: {size}"


def video_format(quality):
    return f"bv*[height<={quality}]+ba/b[height<={quality}]"


def compare_video_formats(metadata):
    # Each selector has a fallback so yt-dlp emits one result per requested limit.
    # Validate the returned resolution before offering that row as a download.
    selectors = ",".join(f"{video_format(quality)}/bv*+ba/b" for quality in video_quality_limits)
    result = subprocess.run(
        [
            *yt_dlp_command, "-t", "mp4", "-f", selectors, "--skip-download",
            "--print", "%(.{height,ext,filesize,filesize_approx,tbr,requested_formats})j",
            "--load-info-json", "-",
        ],
        input=metadata, stdout=subprocess.PIPE, encoding="utf-8", check=True, timeout=10,
    )
    formats = [json.loads(line) for line in result.stdout.splitlines()]
    if len(formats) != len(video_quality_limits):
        raise ValueError("Unexpected number of format selections")
    return {
        quality: selected
        for quality, selected in zip(video_quality_limits, formats)
        if selected.get("height") and selected["height"] <= int(quality)
    }


def comparison_row(quality, selected, duration):
    if selected is None:
        return {"quality": quality, "available": False, "resolution": "—", "size": "Unavailable"}
    size, approximate = media_size({**selected, "duration": duration}, "mp4")
    height = selected.get("height")
    return {
        "quality": quality,
        "available": True,
        "resolution": f"{height}p" if height else "Unknown",
        "size": (f"About {size}" if approximate else size) or "Unknown",
        "message": size_message(size, approximate, "mp4"),
    }


def video_duration(video):
    if video.get("is_live"):
        return "Live"
    duration = video.get("duration")
    if duration is None:
        return "Unavailable"
    minutes, seconds = divmod(int(duration), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def stop_process_tree(process):
    if process.poll() is not None:
        return False
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, check=True, timeout=5,
            )
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return False
    except subprocess.CalledProcessError:
        if process.poll() is not None:
            return False
        raise
    return True


@app.post("/downloads/<download_id>/cancel")
def cancel_download(download_id):
    with active_downloads_lock:
        job = active_downloads.get(download_id)
        if job is None:
            return "", 404
        if job.cancelled:
            return "", 202
        try:
            job.cancelled = stop_process_tree(job.process)
        except (OSError, subprocess.SubprocessError) as error:
            print(f"Could not cancel the download: {error}", flush=True)
            return "", 500
        return "", 202 if job.cancelled else 409


def download_events(command, url, download_id, cancel_url):
    def event(state, message, **details):
        return json.dumps({"state": state, "message": message, **details}) + "\n"

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
            start_new_session=os.name != "nt",
        ) as process:
            job = ActiveDownload(process)
            with active_downloads_lock:
                active_downloads[download_id] = job
            try:
                yield event("starting", "Starting download…", cancel_url=cancel_url)
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
                with active_downloads_lock:
                    active_downloads.pop(download_id)
                if not job.cancelled:
                    try:
                        stop_process_tree(process)
                    except (OSError, subprocess.SubprocessError):
                        if process.poll() is None:
                            process.kill()
        if job.cancelled:
            yield event("cancelled", "Download cancelled.")
        elif returncode == 0:
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
    media_type = "mp3" if request.form.get("format") == "mp3" else "mp4"
    downloading = request.form.get("action") == "download"
    if downloading and media_type == "mp4":
        quality = request.form.get("compared_quality", quality)
    if quality not in qualities:
        quality = "720"
    message = ""
    checked = []
    size = ""
    preview = None
    comparisons = []
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
                options += ["-f", video_format(quality)]

            command = [*yt_dlp_command, *options]
            url = f"https://www.youtube.com/watch?v={key}"
            selection = f"{key}:{quality}:{media_type}"
            if downloading and selection in request.form.getlist("checked"):
                if streaming:
                    download_id = uuid4().hex
                    cancel_url = url_for("cancel_download", download_id=download_id)
                    return Response(
                        download_events(command, url, download_id, cancel_url), mimetype=progress_type,
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
                    preview_options = ["-t", "mp4"] if media_type == "mp4" else options
                    result = subprocess.run(
                        [*yt_dlp_command, *preview_options, "--skip-download", "--dump-single-json", "--", url],
                        check=True,
                        stdout=subprocess.PIPE,
                        encoding="utf-8",
                        timeout=30,
                    )
                    video = json.loads(result.stdout)
                    preview = {
                        "id": key,
                        "title": video.get("title") or "YouTube video",
                        "duration": video_duration(video),
                    }
                except subprocess.TimeoutExpired:
                    message = "The size check timed out. Try again."
                except (OSError, subprocess.CalledProcessError, ValueError):
                    message = "Could not check the size. Check the PowerShell window."
                else:
                    if media_type == "mp4":
                        available = {"best": video}
                        try:
                            available.update(compare_video_formats(result.stdout))
                        except (OSError, subprocess.SubprocessError, ValueError):
                            message = "Could not compare qualities. Best available is still ready to download."
                        else:
                            comparisons = [
                                comparison_row(limit, available.get(limit), video.get("duration"))
                                for limit in (*video_quality_limits, "best")
                            ]
                        if quality not in available:
                            quality = "best"
                            if comparisons:
                                message = "That quality is unavailable. Choose an available row below."
                        selected = {**available[quality], "duration": video.get("duration")}
                        checked = [f"{key}:{limit}:mp4" for limit in available]
                    else:
                        selected = video
                        checked = [selection]
                    amount, approximate = media_size(selected, media_type)
                    size = size_message(amount, approximate, media_type)
    if streaming:
        return {"message": message or "Check the size for these settings before downloading."}, 400
    return render_template_string(
        page, link=link, quality=quality, qualities=qualities, media_type=media_type,
        message=message, checked=checked, size=size, preview=preview, comparisons=comparisons,
    )
