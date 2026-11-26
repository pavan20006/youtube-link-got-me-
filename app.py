import re
import subprocess
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, urlsplit

from flask import Flask, render_template_string, request, send_file

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4096

SITE_NAME = "Video Downloader"
download_slot = Lock()


@dataclass(frozen=True)
class VideoSource:
    platform: str
    video_id: str
    url: str


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
<p>Download a video you own or have permission to download.</p>
<form method="post">
  <label for="url">YouTube or Instagram video link</label>
  <input id="url" name="url" type="url" value="{{ link }}" maxlength="2048"
         placeholder="Paste a video or Reel link" required>
  <button type="submit">Download MP4</button>
</form>
<p>YouTube up to 480p. Instagram public video posts and Reels.</p>
{% if error %}<p role="alert">{{ error }}</p>{% endif %}
</main>
</html>
"""


def video_source_from(url):
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme != "https" or parts.username is not None:
        return None
    if parts.hostname in {"instagram.com", "www.instagram.com"}:
        post = re.fullmatch(
            r"/(?:(?!share/)[A-Za-z0-9_.]{1,30}/)?(p|reels?|tv)/([A-Za-z0-9_-]{5,28})/?",
            parts.path,
        )
        if not post or (post.group(1), post.group(2)) == ("reels", "audio"):
            return None
        kind, shortcode = post.groups()
        if kind == "reels":
            kind = "reel"
        return VideoSource("Instagram", shortcode, f"https://www.instagram.com/{kind}/{shortcode}/")
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
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate):
        return None
    return VideoSource("YouTube", candidate, f"https://www.youtube.com/watch?v={candidate}")


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
    source = video_source_from(link)
    if not source:
        return page(link, "Paste a YouTube video link or a public Instagram video post or Reel link."), 400
    if not download_slot.acquire(blocking=False):
        return page(link, "A download is in progress. Try again in a little while."), 503

    with ExitStack() as cleanup:
        cleanup.callback(download_slot.release)
        folder = cleanup.enter_context(tempfile.TemporaryDirectory())
        output = Path(folder) / "video.mp4"
        if source.platform == "Instagram":
            options = ["--playlist-items", "1", "-f", "b[ext=mp4]/bv[ext=mp4]+ba[ext=m4a]", "--merge-output-format", "mp4"]
        else:
            options = ["-f", "b[ext=mp4][height<=480]"]
        try:
            subprocess.run(
                [
                    "yt-dlp", "--ignore-config", "--no-playlist", "--no-progress",
                    *options, "-o", str(output), "--", source.url,
                ],
                check=True,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.CalledProcessError as error:
            output_text = (error.stderr or "").lower()
            if "http error 429" in output_text or "exceeded the rate-limit" in output_text:
                return page(link, f"{source.platform} is rate-limiting requests. Try again later."), 429
            if (
                "sign in to confirm" in output_text
                or "login required" in output_text
                or "only available for registered users" in output_text
                or "restricted video" in output_text
            ):
                return page(link, f"{source.platform} requires sign-in or verification for this video. Try a public video."), 403
            return page(link, "The video could not be downloaded as an MP4. Try another video."), 502
        except OSError:
            return page(link, "The downloader is temporarily unavailable."), 503

        content_length = output.stat().st_size if output.is_file() else 0
        if content_length == 0:
            return page(link, "No MP4 file was produced. Try another video."), 422

        response = send_file(
            cleanup.enter_context(output.open("rb")),
            mimetype="video/mp4",
            as_attachment=True,
            download_name=f"{source.video_id}.mp4",
            conditional=False,
        )
        response.content_length = content_length
        response.headers["Cache-Control"] = "no-store"
        response.direct_passthrough = False
        response.call_on_close(cleanup.pop_all().close)
        return response
