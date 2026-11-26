import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
from collections import OrderedDict
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from http.client import HTTPException
from math import ceil
from pathlib import Path
from threading import Lock
from time import monotonic
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from flask import Flask, Response, render_template, render_template_string, request, url_for
from jinja2 import DictLoader

app = Flask(__name__)
downloads = Path.home() / "Downloads"
history_database = Path.home() / ".video-downloader" / "history.sqlite3"
qualities = {
    "best": "Best available", "1080": "Up to 1080p", "720": "Up to 720p",
    "480": "Up to 480p", "360": "Up to 360p",
}
video_quality_limits = ("360", "480", "720", "1080")
yt_dlp_command = ["yt-dlp", "--ignore-config", "--no-playlist"]
progress_type = "application/x-ndjson"
download_prefix = "DOWNLOADER_PROGRESS "
processing_prefix = "DOWNLOADER_PROCESSING "
finished_file_prefix = "DOWNLOADER_FILE "
download_complete = "Download complete. Open your Downloads folder."
download_failed = "Download failed. Check the PowerShell window."
photo_types = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


@dataclass
class ActiveDownload:
    process: subprocess.Popen | None = None
    cancelled: bool = False


active_downloads = {}
active_video_ids = set()
active_downloads_lock = Lock()
preview_cache = OrderedDict()
preview_cache_lock = Lock()
preview_cache_seconds = 600
preview_cache_capacity = 32
platform_pause_lock = Lock()
platform_pause_until = {}
platform_pause_seconds = 60

