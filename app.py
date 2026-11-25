import re
import subprocess
import tempfile
from contextlib import ExitStack
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, urlsplit

from flask import Flask, render_template_string, request, send_file

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4096

SITE_NAME = "Video Downloader"
MAX_BYTES = 25 * 1024 * 1024
download_slot = Lock()

PAGE = """
<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ name }}</title>
<style>
  body { font: 1rem/1.5 system-ui; background: #f4f7fc; color: #172641; margin: 0; padding: 3rem 1rem; }
  main { max-width: 35rem; margin: 0 auto; padding: clamp(1.25rem, 5vw, 2.5rem); background: white; border: 1px solid #c8d3e4; border-radius: 1rem; }
  h1 { line-height: 1.15; margin-top: 0; }
  label { display: block; margin-top: 1.5rem; font-weight: 600; }
  input, button { font: inherit; padding: .7rem .9rem; margin-top: .5rem; border-radius: .5rem; }
  input { width: 100%; box-sizing: border-box; border: 1px solid #6d7c91; }
  button { cursor: pointer; border: 1px solid transparent; background: #1647a6; color: white; font-weight: 600; }
  :focus-visible { outline: 2px solid #1647a6; outline-offset: 3px; }
  [role=alert] { color: #951b2e; }
</style>
<main>
<h1>{{ name }}</h1>
<p>Download a short video you own or have permission to download.</p>
<form method="post">
  <label for="url">YouTube video link</label>
  <input id="url" name="url" type="url" value="{{ link }}" maxlength="2048"
         placeholder="https://www.youtube.com/watch?v=..." required>
  <button type="submit">Download MP4</button>
</form>
<p>MP4 · Up to 480p · Up to 25 MiB</p>
{% if error %}<p role="alert">{{ error }}</p>{% endif %}
</main>
</html>
"""


def video_id_from(url):
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme != "https":
        return None
    if parts.hostname == "youtu.be":
        candidate = parts.path.removeprefix("/")
    elif parts.hostname in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        if parts.path == "/watch":
            candidate = parse_qs(parts.query).get("v", [""])[0]
        elif parts.path.startswith("/shorts/"):
            candidate = parts.path.removeprefix("/shorts/").rstrip("/")
        else:
            return None
    else:
        return None
    return candidate if re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate) else None


@app.get("/healthz")
def health():
    return "OK"

@app.route("/", methods=["GET", "POST"])
def download():
    def page(link="", error=""):
        return render_template_string(PAGE, name=SITE_NAME, link=link, error=error)

    if request.method == "GET":
        return page()

    link = request.form.get("url", "").strip()
    video_id = video_id_from(link)
    if not video_id:
        return page(link, "Paste a valid YouTube video or Shorts link."), 400
    if not download_slot.acquire(blocking=False):
        return page(link, "A download is in progress. Try again in a little while."), 503

    with ExitStack() as cleanup:
        cleanup.callback(download_slot.release)
        folder = cleanup.enter_context(tempfile.TemporaryDirectory())
        output = Path(folder) / "video.mp4"
        try:
            subprocess.run(
                [
                    "yt-dlp", "--ignore-config", "--no-playlist", "--no-progress",
                    "-f", "b[ext=mp4][height<=480]",
                    "--max-filesize", "25M",
                    "-o", str(output),
                    "--", f"https://www.youtube.com/watch?v={video_id}",
                ],
                check=True,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                errors="replace",
                timeout=90,
            )
        except subprocess.TimeoutExpired:
            return page(link, "The download timed out. Try a shorter video."), 504
        except subprocess.CalledProcessError as error:
            output_text = (error.stderr or "").lower()
            if "http error 429" in output_text:
                return page(link, "YouTube is rate-limiting requests. Try again later."), 429
            if "sign in to confirm" in output_text and "not a bot" in output_text:
                return page(link, "YouTube is asking for verification. Check whether the video plays on YouTube."), 403
            return page(link, "The video could not be downloaded as an MP4. Try another video."), 502
        except OSError:
            return page(link, "The downloader is temporarily unavailable."), 503

        if not output.is_file() or not 0 < output.stat().st_size <= MAX_BYTES:
            return page(link, "Try a smaller video with an MP4 format."), 422

        response = send_file(
            cleanup.enter_context(output.open("rb")),
            mimetype="video/mp4",
            as_attachment=True,
            download_name=f"{video_id}.mp4",
            conditional=False,
        )
        response.content_length = output.stat().st_size
        response.headers["Cache-Control"] = "no-store"
        response.direct_passthrough = False
        response.call_on_close(cleanup.pop_all().close)
        return response
