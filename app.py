import io
import os
import re
import secrets
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from flask import Flask, Response, render_template_string, request, send_file

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4096

SITE_NAME = "My Video Downloader"
MAX_BYTES = 25 * 1024 * 1024

PAGE = """
<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ name }}</title>
<style>
  body { font: 1rem system-ui; max-width: 34rem; margin: 4rem auto; padding: 0 1rem; }
  input, button { font: inherit; padding: .7rem; margin-top: .5rem; }
  input { width: 100%; box-sizing: border-box; }
  button { cursor: pointer; }
  [role=alert] { color: #a31621; }
</style>
<h1>{{ name }}</h1>
<p>Download a short video you own or have permission to download.</p>
<form method="post">
  <label for="url">YouTube video link</label>
  <input id="url" name="url" type="url"
         placeholder="https://www.youtube.com/watch?v=..." required>
  <button type="submit">Download MP4</button>
</form>
{% if error %}<p role="alert">{{ error }}</p>{% endif %}
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
        if parts.path != "/watch":
            return None
        candidate = parse_qs(parts.query).get("v", [""])[0]
    else:
        return None
    return candidate if re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate) else None


@app.route("/", methods=["GET", "POST"])
def download():
    password = os.environ.get("APP_PASSWORD")
    if not password:
        return "Set APP_PASSWORD in Railway Variables first.", 503

    auth = request.authorization
    if (
        not auth
        or auth.username != "owner"
        or not secrets.compare_digest(
            (auth.password or "").encode(), password.encode()
        )
    ):
        return Response(
            "Sign in to use the downloader.",
            401,
            {"WWW-Authenticate": 'Basic realm="Video Downloader"'},
        )

    if request.method == "GET":
        return render_template_string(PAGE, name=SITE_NAME)

    video_id = video_id_from(request.form.get("url", "").strip())
    if not video_id:
        return render_template_string(
            PAGE, name=SITE_NAME, error="Paste a YouTube video link."
        ), 400

    with tempfile.TemporaryDirectory() as folder:
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
                timeout=90,
            )
        except subprocess.TimeoutExpired:
            return render_template_string(
                PAGE, name=SITE_NAME, error="The download timed out. Try a shorter video."
            ), 504
        except subprocess.CalledProcessError:
            return render_template_string(
                PAGE, name=SITE_NAME,
                error="The video could not be downloaded. Check the Railway logs for details.",
            ), 502

        if not output.is_file() or output.stat().st_size > MAX_BYTES:
            return render_template_string(
                PAGE, name=SITE_NAME, error="Try a smaller video with an MP4 format."
            ), 422

        return send_file(
            io.BytesIO(output.read_bytes()),
            mimetype="video/mp4",
            as_attachment=True,
            download_name=f"{video_id}.mp4",
        )