app.jinja_loader = DictLoader({"history.html": """
{% if history %}
  <ul class="history-list">
    {% for item in history %}
      <li>
        <strong>{{ item.title }}</strong>
        <p>{{ item.format }}{% if item.height %} · {{ item.height }}p{% endif %} · {{ item.size }} · {{ item.date }}</p>
        <form action="{{ url_for('open_history_item', item_id=item.id) }}" method="post">
          <button type="submit" name="target" value="file">Open file</button>
          <button type="submit" name="target" value="folder">Open folder</button>
        </form>
      </li>
    {% endfor %}
  </ul>
{% elif history_query or history_format %}
  <p>No downloads match your filters.</p>
{% else %}
  <p>No completed downloads yet.</p>
{% endif %}
"""})

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
  .photo-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(7rem, 1fr)); gap: 0.75rem; padding: 0; list-style: none; }
  .photo-grid img { display: block; width: 100%; aspect-ratio: 1; object-fit: contain; background: #edf0f4; }
  .photo-grid span { display: block; margin-top: 0.25rem; }
  .quality-comparison { margin: 0 0 1rem; min-width: 0; max-width: 37.5rem; }
  .quality-comparison table { width: 100%; border-collapse: collapse; }
  .quality-comparison th, .quality-comparison td { text-align: left; padding: 0.5rem 0.375rem; border-bottom: 1px solid #b8bdc6; }
  .quality-comparison label { cursor: pointer; }
  .download-history { margin-top: 1.5rem; max-width: 38rem; }
  .download-job { max-width: 36rem; padding: 0.75rem; margin: 0.75rem 0; border: 1px solid #b8bdc6; border-radius: 0.5rem; }
  .download-job h3 { margin: 0 0 0.5rem; font-size: 1.125rem; overflow-wrap: anywhere; }
  .download-job p { margin: 0.375rem 0; }
  .history-filters { display: flex; flex-wrap: wrap; align-items: end; gap: 0.5rem; }
  .history-filters div { display: grid; gap: 0.25rem; }
  .history-filters input, .history-filters select, .history-filters button { font: inherit; }
  .history-list { list-style: none; padding: 0; }
  .history-list li { padding: 0.75rem 0; border-bottom: 1px solid #b8bdc6; overflow-wrap: anywhere; }
  .history-list p { margin: 0.35rem 0; }
</style>
<h1>My Video Downloader</h1>
<form id="download-form" method="post" action="{{ url_for('index') }}">
  <label for="url">YouTube or Instagram link</label>
  <input id="url" name="url" type="url" value="{{ link }}" required>
  <label for="quality">Video quality</label>
  <select id="quality" name="quality" {% if is_instagram %}disabled{% endif %}>
    {% for value, label in qualities.items() %}
      <option value="{{ value }}" {% if value == quality %}selected{% endif %}>{{ label }}</option>
    {% endfor %}
  </select>
  <label for="format">Format</label>
  <select id="format" name="format">
    <option value="mp4">MP4 video with sound</option>
    <option value="mp3" {% if media_type == 'mp3' %}selected{% endif %}>MP3 audio only</option>
    <option value="image" {% if media_type == 'image' %}selected{% endif %}>Instagram photo</option>
  </select>
  <button type="submit" name="action" value="check">Check size</button>
  <div id="checked-result">
  {% if checked %}
    <div id="checked-video">
      <article class="video-preview" aria-labelledby="video-title">
        {% if is_instagram and preview.thumbnail and preview.photos | length <= 1 %}
        <img src="{{ preview.thumbnail }}" alt="" width="192" height="144" referrerpolicy="no-referrer">
        {% elif not is_instagram %}
        <img src="https://i.ytimg.com/vi/{{ preview.id }}/hqdefault.jpg" alt=""
             width="192" height="144" referrerpolicy="no-referrer">
        {% endif %}
        <div>
          <h2 id="video-title">{{ preview.title }}</h2>
          {% if media_type != 'image' %}<p>Duration: {{ preview.duration }}</p>{% endif %}
          {% if preview.photos | length > 1 %}
          <p>{{ preview.photos | length }} photos</p>
          <ul class="photo-grid">
            {% for photo in preview.photos %}
            <li>
              <img src="{{ photo.url }}" alt="" width="160" height="160" loading="lazy" referrerpolicy="no-referrer">
              <span>Slide {{ photo.position }}</span>
            </li>
            {% endfor %}
          </ul>
          {% else %}
          <p id="preview-size" role="status">{{ size }}</p>
          {% endif %}
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
      <button type="submit" name="action" value="download">{{ 'Download all photos' if preview.photos | length > 1 else 'Download image' if media_type == 'image' else 'Download' }}</button>
    </div>
  {% endif %}
  </div>
</form>
<div id="existing-result">
{% if existing %}
  <section id="existing-video" aria-labelledby="existing-title">
    <article class="video-preview">
      <div>
        <h2 id="existing-title">Already downloaded</h2>
        <p>{{ existing.title }}</p>
        <p>{{ existing.format }}{% if existing.height %} · {{ existing.height }}p{% endif %} · {{ existing.size }}</p>
        <form action="{{ url_for('open_history_item', item_id=existing.id) }}" method="post">
          <button type="submit" name="target" value="file">Open existing file</button>
        </form>
        {% if not checked %}
          <button type="submit" form="download-form" name="action" value="check-online">Check online sizes</button>
        {% endif %}
        <p id="existing-status" role="status"></p>
      </div>
    </article>
  </section>
{% endif %}
</div>
<p id="download-status" role="status">{{ message }}</p>
<section id="download-jobs" aria-labelledby="jobs-title" hidden>
  <h2 id="jobs-title">Downloads</h2>
  <div id="download-job-list"></div>
</section>
<template id="download-job-template">
  <article class="download-job">
    <h3></h3>
    <p class="job-status" role="status"></p>
    <div class="job-progress">
      <progress aria-label="Download progress" max="100"></progress>
      <p class="job-details"></p>
      <button class="cancel-download" type="button" hidden>Cancel download</button>
    </div>
  </article>
</template>
<section class="download-history" aria-labelledby="history-title">
  <h2 id="history-title">Download history</h2>
  <form id="history-filters" class="history-filters" method="get" action="{{ url_for('index') }}">
    <div>
      <label for="history-query">Search by title</label>
      <input id="history-query" type="search" name="history_query" value="{{ history_query }}" maxlength="200">
    </div>
    <div>
      <label for="history-format">File format</label>
      <select id="history-format" name="history_format">
        <option value="">All formats</option>
        <option value="MP4" {% if history_format == 'MP4' %}selected{% endif %}>MP4</option>
        <option value="MP3" {% if history_format == 'MP3' %}selected{% endif %}>MP3</option>
        <option value="PHOTOS" {% if history_format == 'PHOTOS' %}selected{% endif %}>Photos</option>
      </select>
    </div>
    <button type="submit">Filter history</button>
  </form>
  <div id="history-entries" data-url="{{ url_for('download_history') }}">
    {% include 'history.html' %}
  </div>
  <p id="history-status" role="status"></p>
</section>
<script>
  const form = document.querySelector('#download-form');
  const checkStatus = document.querySelector('#download-status');
  const checkedResult = document.querySelector('#checked-result');
  const existingResult = document.querySelector('#existing-result');
  const jobs = document.querySelector('#download-jobs');
  const jobList = document.querySelector('#download-job-list');
  const jobTemplate = document.querySelector('#download-job-template');
  const historyForm = document.querySelector('#history-filters');
  const historyEntries = document.querySelector('#history-entries');
  const historyStatus = document.querySelector('#history-status');
  const size = bytes => `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
  let youtubeQuality = form.elements.quality.disabled ? '720' : form.elements.quality.value;
  let previewRequest;
  let historyFilters = new URLSearchParams(new FormData(historyForm));
  let historyRequest;

  async function refreshHistory() {
    historyRequest?.abort();
    const controller = new AbortController();
    historyRequest = controller;
    const url = new URL(historyEntries.dataset.url, window.location.href);
    url.search = historyFilters;
    try {
      const response = await fetch(url, {cache: 'no-store', signal: controller.signal});
      if (!response.ok) throw new Error('History refresh failed');
      const html = await response.text();
      if (historyRequest === controller) {
        historyEntries.innerHTML = html;
        historyStatus.textContent = '';
      }
    } catch {
      if (!controller.signal.aborted) {
        historyStatus.textContent = 'Could not refresh download history. Try again.';
      }
    }
  }

  historyForm.addEventListener('submit', event => {
    event.preventDefault();
    historyFilters = new URLSearchParams(new FormData(historyForm));
    refreshHistory();
  });

  async function openSavedDownload(event, feedback) {
    if (!event.submitter) return;
    event.preventDefault();
    const data = new FormData();
    data.set('target', event.submitter.value);
    try {
      const response = await fetch(event.target.getAttribute('action'), {method: 'POST', body: data});
      const {message} = await response.json();
      feedback.textContent = message;
    } catch {
      feedback.textContent = 'Could not open this download.';
    }
  }

  historyEntries.addEventListener('submit', event => openSavedDownload(event, historyStatus));
  existingResult.addEventListener('submit', event => {
    openSavedDownload(event, document.querySelector('#existing-status'));
  });

  form.addEventListener('input', event => {
    if (event.target.name === 'compared_quality') {
      form.elements.quality.value = event.target.value;
      youtubeQuality = event.target.value;
      document.querySelector('#preview-size').textContent = event.target.dataset.size;
      return;
    }
    previewRequest?.abort();
    checkedResult.replaceChildren();
    checkStatus.textContent = '';
    if (['url', 'format'].includes(event.target.name)) existingResult.replaceChildren();
    if (event.target.name === 'quality') youtubeQuality = event.target.value;
    if (event.target.name === 'url' && form.elements.quality.disabled) {
      form.elements.quality.disabled = false;
      form.elements.quality.value = youtubeQuality;
    }
  });

  async function checkSize(data) {
    previewRequest?.abort();
    const controller = new AbortController();
    previewRequest = controller;
    checkedResult.replaceChildren();
    checkStatus.textContent = 'Checking size…';
    try {
      const response = await fetch(form.getAttribute('action'), {
        method: 'POST', body: data, signal: controller.signal,
      });
      const page = new DOMParser().parseFromString(await response.text(), 'text/html');
      if (controller.signal.aborted) return;
      const quality = page.querySelector('#quality');
      form.elements.quality.value = quality.value;
      form.elements.quality.disabled = quality.disabled;
      if (!quality.disabled) youtubeQuality = quality.value;
      checkedResult.replaceChildren(...page.querySelector('#checked-result').childNodes);
      existingResult.replaceChildren(...page.querySelector('#existing-result').childNodes);
      checkStatus.textContent = page.querySelector('#download-status').textContent;
    } catch {
      if (!controller.signal.aborted) {
        checkStatus.textContent = 'Could not check the size. Try again.';
      }
    } finally {
      if (previewRequest === controller) previewRequest = null;
    }
  }

  function showProgress(job, event) {
    const {status, bar, details, progress} = job;
    if (status.textContent !== event.message) status.textContent = event.message;
    if (event.state === 'complete') {
      bar.value = 100;
      details.textContent = event.photo_count ? `${event.photo_count} photos saved` : '100%';
      return;
    }
    if (event.state === 'error' || event.state === 'cancelled') progress.hidden = true;
    if (event.photo_count) {
      bar.value = event.photos_saved / event.photo_count * 100;
      details.textContent = `${event.photos_saved} of ${event.photo_count} photos saved`;
      return;
    }

    const {downloaded, total, estimated, eta, speed} = event;
    const parts = [];
    if (total && downloaded != null) {
      const percent = Math.min(100, downloaded / total * 100);
      bar.value = percent;
      parts.push(`${Math.floor(percent)}%`, `${size(downloaded)} of ${estimated ? 'about ' : ''}${size(total)}`);
    } else {
      bar.removeAttribute('value');
      if (downloaded != null) parts.push(`${size(downloaded)} downloaded`);
    }
    if (Number.isFinite(speed) && speed > 0) {
      if (speed >= 1024 ** 2) parts.push(`${size(speed)}/s`);
      else if (speed >= 1024) parts.push(`${(speed / 1024).toFixed(1)} KiB/s`);
      else parts.push(`${Math.round(speed)} B/s`);
    }
    if (eta > 0) {
      const remaining = Math.ceil(eta);
      const minutes = Math.floor(remaining / 60);
      const seconds = remaining % 60;
      parts.push(`about ${minutes ? `${minutes} min ` : ''}${seconds} sec left`);
    }
    details.textContent = parts.join(' · ');
  }

  async function startDownload(data, submitter) {
    const card = jobTemplate.content.firstElementChild.cloneNode(true);
    const title = document.querySelector('#video-title')?.textContent || data.get('url');
    card.querySelector('h3').textContent = title;
    const job = {
      bar: card.querySelector('progress'),
      details: card.querySelector('.job-details'),
      progress: card.querySelector('.job-progress'),
      status: card.querySelector('.job-status'),
    };
    const cancel = card.querySelector('.cancel-download');
    let cancelUrl = null;
    let cancelling = false;
    cancel.addEventListener('click', async () => {
      if (!cancelUrl || cancelling) return;
      cancelling = true;
      cancel.disabled = true;
      job.status.textContent = 'Cancelling download…';
      try {
        const response = await fetch(cancelUrl, {method: 'POST'});
        // The download may have finished before the cancellation request arrived.
        if (!response.ok && response.status !== 404 && response.status !== 409) {
          throw new Error('Cancellation failed');
        }
      } catch {
        if (cancelUrl) {
          cancelling = false;
          cancel.disabled = false;
          job.status.textContent = 'Could not cancel. Try again.';
        }
      }
    });

    jobList.prepend(card);
    jobs.hidden = false;
    if (submitter) submitter.disabled = true;
    showProgress(job, {message: 'Starting download…'});

    const controller = new AbortController();
    try {
      const response = await fetch(form.getAttribute('action'), {
        method: 'POST', body: data, headers: {Accept: 'application/x-ndjson'}, signal: controller.signal,
      });
      if (response.status === 400) {
        const {message} = await response.json();
        showProgress(job, {state: 'error', message});
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
          if (!cancelling || finished) showProgress(job, update);
          if (update.history_saved) refreshHistory();
        }
      }
      if (!finished) throw new Error('Download connection closed');
    } catch {
      controller.abort();
      showProgress(job, {state: 'error', message: 'Download interrupted. Check the PowerShell window.'});
    } finally {
      cancelUrl = null;
      cancel.hidden = true;
      if (submitter) submitter.disabled = false;
    }
  }

  form.addEventListener('submit', event => {
    const action = event.submitter?.value || 'check';
    if (action === 'download' && (!window.ReadableStream || !window.TextDecoder)) return;
    event.preventDefault();
    const data = new FormData(form);
    data.set('action', action);
    if (action === 'download') startDownload(data, event.submitter);
    else checkSize(data);
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


@dataclass(frozen=True)
class VideoSource:
    platform: str
    key: str
    url: str


def video_source_from(link):
    try:
        parts = urlsplit(link)
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
        kind, key = post.groups()
        if kind == "reels":
            kind = "reel"
        return VideoSource("Instagram", key, f"https://www.instagram.com/{kind}/{key}/")
    key = video_id(link)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", key):
        return VideoSource("YouTube", key, f"https://www.youtube.com/watch?v={key}")
    return None


def instagram_cdn_url(value):
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    host = parts.hostname or ""
    if (
        parts.scheme == "https" and parts.username is None and port in (None, 443)
        and host.endswith((".cdninstagram.com", ".fbcdn.net"))
    ):
        return value
    return None


class InstagramCDNRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not instagram_cdn_url(newurl):
            return None
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            redirected.method = req.get_method()
        return redirected


def instagram_track_size(track, *, photo=False):
    url = instagram_cdn_url(track.get("url"))
    if not url or track.get("fragments") or track.get("protocol") not in (None, "http", "https"):
        return None

    media_headers = track.get("http_headers") or {}
    headers = {name: value for name, value in media_headers.items() if name.lower() in {"user-agent", "referer"}}
    headers["Accept-Encoding"] = "identity"
    opener = build_opener(InstagramCDNRedirect())
    for method in ("HEAD", "GET"):
        request_headers = {**headers, "Range": "bytes=0-0"} if method == "GET" else headers
        try:
            with opener.open(Request(url, headers=request_headers, method=method), timeout=3) as response:
                media_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
                expected_type = media_type in photo_types if photo else media_type.startswith(("video/", "audio/"))
                if encoding != "identity" or (
                    media_type and not expected_type and media_type not in {"application/octet-stream", "binary/octet-stream"}
                ):
                    continue
                if response.status == 206:
                    match = re.fullmatch(r"bytes 0-0/([1-9][0-9]{0,18})", response.headers.get("Content-Range", ""))
                    if match:
                        return int(match.group(1))
                elif response.status == 200:
                    length = response.headers.get("Content-Length", "")
                    if re.fullmatch(r"[1-9][0-9]{0,18}", length):
                        return int(length)
        except HTTPError as error:
            error.close()
            if error.code == 429:
                return None
        except OSError:
            continue
    return None


def format_mib(size):
    return f"{size / 1024**2:.2f} MiB"


def local_download_path(value):
    path = Path(value).resolve()
    if not path.is_relative_to(downloads.resolve()) or path.suffix.lower() not in {".mp4", ".mp3", *photo_types.values()}:
        raise ValueError("File is outside the supported downloads")
    return path


def record_download(video):
    path = local_download_path(video.get("filepath") or "")
    if not path.is_file():
        raise FileNotFoundError(path)
    size = path.stat().st_size
    media_type = path.suffix[1:].upper()
    height = video.get("height") if media_type == "MP4" else None
    downloaded_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    history_database.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(history_database)) as connection, connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS downloads (
                id INTEGER PRIMARY KEY,
                path TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                format TEXT NOT NULL,
                height INTEGER,
                size INTEGER NOT NULL,
                downloaded_at TEXT NOT NULL
            )
        """)
        connection.execute("""
            INSERT INTO downloads (path, title, format, height, size, downloaded_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET title=excluded.title, format=excluded.format,
                height=excluded.height, size=excluded.size, downloaded_at=excluded.downloaded_at
        """, (str(path), video.get("title") or path.stem, media_type, height, size, downloaded_at))


def history_rows(item_id=None, *, query="", media_format="", filename_suffix=""):
    if not history_database.is_file():
        return []
    with closing(sqlite3.connect(history_database)) as connection:
        connection.row_factory = sqlite3.Row
        if item_id is not None:
            return connection.execute("SELECT path FROM downloads WHERE id = ?", (item_id,)).fetchall()
        connection.create_function("casefold", 1, str.casefold)
        query = query.casefold()
        return connection.execute("""
            SELECT id, path, title, format, height, size, downloaded_at FROM downloads
            WHERE (? = '' OR instr(casefold(title), ?) > 0)
              AND (? = '' OR format = ? OR (? = 'PHOTOS' AND format IN ('JPG', 'PNG', 'WEBP')))
              AND (? = '' OR substr(path, -length(?)) = ?)
            ORDER BY downloaded_at DESC, id DESC LIMIT 50
        """, (query, query, media_format, media_format, media_format, filename_suffix, filename_suffix, filename_suffix)).fetchall()


def find_existing_download(key, media_type):
    # yt-dlp's default completed filename ends with the video ID before the extension.
    extensions = photo_types.values() if media_type == "image" else (f".{media_type}",)
    try:
        rows = [
            row for extension in extensions
            for row in history_rows(media_format=extension[1:].upper(), filename_suffix=f" [{key}]{extension}")
        ]
    except (OSError, sqlite3.Error) as error:
        print(f"Could not look up existing downloads: {error}", flush=True)
        return None
    for item in rows:
        try:
            path = local_download_path(item["path"])
            if path.is_file():
                return {**dict(item), "size": format_mib(path.stat().st_size)}
        except (OSError, RuntimeError, ValueError):
            continue
    return None


def recent_history(query, media_format):
    try:
        return [
            {
                **dict(item), "size": format_mib(item["size"]),
                "date": datetime.fromisoformat(item["downloaded_at"]).astimezone().strftime("%d %b %Y, %H:%M"),
            }
            for item in history_rows(query=query, media_format=media_format)
        ]
    except (OSError, sqlite3.Error, ValueError) as error:
        print(f"Could not read download history: {error}", flush=True)
        return []


def history_context():
    query = request.args.get("history_query", "").strip()[:200]
    media_format = request.args.get("history_format", "").upper()
    if media_format not in {"MP4", "MP3", "PHOTOS"}:
        media_format = ""
    return {
        "history": recent_history(query, media_format),
        "history_query": query,
        "history_format": media_format,
    }


@app.get("/history")
def download_history():
    return render_template("history.html", **history_context())


@app.post("/history/<int:item_id>/open")
def open_history_item(item_id):
    target = request.form.get("target")
    if target not in {"file", "folder"} or request.headers.get("Sec-Fetch-Site") == "cross-site":
        return {"message": "Invalid request."}, 400
    try:
        rows = history_rows(item_id)
        if not rows:
            return {"message": "This download could not be found in history."}, 404
        path = local_download_path(rows[0]["path"])
        if target == "folder":
            path = path.parent
        exists = path.is_dir() if target == "folder" else path.is_file()
        if not exists:
            return {"message": f"The {target} is no longer in Downloads."}, 404
        if os.name == "nt":
            os.startfile(path)
        else:
            launcher = "open" if sys.platform == "darwin" else "xdg-open"
            subprocess.Popen([launcher, str(path)])
    except (OSError, sqlite3.Error, ValueError) as error:
        print(f"Could not open download history item: {error}", flush=True)
        return {"message": f"Could not open the {target}."}, 500
    return {"message": f"Opening the {target}."}


def media_size(video, media_type):
    duration = video.get("duration")
    if media_type == "mp3":
        if duration:
            # FFmpeg's default MP3 conversion here uses variable bitrate.
            size = duration * 130_000 / 8
            return format_mib(size), True
    else:
        tracks = video.get("requested_formats")
        if not tracks and video.get("ext") == "mp4" and video.get("filesize"):
            return format_mib(video["filesize"]), False
        sizes = []
        for track in tracks or [video]:
            size = track.get("filesize") or track.get("filesize_approx")
            if not size and duration and track.get("tbr"):
                size = duration * track["tbr"] * 1000 / 8
            if not size:
                break
            sizes.append(size)
        else:
            return format_mib(sum(sizes)), True
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


def download_options(platform, media_type, quality="best"):
    options = ["--playlist-items", "1"] if platform == "Instagram" else []
    if media_type == "image":
        return ["--ignore-no-formats-error"]
    if media_type == "mp3":
        return [*options, "-x", "--audio-format", "mp3"]
    if platform == "Instagram":
        return [*options, "-f", "b[ext=mp4]/bv[ext=mp4]+ba[ext=m4a]", "--merge-output-format", "mp4"]
    options = ["-t", "mp4"]
    if quality != "best":
        options += ["-f", video_format(quality)]
    return options


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
            job.cancelled = job.process is None or stop_process_tree(job.process)
        except (OSError, subprocess.SubprocessError) as error:
            print(f"Could not cancel the download: {error}", flush=True)
            return "", 500
        return "", 202 if job.cancelled else 409


def platform_restriction(output, platform):
    output = output.lower()
    if any(reason in output for reason in ("http error 429", "429 too many requests", "exceeded the rate-limit")):
        return 429, f"{platform} is rate-limiting requests. Wait a while before trying again."
    if platform == "Instagram" and any(reason in output for reason in (
        "login required", "only available for registered users", "restricted video", "redirected to the login page",
    )):
        return 403, "Instagram requires sign-in for this video. Try a public post or Reel."
    if platform == "YouTube" and "sign in to confirm" in output and "not a bot" in output:
        return 403, "YouTube is asking for verification. Open the video on YouTube to check whether it plays."
    return None


def pause_platform_requests(platform):
    with platform_pause_lock:
        platform_pause_until[platform] = monotonic() + platform_pause_seconds


def platform_pause_message(platform):
    with platform_pause_lock:
        remaining = ceil(platform_pause_until.get(platform, 0) - monotonic())
    if remaining > 0:
        return f"The app is pausing {platform} requests for {remaining} more seconds. {platform} may need longer."
    return None


class PreviewRequestError(Exception):
    def __init__(self, message, http_status=200):
        super().__init__(message)
        self.http_status = http_status


def instagram_photos(video, key):
    entries = (video.get("entries") or []) if video.get("_type") == "playlist" else [video]
    photos = []
    for index, entry in enumerate(entries, 1):
        if not isinstance(entry, dict) or entry.get("formats") or entry.get("url") or entry.get("duration"):
            continue
        thumbnail = instagram_cdn_url(entry.get("thumbnail"))
        if thumbnail:
            position = entry.get("playlist_index") or index
            suffix = "" if position == 1 else f"-{position:02d}"
            photos.append({"position": position, "url": thumbnail, "key": f"instagram-{key}{suffix}"})
    return photos


def lookup_preview(key, media_type, url):
    source = video_source_from(url)
    platform = source.platform if source else "YouTube"
    cache_key = (platform, key, media_type)
    with preview_cache_lock:
        # Serialize metadata lookups so simultaneous checks can share the same result.
        cached = preview_cache.get(cache_key)
        if cached and cached[0] > monotonic():
            preview_cache.move_to_end(cache_key)
            return cached[1]
        pause = platform_pause_message(platform)
        if pause:
            raise PreviewRequestError(pause, 429)

        options = download_options(platform, media_type)
        failure = "Could not check the size. Check the PowerShell window."
        try:
            result = subprocess.run(
                [*yt_dlp_command, *options, "--skip-download", "--dump-single-json", "--", url],
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                encoding="utf-8", errors="replace", timeout=30,
            )
            video = json.loads(result.stdout)
            if platform == "Instagram" and media_type != "image" and video.get("_type") == "playlist":
                video = next((entry for entry in (video.get("entries") or []) if isinstance(entry, dict)), None)
                if video is None:
                    raise PreviewRequestError("No video was found in this Instagram post.")
            fallback_title = "Instagram photo" if media_type == "image" else f"{platform} video"
            preview = {"id": key, "title": video.get("title") or fallback_title, "duration": video_duration(video)}
            if platform == "Instagram":
                preview["thumbnail"] = instagram_cdn_url(video.get("thumbnail"))
            if media_type == "image":
                photos = instagram_photos(video, key)
                if not photos:
                    raise PreviewRequestError("No downloadable photos were found in this Instagram post.")
                preview["photos"] = photos
                preview["thumbnail"] = photos[0]["url"]
                if preview["title"].startswith("Video by "):
                    preview["title"] = "Photo by " + preview["title"].removeprefix("Video by ")
        except subprocess.TimeoutExpired as error:
            raise PreviewRequestError("The size check timed out. Try again.") from error
        except subprocess.CalledProcessError as error:
            output = error.stderr or ""
            if output:
                print(output, end="", file=sys.stderr, flush=True)
            restriction = platform_restriction(output, platform)
            if restriction:
                http_status, message = restriction
                if http_status == 429:
                    pause_platform_requests(platform)
                raise PreviewRequestError(message, http_status) from error
            if platform == "Instagram" and media_type != "image" and "there is no video in this post" in output.lower():
                raise PreviewRequestError("Choose Instagram photo and click Check size to save the photo.") from error
            raise PreviewRequestError(failure) from error
        except (OSError, ValueError) as error:
            raise PreviewRequestError(failure) from error

        if result.stderr:
            print(result.stderr, end="", file=sys.stderr, flush=True)
        comparisons = []
        message = ""
        if media_type == "image":
            length = instagram_track_size({"url": photos[0]["url"]}, photo=True) if len(photos) == 1 else None
            sizes = {"best": size_message(format_mib(length) if length else None, False, media_type)}
        elif media_type == "mp4" and platform == "YouTube":
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
            sizes = {row["quality"]: row["message"] for row in comparisons if row["available"]}
            if not sizes:
                sizes = {"best": comparison_row("best", video, video.get("duration"))["message"]}
        else:
            amount, approximate = media_size(video, media_type)
            if amount is None and platform == "Instagram" and media_type == "mp4":
                for track in video.get("requested_formats") or [video]:
                    if not track.get("filesize") and not track.get("filesize_approx"):
                        length = instagram_track_size(track)
                        if length:
                            track["filesize"] = length
                amount, approximate = media_size(video, media_type)
            sizes = {"best": size_message(amount, approximate, media_type)}

        data = (preview, sizes, comparisons, message)
        preview_cache[cache_key] = (monotonic() + preview_cache_seconds, data)
        preview_cache.move_to_end(cache_key)
        if len(preview_cache) > preview_cache_capacity:
            preview_cache.popitem(last=False)
        return data


def download_event(state, message, **details):
    return json.dumps({"state": state, "message": message, **details}) + "\n"


def reserve_download(key):
    with active_downloads_lock:
        if key in active_video_ids:
            return False
        active_video_ids.add(key)
        return True


def photo_extension(prefix):
    if prefix.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP":
        return ".webp"
    raise ValueError("The server did not return a supported photo")


def save_photo(photo, opener, job, show_bytes):
    url = instagram_cdn_url(photo["url"])
    if not url:
        raise ValueError("No Instagram photo URL is available")
    with tempfile.TemporaryDirectory(dir=downloads) as folder:
        partial = Path(folder) / "photo.part"
        request = Request(url, headers={"Accept-Encoding": "identity"})
        with opener.open(request, timeout=10) as response, partial.open("wb") as stream:
            prefix = response.read(12)
            extension = photo_extension(prefix)
            length = response.headers.get("Content-Length", "")
            total = int(length) if re.fullmatch(r"[1-9][0-9]{0,18}", length) else None
            stream.write(prefix)
            received = len(prefix)
            while not job.cancelled:
                data = response.read(128 * 1024)
                if not data:
                    break
                stream.write(data)
                received += len(data)
                if show_bytes:
                    yield download_event("downloading", "Downloading image…", downloaded=received, total=total)
        with active_downloads_lock:
            if job.cancelled:
                return None
            if total is not None and received != total:
                raise ValueError("The image transfer ended early")
            path = downloads / f"Instagram photo [{photo['key']}]{extension}"
            partial.replace(path)
    return path


def photo_download_events(source, download_id, cancel_url):
    pause = platform_pause_message(source.platform)
    if pause:
        yield download_event("error", pause, http_status=429)
        return
    key = (source.platform, source.key)
    if not reserve_download(key):
        yield download_event("error", "This post is already downloading.", http_status=409)
        return

    job = ActiveDownload()
    try:
        with active_downloads_lock:
            active_downloads[download_id] = job
        yield download_event("starting", "Starting photo download…", cancel_url=cancel_url)
        preview, _, _, _ = lookup_preview(source.key, "image", source.url)
        photos = preview["photos"]
        count = len(photos)
        saved = 0
        history_saved = False
        history_error = False
        rate_limited = False
        downloads.mkdir(exist_ok=True)
        opener = build_opener(InstagramCDNRedirect())
        for photo in photos:
            if job.cancelled:
                break
            if count > 1:
                yield download_event("downloading", f"Saving photo from slide {photo['position']}…", photos_saved=saved, photo_count=count)
            if find_existing_download(photo["key"], "image"):
                saved += 1
                continue
            try:
                path = yield from save_photo(photo, opener, job, show_bytes=count == 1)
            except HTTPError as error:
                error.close()
                if error.code == 429:
                    pause_platform_requests(source.platform)
                    rate_limited = True
                    break
                print(f"Instagram photo from slide {photo['position']} failed: HTTP {error.code}", flush=True)
                continue
            except (OSError, ValueError, HTTPException) as error:
                print(f"Could not save Instagram photo from slide {photo['position']}: {error}", flush=True)
                continue
            if path is None:
                break

            saved += 1
            title = f"{preview['title']} · Slide {photo['position']}" if count > 1 else preview["title"]
            try:
                record_download({"filepath": str(path), "title": title})
            except (OSError, sqlite3.Error, ValueError) as error:
                print(f"Could not save download history: {error}", flush=True)
                history_error = True
            else:
                history_saved = True

        with active_downloads_lock:
            active_downloads.pop(download_id, None)
        summary = f"Saved {saved} of {count} photos."
        if job.cancelled:
            message = f"Download cancelled. {summary}" if count > 1 else "Download cancelled."
            yield download_event("cancelled", message, history_saved=history_saved)
        elif rate_limited:
            message = "Instagram is rate-limiting requests. Try again later."
            if count > 1:
                message = f"{summary} {message}"
            yield download_event("error", message, http_status=429, history_saved=history_saved)
        elif saved < count:
            message = f"{summary} Try again for the remaining photos." if count > 1 else "Could not save the Instagram photo."
            yield download_event("error", message, history_saved=history_saved)
        else:
            message = f"{summary} Open your Downloads folder." if count > 1 else download_complete
            if history_error:
                message += " Some download history could not be saved."
            yield download_event("complete", message, history_saved=history_saved, photo_count=count if count > 1 else None)
    except PreviewRequestError as error:
        yield download_event("error", str(error), http_status=error.http_status)
    except (OSError, ValueError) as error:
        print(f"Photo download failed: {error}", flush=True)
        if job.cancelled:
            yield download_event("cancelled", "Download cancelled.")
        else:
            yield download_event("error", "Could not save the Instagram photo.")
    finally:
        with active_downloads_lock:
            active_downloads.pop(download_id, None)
            active_video_ids.remove(key)


def download_events(command, url, download_id, cancel_url):
    event = download_event

    source = video_source_from(url)
    platform = source.platform if source else "YouTube"
    pause = platform_pause_message(platform)
    if pause:
        yield event("error", pause, http_status=429)
        return
    key = (platform, source.key) if source else url
    if not reserve_download(key):
        yield event("error", "This video is already downloading.", http_status=409)
        return
    finished_video = None
    restriction = None
    try:
        downloads.mkdir(exist_ok=True)
        with subprocess.Popen(
            [
                *command, "--progress", "--newline", "--progress-delta", "0.5",
                "--progress-template",
                f"download:{download_prefix}%(info.vcodec)j\t"
                "%(progress.{status,downloaded_bytes,total_bytes,total_bytes_estimate,eta,speed})j",
                "--progress-template", f"postprocess:{processing_prefix}%(progress.postprocessor)s",
                "--print", f"after_move:{finished_file_prefix}" "%(.{filepath,title,height})j",
                "--no-simulate", "--no-quiet", "--encoding", "utf-8",
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
                    if line.startswith(finished_file_prefix):
                        try:
                            finished_video = json.loads(line.removeprefix(finished_file_prefix))
                        except ValueError:
                            continue
                    elif line.startswith(processing_prefix):
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
                            estimated=not total, eta=update.get("eta"), speed=update.get("speed"),
                        )
                    else:
                        print(line, end="", flush=True)
                        blocked = platform_restriction(line, platform)
                        if blocked and (restriction is None or blocked[0] == 429):
                            restriction = blocked
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
            try:
                if not isinstance(finished_video, dict):
                    raise ValueError("yt-dlp did not report the final file")
                record_download(finished_video)
            except (OSError, sqlite3.Error, ValueError) as error:
                print(f"Could not save download history: {error}", flush=True)
                yield event("complete", f"{download_complete} History could not be saved.")
            else:
                yield event("complete", download_complete, history_saved=True)
        elif restriction:
            http_status, message = restriction
            if http_status == 429:
                pause_platform_requests(platform)
            yield event("error", message, http_status=http_status)
        else:
            yield event("error", download_failed)
    except OSError as error:
        print(error, flush=True)
        yield event("error", download_failed)
    finally:
        with active_downloads_lock:
            active_video_ids.remove(key)


@app.route("/", methods=["GET", "POST"])
def index():
    link = request.form.get("url", "").strip()
    quality = request.form.get("quality", "720")
    media_type = request.form.get("format", "mp4")
    if media_type not in {"mp4", "mp3", "image"}:
        media_type = "mp4"
    action = request.form.get("action")
    downloading = action == "download"
    if downloading and media_type == "mp4":
        quality = request.form.get("compared_quality", quality)
    if quality not in qualities:
        quality = "720"
    message = ""
    checked = []
    size = ""
    preview = None
    existing = None
    comparisons = []
    is_instagram = False
    http_status = 200
    streaming = request.headers.get("Accept") == progress_type
    if request.method == "POST":
        source = video_source_from(link)
        if not source:
            message = "Paste a YouTube video link or a public Instagram post or Reel link."
        elif media_type == "image" and not source.url.startswith("https://www.instagram.com/p/"):
            message = "Choose Instagram photo with a public Instagram photo post link."
        else:
            is_instagram = source.platform == "Instagram"
            if is_instagram:
                quality = "best"
            options = download_options(source.platform, media_type, quality)
            history_key = f"instagram-{source.key}" if is_instagram else source.key
            if is_instagram:
                options += ["-o", f"%(title)s [{history_key}].%(ext)s"]
            command = [*yt_dlp_command, *options]
            selection = f"{history_key}:{quality}:{media_type}"
            if downloading and selection in request.form.getlist("checked"):
                download_id = uuid4().hex
                cancel_url = url_for("cancel_download", download_id=download_id)
                if media_type == "image":
                    events = photo_download_events(source, download_id, cancel_url)
                else:
                    events = download_events(command, source.url, download_id, cancel_url)
                if streaming:
                    return Response(
                        events, mimetype=progress_type,
                        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
                    )
                for event in events:
                    update = json.loads(event)
                    message = update["message"]
                    http_status = update.get("http_status", 200)
            else:
                if streaming:
                    return {"message": "Check the size for these settings before downloading."}, 400
                existing = find_existing_download(history_key, media_type)
                if media_type == "image" or not existing or action == "check-online":
                    try:
                        preview, sizes, comparisons, message = lookup_preview(source.key, media_type, source.url)
                    except PreviewRequestError as error:
                        http_status, message = error.http_status, str(error)
                    else:
                        if media_type == "mp4" and not is_instagram:
                            if quality not in sizes:
                                quality = "best"
                                if comparisons:
                                    message = "That quality is unavailable. Choose an available row below."
                            size = sizes[quality]
                            checked = [f"{source.key}:{limit}:mp4" for limit in sizes]
                        else:
                            size = sizes["best"]
                            checked = [selection]
                            if media_type == "image":
                                photos = preview["photos"]
                                existing = find_existing_download(photos[0]["key"], "image") if len(photos) == 1 else None
                                if existing and action != "check-online":
                                    checked = []
    if streaming:
        return {"message": message or "Check the size for these settings before downloading."}, 400
    return render_template_string(
        page, link=link, quality=quality, qualities=qualities, media_type=media_type,
        message=message, checked=checked, size=size, preview=preview, existing=existing,
        comparisons=comparisons, is_instagram=is_instagram, **history_context(),
    ), http_status
