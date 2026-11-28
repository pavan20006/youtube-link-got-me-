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
from math import ceil, pi
from pathlib import Path
from threading import Lock
from time import monotonic
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
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
twitter_photo_headers = {"User-Agent": "Mozilla/5.0", "Accept": "image/*"}
history_sorts = {
    "newest": ("Newest first", "downloaded_at DESC, id DESC"),
    "oldest": ("Oldest first", "downloaded_at ASC, id ASC"),
    "largest": ("Largest files first", "size DESC, downloaded_at DESC, id DESC"),
}


@dataclass
class ActiveDownload:
    process: subprocess.Popen | None = None
    cancelled: bool = False
    batch: bool = False


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

app.jinja_loader = DictLoader({
    "app-style.css": """
:root {
  color-scheme: light;
  --ink: #182a22;
  --muted: #52655a;
  --surface: #fff;
  --wash: #f2f6ef;
  --line: #dbe4da;
  --green: #174b36;
  --green-hover: #103b2a;
  --focus: #38795b;
  font-family: "Segoe UI", system-ui, sans-serif;
}
*, *::before, *::after { box-sizing: border-box; }
body {
  min-height: 100vh;
  margin: 0;
  background: radial-gradient(ellipse at 50% 0, #e8f1e6, transparent 40rem), #f6f7f2;
  color: var(--ink);
  line-height: 1.5;
}
a { color: var(--green); }
button, input, select { font: inherit; }
button, .nav-link {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 0.5rem;
  padding: 0.55rem 0.9rem;
  min-height: 2.625rem;
  color: var(--green);
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 0.625rem;
  font-weight: 650;
  cursor: pointer;
}
button[hidden] { display: none; }
button:hover, .nav-link:hover { background: var(--wash); border-color: #b9cbbb; }
button:disabled { opacity: 0.55; cursor: default; }
.primary {
  color: #fff;
  background: var(--green);
  border-color: var(--green);
}
.primary:hover { background: var(--green-hover); border-color: var(--green-hover); }
:focus-visible { outline: 2px solid var(--focus); outline-offset: 3px; }
:is(input[type="url"], input[type="search"], select) {
  width: 100%;
  min-height: 3rem;
  padding: 0.625rem 0.75rem;
  border: 1px solid #c6d4c8;
  border-radius: 0.625rem;
  color: var(--ink);
  background: var(--surface);
}
input::placeholder { color: #65776d; }
input[type="checkbox"], input[type="radio"], progress { accent-color: var(--green); }
select:disabled { background: var(--wash); color: var(--muted); }
.site-header {
  width: min(68rem, 100% - 2rem);
  margin-inline: auto;
  min-height: 5rem;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  border-bottom: 1px solid var(--line);
}
.wordmark {
  display: flex;
  align-items: center;
  gap: 0.625rem;
  font-weight: 750;
  line-height: 1.2;
}
.brand-icon {
  display: grid;
  place-items: center;
  flex: 0 0 2.25rem;
  aspect-ratio: 1;
  border-radius: 0.625rem;
  background: #cae7ca;
  color: var(--green);
  font-size: 1.4rem;
}
.nav-link { text-decoration: none; font-size: 0.9rem; }
.page-shell { width: min(51rem, 100% - 2rem); margin: clamp(2.25rem, 8vw, 5rem) auto 4rem; }
.page-heading {
  margin: 0 0 1.5rem;
  font-size: clamp(2.2rem, 6vw, 3.65rem);
  line-height: 1.05;
  letter-spacing: -0.055em;
}
.panel {
  padding: clamp(1rem, 4vw, 2rem);
  border: 1px solid var(--line);
  border-radius: 1rem;
  background: var(--surface);
  box-shadow: 0 12px 36px rgb(25 63 38 / 6%);
}
.field { min-width: 0; }
.field > label {
  display: block;
  margin-bottom: 0.4rem;
  font-size: 0.875rem;
  font-weight: 700;
}
.form-note, .history-list p, .empty-state { color: var(--muted); }
.form-note { margin: 1.25rem 0 0; padding-top: 1rem; border-top: 1px solid var(--line); font-size: 0.875rem; }
@media (max-width: 30rem) {
  .site-header { min-height: 4.25rem; }
  .wordmark { gap: 0.45rem; font-size: 0.875rem; }
  .brand-icon { flex-basis: 1.875rem; align-self: center; font-size: 1.2rem; }
  .nav-link { padding-inline: 0.625rem; font-size: 0.8rem; white-space: nowrap; }
}
""",
    "wordmark.html": """
<span class="wordmark"><span class="brand-icon" aria-hidden="true">↓</span>My Video Downloader</span>
""",
    "history-entries.html": """
{% if history %}
  <ul class="history-list">
    {% for item in history %}
      <li>
        <div>
          <strong>{{ item.title }}</strong>
          <p>{{ item.format }}{% if item.height %} · {{ item.height }}p{% endif %} · {{ item.size }} · {{ item.date }}</p>
        </div>
        <form action="{{ url_for('open_history_item', item_id=item.id) }}" method="post">
          <button type="submit" name="target" value="file">Open file</button>
          <button type="submit" name="target" value="folder">Open folder</button>
        </form>
      </li>
    {% endfor %}
  </ul>
{% elif history_query or history_format %}
  <p class="empty-state">No downloads match your filters.</p>
{% else %}
  <p class="empty-state">No completed downloads yet.</p>
{% endif %}
""",
    "open-saved-download.js": """
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
""",
    "history.html": """
<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Download history</title>
<style>
  {% include 'app-style.css' %}
  .history-filters { display: grid; grid-template-columns: minmax(0, 1.6fr) minmax(0, 1fr) minmax(10.5rem, 1.2fr) auto; align-items: end; gap: 0.75rem; }
  .history-filters .primary { min-height: 3rem; }
  .history-list { list-style: none; margin: 1.5rem 0 0; padding: 0; border-top: 1px solid var(--line); }
  .history-list li { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 0.75rem 1rem; padding: 1rem 0; overflow-wrap: anywhere; }
  .history-list li + li { border-top: 1px solid var(--line); }
  .history-list li > div { flex: 1 1 16rem; min-width: 0; }
  .history-list p { margin: 0.25rem 0 0; font-size: 0.875rem; }
  .history-list form { display: flex; flex-wrap: wrap; gap: 0.5rem; }
  .empty-state { padding: 1.75rem 1rem; margin: 1.5rem 0 0; text-align: center; background: var(--wash); border-radius: 0.625rem; }
  #history-status:empty { margin: 0; }
  @media (max-width: 44rem) { .history-filters { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
  @media (max-width: 32rem) { .history-filters { grid-template-columns: minmax(0, 1fr); } }
</style>
<header class="site-header">
  {% include 'wordmark.html' %}
  <nav><a class="nav-link" href="{{ url_for('index') }}">Back to downloader</a></nav>
</header>
<main class="page-shell">
  <h1 class="page-heading">Download history</h1>
  <section class="panel" aria-label="Search and filter downloads">
  <form class="history-filters" method="get" action="{{ url_for('download_history') }}">
    <div class="field">
      <label for="history-query">Search by title</label>
      <input id="history-query" type="search" name="history_query" value="{{ history_query }}" maxlength="200">
    </div>
    <div class="field">
      <label for="history-format">File format</label>
      <select id="history-format" name="history_format">
        <option value="">All formats</option>
        <option value="MP4" {% if history_format == 'MP4' %}selected{% endif %}>MP4</option>
        <option value="MP3" {% if history_format == 'MP3' %}selected{% endif %}>MP3</option>
        <option value="PHOTOS" {% if history_format == 'PHOTOS' %}selected{% endif %}>Photos</option>
      </select>
    </div>
    <div class="field">
      <label for="history-sort">Sort by</label>
      <select id="history-sort" name="history_sort">
        {% for value, option in history_sorts.items() %}
          <option value="{{ value }}" {% if history_sort == value %}selected{% endif %}>{{ option[0] }}</option>
        {% endfor %}
      </select>
    </div>
    <button class="primary" type="submit">Filter history</button>
  </form>
  <div id="history-entries" data-url="{{ url_for('download_history_entries') }}">
    {% include 'history-entries.html' %}
  </div>
  <p id="history-status" role="status"></p>
  </section>
</main>
<script>
  {% include 'open-saved-download.js' %}
  const entries = document.querySelector('#history-entries');
  const historyStatus = document.querySelector('#history-status');
  entries.addEventListener('submit', event => openSavedDownload(event, historyStatus));

  if (typeof BroadcastChannel === 'function') {
    const updates = new BroadcastChannel('download-history');
    const url = new URL(entries.dataset.url, window.location.href);
    url.search = window.location.search;
    let request;
    updates.addEventListener('message', async () => {
      request?.abort();
      historyStatus.textContent = '';
      const controller = new AbortController();
      request = controller;
      try {
        const response = await fetch(url, {cache: 'no-store', signal: controller.signal});
        if (!response.ok) throw new Error('History refresh failed');
        const html = await response.text();
        if (!controller.signal.aborted) entries.innerHTML = html;
      } catch {
        if (!controller.signal.aborted) historyStatus.textContent = 'Could not refresh download history. Reload to try again.';
      }
    });
  }
</script>
</html>
""",
})

page = r"""
<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>My Video Downloader</title>
<style>
  {% include 'app-style.css' %}
  .form-options {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1.15fr) auto;
    align-items: end;
    gap: 0.75rem;
    margin-top: 1rem;
  }
  .form-options .primary { min-height: 3rem; padding-inline: 1.4rem; }
  #url { min-height: 3.25rem; }
  .url-controls { display: flex; align-items: stretch; gap: 0.5rem; }
  .url-controls input { min-width: 0; }
  #paste-link { flex-shrink: 0; }
  #paste-status { margin: 0.4rem 0 0; color: var(--muted); font-size: 0.875rem; }
  #paste-status:empty { margin: 0; }
  #download-status { margin: 1rem 0 0; padding: 0.75rem 1rem; border-radius: 0.625rem; background: var(--wash); }
  #download-status:empty, #existing-status:empty { margin: 0; padding: 0; }
  progress { width: min(100%, 30rem); height: 1.25rem; }
  .video-preview {
    display: flex;
    flex-wrap: wrap;
    gap: 1rem;
    align-items: center;
    margin: 1.25rem 0 1rem;
    padding: 1rem;
    border: 1px solid var(--line);
    border-radius: 0.75rem;
    background: var(--wash);
  }
  .video-preview img { max-width: 100%; height: auto; border-radius: 0.5rem; }
  .video-preview div { flex: 1 1 12rem; min-width: 0; overflow-wrap: anywhere; }
  .video-preview h2 { font-size: 1.125rem; margin: 0; }
  .video-preview p { margin: 0.375rem 0 0; }
  .photo-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(7rem, 1fr)); gap: 0.75rem; padding: 0; list-style: none; }
  .photo-grid img { display: block; width: 100%; aspect-ratio: 1; object-fit: contain; background: #e4ece4; }
  .photo-grid span { display: block; margin-top: 0.25rem; }
  .photo-grid label { display: block; padding: 0.25rem; border-radius: 0.25rem; cursor: pointer; }
  .photo-grid label:has(:checked) { outline: 2px solid var(--focus); background: #e3efe1; }
  .photo-grid button { margin: 0.375rem 0.25rem 0; }
  .photo-actions { display: flex; flex-wrap: wrap; gap: 0.5rem; margin-top: 0.75rem; }
  #photo-preview-dialog {
    box-sizing: border-box;
    width: min(60rem, calc(100% - 2rem));
    max-height: calc(100% - 2rem);
    padding: 1rem;
    border: 1px solid var(--line);
    border-radius: 1rem;
    color: var(--ink);
  }
  #photo-preview-dialog::backdrop { background: rgb(0 0 0 / 65%); }
  .photo-dialog-header { display: flex; justify-content: space-between; align-items: center; gap: 1rem; }
  .photo-dialog-header h2 { margin: 0; }
  #enlarged-photo { display: block; width: 100%; height: 68vh; object-fit: contain; margin: 0.75rem 0; background: #edf0f4; }
  #post-video-player { display: block; width: 100%; max-height: 68vh; margin: 0.75rem 0; background: #182a22; }
  #enlarged-photo[hidden], #post-video-player[hidden] { display: none; }
  .photo-grid .media-placeholder { display: grid; place-items: center; aspect-ratio: 1; background: #e4ece4; border-radius: 0.5rem; }
  .quality-comparison { margin: 0 0 1rem; min-width: 0; border: 1px solid var(--line); border-radius: 0.75rem; padding: 0.5rem; }
  .quality-comparison legend { font-weight: 650; }
  .quality-comparison table { width: 100%; border-collapse: collapse; }
  .quality-comparison th, .quality-comparison td { text-align: left; padding: 0.5rem 0.375rem; border-bottom: 1px solid var(--line); }
  .quality-comparison tbody tr:last-child td { border-bottom: 0; }
  .quality-comparison label { cursor: pointer; }
  #download-jobs { margin-top: 2rem; }
  #jobs-title { margin-bottom: 0.75rem; }
  .download-job { margin: 0.75rem 0; }
  .download-job h3 { margin: 0 0 0.5rem; font-size: 1.125rem; overflow-wrap: anywhere; }
  .download-job p { margin: 0.375rem 0; }
  .video-preview form { display: inline-block; margin-top: 0.75rem; }
  @media (max-width: 36rem) {
    .form-options { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }
    .form-options .primary { grid-column: 1 / -1; }
    .quality-comparison table { font-size: 0.875rem; overflow-wrap: anywhere; }
  }
  @media (max-width: 32rem) {
    .form-options { grid-template-columns: minmax(0, 1fr); }
  }
</style>
<header class="site-header">
  {% include 'wordmark.html' %}
  <nav><a class="nav-link" href="{{ url_for('download_history') }}" target="_blank" rel="noopener"
          aria-label="Download history (opens in a new tab)">Download history</a></nav>
</header>
<main class="page-shell">
<h1 class="page-heading">Download media</h1>
<div class="panel">
<form id="download-form" method="post" action="{{ url_for('index') }}">
  <div class="field">
    <label for="url">YouTube, Instagram, or Twitter/X link</label>
    <div class="url-controls">
      <input id="url" name="url" type="url" value="{{ link }}" placeholder="https://…" required>
      <button id="paste-link" type="button" hidden>Paste link</button>
    </div>
    <p id="paste-status" role="status"></p>
  </div>
  <div class="form-options">
    <div class="field">
      <label for="quality">Video quality</label>
      <select id="quality" name="quality" {% if best_only %}disabled{% endif %}>
        {% for value, label in qualities.items() %}
          <option value="{{ value }}" {% if value == quality %}selected{% endif %}>{{ label }}</option>
        {% endfor %}
      </select>
    </div>
    <div class="field">
      <label for="format">Format</label>
      <select id="format" name="format">
        <option value="mp4">MP4 video with sound</option>
        <option value="mp3" {% if media_type == 'mp3' %}selected{% endif %}>MP3 audio only</option>
        <option value="image" {% if media_type == 'image' %}selected{% endif %}>Photo (Instagram or X)</option>
        <option value="mixed" {% if media_type == 'mixed' %}selected{% endif %}>Photos and videos</option>
      </select>
    </div>
    <button class="primary" type="submit" name="action" value="check">{{ 'Preview post' if media_type == 'mixed' else 'Check size' }}</button>
  </div>
  <p class="form-note">Files are saved to your Downloads folder.</p>
  <div id="checked-result">
  {% if checked %}
    <div id="checked-video">
      <article class="video-preview" aria-labelledby="video-title">
        {% if media_type != 'mixed' and preview.thumbnail and preview.photos | length <= 1 %}
        <img src="{{ preview.thumbnail }}" alt="" width="192" height="144" referrerpolicy="no-referrer">
        {% elif not best_only %}
        <img src="https://i.ytimg.com/vi/{{ preview.id }}/hqdefault.jpg" alt=""
             width="192" height="144" referrerpolicy="no-referrer">
        {% endif %}
        <div>
          <h2 id="video-title">{{ preview.title }}</h2>
          {% if media_type in ('mp4', 'mp3') %}<p>Duration: {{ preview.duration }}</p>{% endif %}
          {% if media_type == 'mixed' or preview.photos | length > 1 %}
          {% set mixed = media_type == 'mixed' %}
          {% set items = preview.media_items if mixed else preview.photos %}
          <p>{{ items | length }} {{ ('item' if items | length == 1 else 'items') if mixed else 'photos' }}</p>
          <div class="photo-actions">
            <button type="button" data-photo-selection="all" hidden>Select all</button>
            <button type="button" data-photo-selection="clear" hidden>Clear selection</button>
            <button class="primary" type="submit" name="action" value="download-selected">Download selected</button>
            <button type="submit" name="action" value="download">{{ 'Download all items' if mixed else 'Download all photos' }}</button>
          </div>
          <p id="photo-selection-status" role="status" hidden></p>
          <ul class="photo-grid">
            {% for item in items %}
            {% set video_item = item.kind == 'video' %}
            {% set item_label = ('Video' if video_item else 'Photo') ~ ' · Slide ' ~ item.position if mixed else 'Slide ' ~ item.position %}
            <li>
              <label>
                {% if not video_item or item.thumbnail %}
                  <img src="{{ item.thumbnail if video_item else item.url }}" alt="" width="160" height="160" loading="lazy" referrerpolicy="no-referrer">
                {% else %}
                  <span class="media-placeholder" aria-hidden="true">No thumbnail</span>
                {% endif %}
                <span><input type="checkbox" name="{{ 'media_position' if mixed else 'photo_position' }}" value="{{ item.position }}"> {{ item_label }}</span>
              </label>
              {% if video_item and item.duration != 'Unavailable' %}<small>{{ item.duration }}</small>{% endif %}
              {% if not video_item or item.playback %}
                <button type="button" data-enlarge-photo data-title="{{ item_label }}"
                        {% if video_item %}data-playback="{{ item.playback }}" aria-label="Preview video on slide {{ item.position }}"{% else %}aria-label="Enlarge slide {{ item.position }}"{% endif %}
                        hidden>{{ 'Preview video' if video_item else 'Enlarge' }}</button>
              {% endif %}
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
      {% if media_type != 'mixed' and preview.photos | length <= 1 %}
      <button class="primary" type="submit" name="action" value="download">{{ 'Download image' if media_type == 'image' else 'Download' }}</button>
      {% endif %}
    </div>
  {% endif %}
  </div>
</form>
<dialog id="photo-preview-dialog" aria-labelledby="enlarged-photo-title">
  <div class="photo-dialog-header">
    <h2 id="enlarged-photo-title"></h2>
    <form method="dialog">
      <button autofocus>Close</button>
    </form>
  </div>
  <img id="enlarged-photo" alt="Enlarged photo" referrerpolicy="no-referrer">
  <video id="post-video-player" controls preload="none" hidden></video>
  <label><input type="checkbox" id="enlarged-photo-selection"> <span id="enlarged-selection-label">Select this photo</span></label>
</dialog>
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
</div>
<section id="download-jobs" aria-labelledby="jobs-title" hidden>
  <h2 id="jobs-title">Downloads</h2>
  <div id="download-job-list"></div>
</section>
</main>
<template id="download-job-template">
  <article class="download-job panel">
    <h3></h3>
    <p class="job-status" role="status"></p>
    <div class="job-progress">
      <progress aria-label="Download progress" max="100"></progress>
      <p class="job-details"></p>
      <button class="cancel-download" type="button" hidden>Cancel download</button>
    </div>
  </article>
</template>
<script>
  const form = document.querySelector('#download-form');
  const urlField = form.elements.url;
  const pasteLink = document.querySelector('#paste-link');
  const pasteStatus = document.querySelector('#paste-status');
  const previewButton = form.querySelector('button[value="check"]');
  const checkStatus = document.querySelector('#download-status');
  const checkedResult = document.querySelector('#checked-result');
  const photoDialog = document.querySelector('#photo-preview-dialog');
  const enlargedPhotoTitle = document.querySelector('#enlarged-photo-title');
  const enlargedPhoto = document.querySelector('#enlarged-photo');
  const postVideoPlayer = document.querySelector('#post-video-player');
  const enlargedPhotoSelection = document.querySelector('#enlarged-photo-selection');
  const enlargedSelectionLabel = document.querySelector('#enlarged-selection-label');
  const itemCheckbox = 'input:is([name="photo_position"], [name="media_position"])';
  let enlargedPhotoInput;
  const existingResult = document.querySelector('#existing-result');
  const jobs = document.querySelector('#download-jobs');
  const jobList = document.querySelector('#download-job-list');
  const jobTemplate = document.querySelector('#download-job-template');
  const historyUpdates = typeof BroadcastChannel === 'function' ? new BroadcastChannel('download-history') : null;
  const size = bytes => `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
  let youtubeQuality = form.elements.quality.disabled ? '720' : form.elements.quality.value;
  let previewRequest;
  {% include 'open-saved-download.js' %}
  existingResult.addEventListener('submit', event => {
    openSavedDownload(event, document.querySelector('#existing-status'));
  });

  pasteLink.hidden = false;
  pasteLink.addEventListener('click', async () => {
    const previousValue = urlField.value;
    pasteStatus.textContent = '';
    pasteLink.disabled = true;
    let copiedText;
    try {
      copiedText = (await navigator.clipboard.readText()).trim();
    } catch {
      pasteStatus.textContent = 'Clipboard unavailable. Press Ctrl+V in the link box to paste.';
      urlField.focus();
      return;
    } finally {
      pasteLink.disabled = false;
    }
    if (urlField.value !== previousValue) return;
    urlField.focus();
    if (!copiedText) {
      pasteStatus.textContent = 'Your clipboard is empty. Copy a link first.';
      return;
    }
    try {
      if (new URL(copiedText).protocol !== 'https:') {
        pasteStatus.textContent = 'Copy an HTTPS link and try again.';
        return;
      }
    } catch {
      pasteStatus.textContent = 'Copy a link and try again.';
      return;
    }
    if (copiedText !== previousValue) {
      urlField.value = copiedText;
      urlField.dispatchEvent(new Event('input', {bubbles: true}));
    }
    pasteStatus.textContent = 'Link pasted.';
  });

  function updatePhotoSelection() {
    const feedback = checkedResult.querySelector('#photo-selection-status');
    if (!feedback) return;
    const inputs = checkedResult.querySelectorAll(itemCheckbox);
    const count = checkedResult.querySelectorAll(`${itemCheckbox}:checked`).length;
    const submit = checkedResult.querySelector('button[value="download-selected"]');
    submit.disabled = count === 0 || submit.getAttribute('aria-busy') === 'true';
    feedback.hidden = false;
    feedback.textContent = `${count} of ${inputs.length} selected`;
    checkedResult.querySelectorAll('button[data-photo-selection]').forEach(button => { button.hidden = false; });
    if (typeof photoDialog.showModal === 'function') {
      checkedResult.querySelectorAll('button[data-enlarge-photo]').forEach(button => { button.hidden = false; });
    }
  }

  checkedResult.addEventListener('click', event => {
    const enlargeButton = event.target.closest('button[data-enlarge-photo]');
    if (enlargeButton) {
      const tile = enlargeButton.closest('li');
      enlargedPhotoInput = tile.querySelector(itemCheckbox);
      enlargedPhotoTitle.textContent = enlargeButton.dataset.title;
      const playback = enlargeButton.dataset.playback;
      enlargedPhoto.hidden = Boolean(playback);
      postVideoPlayer.hidden = !playback;
      enlargedSelectionLabel.textContent = playback ? 'Select this video' : 'Select this photo';
      if (playback) postVideoPlayer.src = playback;
      else enlargedPhoto.src = tile.querySelector('img').src;
      enlargedPhotoSelection.checked = enlargedPhotoInput.checked;
      photoDialog.showModal();
      return;
    }
    const button = event.target.closest('button[data-photo-selection]');
    if (!button) return;
    checkedResult.querySelectorAll(itemCheckbox).forEach(input => {
      input.checked = button.dataset.photoSelection === 'all';
    });
    updatePhotoSelection();
  });

  enlargedPhotoSelection.addEventListener('input', () => {
    enlargedPhotoInput.checked = enlargedPhotoSelection.checked;
    updatePhotoSelection();
  });
  photoDialog.addEventListener('close', () => {
    enlargedPhoto.removeAttribute('src');
    if (postVideoPlayer.hasAttribute('src')) {
      postVideoPlayer.pause();
      postVideoPlayer.removeAttribute('src');
      postVideoPlayer.load();
    }
    enlargedPhotoInput = undefined;
  });

  updatePhotoSelection();
  form.addEventListener('input', event => {
    if (event.target === urlField) pasteStatus.textContent = '';
    if (event.target.name === 'format') {
      previewButton.textContent = event.target.value === 'mixed' ? 'Preview post' : 'Check size';
    }
    if (event.target.matches(itemCheckbox)) {
      updatePhotoSelection();
      return;
    }
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
    const mixed = data.get('format') === 'mixed';
    checkStatus.textContent = mixed ? 'Loading post…' : 'Checking size…';
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
      updatePhotoSelection();
      existingResult.replaceChildren(...page.querySelector('#existing-result').childNodes);
      checkStatus.textContent = page.querySelector('#download-status').textContent;
    } catch {
      if (!controller.signal.aborted) {
        checkStatus.textContent = mixed ? 'Could not preview the post. Try again.' : 'Could not check the size. Try again.';
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
      if (event.item_count) details.textContent = `${event.item_count} ${event.item_count === 1 ? 'item' : 'items'} ready`;
      else details.textContent = event.photo_count ? `${event.photo_count} photos saved` : '100%';
      return;
    }
    if (event.state === 'error' || event.state === 'cancelled') progress.hidden = true;
    if (event.item_count) {
      const itemProgress = event.total && event.downloaded != null ? Math.min(1, event.downloaded / event.total) : 0;
      bar.value = (event.items_saved + itemProgress) / event.item_count * 100;
      details.textContent = `${event.items_saved} of ${event.item_count} items ready`;
      return;
    }
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
    if (submitter) {
      submitter.disabled = true;
      submitter.setAttribute('aria-busy', 'true');
    }
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
          if (update.history_saved) historyUpdates?.postMessage('saved');
        }
      }
      if (!finished) throw new Error('Download connection closed');
    } catch {
      controller.abort();
      showProgress(job, {state: 'error', message: 'Download interrupted. Check the PowerShell window.'});
    } finally {
      cancelUrl = null;
      cancel.hidden = true;
      if (submitter) {
        submitter.disabled = false;
        submitter.removeAttribute('aria-busy');
        updatePhotoSelection();
      }
    }
  }

  form.addEventListener('submit', event => {
    const action = event.submitter?.value || 'check';
    const downloading = action === 'download' || action === 'download-selected';
    if (downloading && (!window.ReadableStream || !window.TextDecoder)) return;
    event.preventDefault();
    const data = new FormData(form);
    data.set('action', action);
    if (downloading) startDownload(data, event.submitter);
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
    video_position: str = ""


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
    if parts.hostname in {
        "x.com", "www.x.com", "m.x.com", "mobile.x.com",
        "twitter.com", "www.twitter.com", "m.twitter.com", "mobile.twitter.com",
    }:
        post = re.fullmatch(
            r"/(?:i/web|[A-Za-z0-9_]{1,15})/status/([1-9][0-9]{0,19})(?:/(photo|video)/([1-9][0-9]{0,2}))?/?",
            parts.path,
        )
        if post and parts.netloc.lower() in {parts.hostname, f"{parts.hostname}:443"}:
            key, kind, position = post.groups()
            url = f"https://x.com/i/web/status/{key}"
            if kind == "video":
                return VideoSource("Twitter/X", key, f"{url}/video/{position}", position)
            return VideoSource("Twitter/X", key, url)
        return None
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


def media_cdn_url(platform, value):
    if platform == "Instagram":
        return instagram_cdn_url(value)
    if platform != "Twitter/X" or not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme == "https" and parts.username is None and port in (None, 443):
        if parts.hostname == "pbs.twimg.com" or parts.hostname == "video.twimg.com":
            return value
    return None


class MediaCDNRedirect(HTTPRedirectHandler):
    def __init__(self, platform):
        self.platform = platform

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not media_cdn_url(self.platform, newurl):
            return None
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            redirected.method = req.get_method()
        return redirected


def media_track_size(platform, track, *, photo=False):
    url = media_cdn_url(platform, track.get("url"))
    if not url or track.get("fragments") or track.get("protocol") not in (None, "http", "https"):
        return None

    media_headers = track.get("http_headers") or {}
    headers = {name: value for name, value in media_headers.items() if name.lower() in {"user-agent", "referer"}}
    if photo and platform == "Twitter/X":
        headers.update(twitter_photo_headers)
    headers["Accept-Encoding"] = "identity"
    opener = build_opener(MediaCDNRedirect(platform))
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


def history_rows(item_id=None, *, query="", media_format="", filename_suffix="", sort="newest"):
    if not history_database.is_file():
        return []
    with closing(sqlite3.connect(history_database)) as connection:
        connection.row_factory = sqlite3.Row
        if item_id is not None:
            return connection.execute("SELECT path FROM downloads WHERE id = ?", (item_id,)).fetchall()
        connection.create_function("casefold", 1, str.casefold)
        query = query.casefold()
        order_by = history_sorts.get(sort, history_sorts["newest"])[1]
        return connection.execute(f"""
            SELECT id, path, title, format, height, size, downloaded_at FROM downloads
            WHERE (? = '' OR instr(casefold(title), ?) > 0)
              AND (? = '' OR format = ? OR (? = 'PHOTOS' AND format IN ('JPG', 'PNG', 'WEBP')))
              AND (? = '' OR substr(path, -length(?)) = ?)
            ORDER BY {order_by} LIMIT 50
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


def recent_history(query, media_format, sort="newest"):
    try:
        return [
            {
                **dict(item), "size": format_mib(item["size"]),
                "date": datetime.fromisoformat(item["downloaded_at"]).astimezone().strftime("%d %b %Y, %H:%M"),
            }
            for item in history_rows(query=query, media_format=media_format, sort=sort)
        ]
    except (OSError, sqlite3.Error, ValueError) as error:
        print(f"Could not read download history: {error}", flush=True)
        return []


def history_context():
    query = request.args.get("history_query", "").strip()[:200]
    media_format = request.args.get("history_format", "").upper()
    if media_format not in {"MP4", "MP3", "PHOTOS"}:
        media_format = ""
    sort = request.args.get("history_sort", "newest")
    if sort not in history_sorts:
        sort = "newest"
    return {
        "history": recent_history(query, media_format, sort),
        "history_query": query,
        "history_format": media_format,
        "history_sort": sort,
        "history_sorts": history_sorts,
    }


@app.get("/history")
def download_history():
    return render_template("history.html", **history_context())


@app.get("/history/entries")
def download_history_entries():
    return render_template("history-entries.html", **history_context())


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


def download_options(platform, media_type, quality="best", *, playlist_position=1):
    if media_type == "image":
        return ["--ignore-no-formats-error"]
    options = ["--playlist-items", str(playlist_position)] if platform != "YouTube" else []
    if platform == "Twitter/X":
        options += ["--ies", "twitter,twitter:amplify,twitter:card"]
    if media_type == "mp3":
        return [*options, "-x", "--audio-format", "mp3"]
    if platform == "Instagram":
        return [*options, "-f", "b[ext=mp4]/bv[ext=mp4]+ba[ext=m4a]/bv[ext=mp4]", "--merge-output-format", "mp4"]
    options += ["-t", "mp4"]
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
            job.cancelled = job.process is None or stop_process_tree(job.process) or job.batch
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
    if platform == "Twitter/X" and any(reason in output for reason in (
        "login required", "only available to registered", "protected tweets", "not authorized",
        "this tweet is unavailable",
    )):
        return 403, "Twitter/X did not make this post available. Try a public post that opens without signing in."
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


def instagram_entries(video):
    entries = (video.get("entries") or []) if video.get("_type") == "playlist" else [video]
    for index, entry in enumerate(entries, 1):
        if isinstance(entry, dict):
            position = entry.get("playlist_index")
            yield (position if isinstance(position, int) and position > 0 else index), entry


def instagram_photos(video, key):
    photos = []
    for position, entry in instagram_entries(video):
        if entry.get("formats") or entry.get("url") or entry.get("duration"):
            continue
        thumbnail = instagram_cdn_url(entry.get("thumbnail"))
        if thumbnail:
            suffix = "" if position == 1 else f"-{position:02d}"
            photos.append({"position": position, "url": thumbnail, "key": f"instagram-{key}{suffix}"})
    return photos


def instagram_post_preview(video, key):
    items = [{**photo, "kind": "photo"} for photo in instagram_photos(video, key)]
    for position, entry in instagram_entries(video):
        if not (entry.get("formats") or entry.get("url")):
            continue
        playback = None
        for candidate in reversed(entry.get("formats") or [entry]):
            if candidate.get("ext") != "mp4" or candidate.get("fragments") or candidate.get("protocol") not in (None, "http", "https"):
                continue
            if candidate.get("vcodec") != "none":
                url = instagram_cdn_url(candidate.get("url"))
                if url and (not playback or candidate.get("acodec") != "none"):
                    playback = url
                if url and candidate.get("acodec") != "none":
                    break
        video_key = f"instagram-{key}-video-{position}" if video.get("_type") == "playlist" else f"instagram-{key}"
        items.append({
            "kind": "video", "position": position, "key": video_key,
            "thumbnail": instagram_cdn_url(entry.get("thumbnail")),
            "playback": playback, "duration": video_duration(entry),
        })
    if not items:
        raise PreviewRequestError("No downloadable items were found in this Instagram post.")
    return {"title": video.get("title") or "Instagram post", "media_items": sorted(items, key=lambda item: item["position"])}


def twitter_post_preview(key, *, mixed=False):
    try:
        from yt_dlp.jsinterp import js_number_to_string
    except ImportError as error:
        raise PreviewRequestError(
            'Twitter/X previews require the yt-dlp Python package. In PowerShell, run py -m pip install -U "yt-dlp[default]".'
        ) from error

    # X's public embed endpoint uses a token calculated from the post ID.
    token = js_number_to_string((int(key) / 1e15) * pi, 36).replace(".", "").replace("0", "")
    query = urlencode({"id": key, "token": token, "lang": "en"})
    opener = build_opener(MediaCDNRedirect("Twitter/X"))
    try:
        with opener.open(f"https://cdn.syndication.twimg.com/tweet-result?{query}", timeout=10) as response:
            post = json.loads(response.read(2 * 1024 * 1024))
    except HTTPError as error:
        error.close()
        if error.code == 429:
            pause_platform_requests("Twitter/X")
            raise PreviewRequestError("Twitter/X is rate-limiting requests. Wait a while before trying again.", 429) from error
        if error.code in {401, 403, 404}:
            raise PreviewRequestError("Twitter/X did not make this post available to public previews.", error.code) from error
        raise

    if not isinstance(post, dict) or not post or post.get("tombstone"):
        raise PreviewRequestError("Twitter/X did not make this post available to public previews.")
    items = []
    media_details = post.get("mediaDetails")
    if not isinstance(media_details, list):
        media_details = []
    for position, media in enumerate(media_details, 1):
        if not isinstance(media, dict):
            continue
        kind = media.get("type")
        url = media_cdn_url("Twitter/X", media.get("media_url_https"))
        if kind == "photo" and url:
            parts = urlsplit(url)
            if parts.hostname == "pbs.twimg.com" and parts.path.startswith("/media/"):
                suffix = "" if position == 1 else f"-{position:02d}"
                items.append({"kind": "photo", "position": position, "url": url, "key": f"twitter-{key}{suffix}"})
        elif mixed and kind == "video":
            info = media.get("video_info")
            if not isinstance(info, dict):
                info = {}
            playback = None
            bitrate = -1
            for variant in info.get("variants") or []:
                if isinstance(variant, dict) and variant.get("content_type") == "video/mp4":
                    candidate = media_cdn_url("Twitter/X", variant.get("url"))
                    rate = variant.get("bitrate") or 0
                    if candidate and urlsplit(candidate).hostname == "video.twimg.com" and isinstance(rate, (int, float)) and rate > bitrate:
                        playback, bitrate = candidate, rate
            duration = info.get("duration_millis")
            items.append({
                "kind": "video", "position": position, "key": f"twitter-{key}-video-{position}",
                "thumbnail": url, "playback": playback,
                "duration": video_duration({"duration": duration / 1000 if isinstance(duration, (int, float)) else None}),
            })
    if not items:
        if mixed:
            raise PreviewRequestError("No downloadable items were found in this Twitter/X post.")
        raise PreviewRequestError("No photos were found in this Twitter/X post. For a video, choose MP4 video with sound.")
    user = post.get("user")
    author = user.get("screen_name") if isinstance(user, dict) else None
    title = f"Post by @{author}" if author else "Twitter/X post"
    if mixed:
        return {"title": title, "media_items": items}
    return {"id": key, "title": title, "photos": items, "thumbnail": items[0]["url"]}


def lookup_preview(key, media_type, url):
    source = video_source_from(url)
    platform = source.platform if source else "YouTube"
    cache_key = (platform, key, media_type, source.video_position if source and media_type in {"mp4", "mp3"} else "")
    with preview_cache_lock:
        # Serialize metadata lookups so simultaneous checks can share the same result.
        cached = preview_cache.get(cache_key)
        if cached and cached[0] > monotonic():
            preview_cache.move_to_end(cache_key)
            return cached[1]
        pause = platform_pause_message(platform)
        if pause:
            raise PreviewRequestError(pause, 429)

        result = None
        failure = "Could not preview the post." if media_type == "mixed" else "Could not check the size."
        try:
            if platform == "Twitter/X" and media_type in {"image", "mixed"}:
                preview = twitter_post_preview(key, mixed=media_type == "mixed")
            else:
                options = download_options(platform, "image" if media_type == "mixed" else media_type)
                result = subprocess.run(
                    [*yt_dlp_command, *options, "--skip-download", "--dump-single-json", "--", url],
                    check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    encoding="utf-8", errors="replace", timeout=30,
                )
                video = json.loads(result.stdout)
                if platform != "YouTube" and media_type in {"mp4", "mp3"} and video.get("_type") == "playlist":
                    video = next((entry for entry in (video.get("entries") or []) if isinstance(entry, dict)), None)
                    if video is None:
                        raise PreviewRequestError(f"No video was found in this {platform} post.")
                fallback_title = "Instagram photo" if media_type == "image" else f"{platform} video"
                preview = {"id": key, "title": video.get("title") or fallback_title, "duration": video_duration(video)}
                if platform != "YouTube":
                    preview["thumbnail"] = media_cdn_url(platform, video.get("thumbnail"))
                if media_type == "mixed":
                    preview = instagram_post_preview(video, key)
                elif media_type == "image":
                    photos = instagram_photos(video, key)
                    if not photos:
                        raise PreviewRequestError("No downloadable photos were found in this Instagram post.")
                    preview["photos"] = photos
                    preview["thumbnail"] = photos[0]["url"]
                    if preview["title"].startswith("Video by "):
                        preview["title"] = "Photo by " + preview["title"].removeprefix("Video by ")
        except subprocess.TimeoutExpired as error:
            message = "The post preview timed out." if media_type == "mixed" else "The size check timed out."
            raise PreviewRequestError(f"{message} Try again.") from error
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
            if platform == "Instagram" and media_type in {"mp4", "mp3"} and "there is no video in this post" in output.lower():
                raise PreviewRequestError("Choose Photo (Instagram or X) and click Check size to see the photo.") from error
            if platform == "Twitter/X" and any(reason in output.lower() for reason in (
                "no video could be found", "no suitable extractor", "is not a video",
            )):
                raise PreviewRequestError("No supported video was found in this Twitter/X post. For photos, choose Photo (Instagram or X).") from error
            raise PreviewRequestError(f"{failure} Check the PowerShell window.") from error
        except (OSError, ValueError, HTTPException) as error:
            raise PreviewRequestError(f"{failure} Check the PowerShell window.") from error

        if result and result.stderr:
            print(result.stderr, end="", file=sys.stderr, flush=True)
        comparisons = []
        message = ""
        if media_type == "mixed":
            sizes = {"best": ""}
        elif media_type == "image":
            photos = preview["photos"]
            length = media_track_size(platform, photos[0], photo=True) if len(photos) == 1 else None
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
            if amount is None and platform != "YouTube" and media_type == "mp4":
                for track in video.get("requested_formats") or [video]:
                    if not track.get("filesize") and not track.get("filesize_approx"):
                        length = media_track_size(platform, track)
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


def save_photo(platform, photo, opener, job, show_bytes):
    url = media_cdn_url(platform, photo["url"])
    if not url:
        raise ValueError("No photo URL is available")
    with tempfile.TemporaryDirectory(dir=downloads) as folder:
        partial = Path(folder) / "photo.part"
        headers = {"Accept-Encoding": "identity"}
        if platform == "Twitter/X":
            headers.update(twitter_photo_headers)
        request = Request(url, headers=headers)
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
            label = "Instagram" if platform == "Instagram" else "Twitter"
            path = downloads / f"{label} photo [{photo['key']}]{extension}"
            partial.replace(path)
    return path


def photo_download_events(source, download_id, cancel_url, selected_positions=None):
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
        is_carousel = len(photos) > 1
        if selected_positions is not None:
            positions = set(selected_positions)
            available = {str(photo["position"]) for photo in photos}
            if not positions or not positions <= available:
                yield download_event("error", "Select at least one available photo from this post.", http_status=400)
                return
            photos = [photo for photo in photos if str(photo["position"]) in positions]
        count = len(photos)
        saved = 0
        history_saved = False
        history_error = False
        rate_limited = False
        downloads.mkdir(exist_ok=True)
        opener = build_opener(MediaCDNRedirect(source.platform))
        for photo in photos:
            if job.cancelled:
                break
            if count > 1:
                yield download_event("downloading", f"Saving photo from slide {photo['position']}…", photos_saved=saved, photo_count=count)
            if find_existing_download(photo["key"], "image"):
                saved += 1
                continue
            try:
                path = yield from save_photo(source.platform, photo, opener, job, show_bytes=count == 1)
            except HTTPError as error:
                error.close()
                if error.code == 429:
                    pause_platform_requests(source.platform)
                    rate_limited = True
                    break
                print(f"{source.platform} photo from slide {photo['position']} failed: HTTP {error.code}", flush=True)
                continue
            except (OSError, ValueError, HTTPException) as error:
                print(f"Could not save {source.platform} photo from slide {photo['position']}: {error}", flush=True)
                continue
            if path is None:
                break

            saved += 1
            title = f"{preview['title']} · Slide {photo['position']}" if is_carousel else preview["title"]
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
            message = f"{source.platform} is rate-limiting requests. Try again later."
            if count > 1:
                message = f"{summary} {message}"
            yield download_event("error", message, http_status=429, history_saved=history_saved)
        elif saved < count:
            message = f"{summary} Try again for the remaining photos." if count > 1 else f"Could not save the {source.platform} photo."
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
            yield download_event("error", f"Could not save the {source.platform} photo.")
    finally:
        with active_downloads_lock:
            active_downloads.pop(download_id, None)
            active_video_ids.remove(key)


def download_events(command, url, download_id, cancel_url, *, job=None):
    event = download_event

    source = video_source_from(url)
    platform = source.platform if source else "YouTube"
    pause = platform_pause_message(platform)
    if pause:
        yield event("error", pause, http_status=429)
        return
    key = (platform, source.key) if source else url
    owns_job = job is None
    if owns_job:
        if not reserve_download(key):
            yield event("error", "This video is already downloading.", http_status=409)
            return
        job = ActiveDownload()
        with active_downloads_lock:
            active_downloads[download_id] = job
    finished_video = None
    restriction = None
    try:
        if job.cancelled:
            yield event("cancelled", "Download cancelled.")
            return
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
            with active_downloads_lock:
                job.process = process
            try:
                if job.cancelled:
                    stop_process_tree(process)
                if owns_job:
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
                    job.process = None
                if process.poll() is None:
                    try:
                        stop_process_tree(process)
                    except (OSError, subprocess.SubprocessError):
                        if process.poll() is None:
                            process.kill()
        if job.cancelled and (returncode != 0 or not isinstance(finished_video, dict)):
            yield event("cancelled", "Download cancelled.")
        elif returncode == 0:
            if job.batch:
                try:
                    path = local_download_path(finished_video.get("filepath") or "") if isinstance(finished_video, dict) else None
                    if path is None or not path.is_file():
                        raise ValueError("yt-dlp did not save the selected file")
                except (OSError, ValueError) as error:
                    print(error, flush=True)
                    yield event("error", download_failed)
                    return
            try:
                if not isinstance(finished_video, dict):
                    raise ValueError("yt-dlp did not report the final file")
                record_download(finished_video)
            except (OSError, sqlite3.Error, ValueError) as error:
                print(f"Could not save download history: {error}", flush=True)
                yield event("complete", f"{download_complete} History could not be saved.", history_error=True)
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
        if owns_job:
            with active_downloads_lock:
                active_downloads.pop(download_id, None)
                active_video_ids.remove(key)


def mixed_item_events(source, item, title, download_id, cancel_url, job, opener):
    if item["kind"] == "video":
        url = source.url
        position = item["position"]
        if source.platform == "Twitter/X":
            url = f"https://x.com/i/web/status/{source.key}/video/{position}"
            position = 1
        options = download_options(source.platform, "mp4", playlist_position=position)
        command = [*yt_dlp_command, *options, "-o", f"%(title)s [{item['key']}].%(ext)s"]
        yield from download_events(command, url, download_id, cancel_url, job=job)
        return

    try:
        path = yield from save_photo(source.platform, item, opener, job, show_bytes=True)
    except HTTPError as error:
        error.close()
        if error.code == 429:
            pause_platform_requests(source.platform)
        yield download_event("error", f"Could not save photo on slide {item['position']}.", http_status=error.code)
        return
    except (OSError, ValueError, HTTPException) as error:
        print(f"Could not save photo on slide {item['position']}: {error}", flush=True)
        yield download_event("error", f"Could not save photo on slide {item['position']}.")
        return
    if path is None:
        yield download_event("cancelled", "Download cancelled.")
        return
    try:
        record_download({"filepath": str(path), "title": f"{title} · Slide {item['position']}"})
    except (OSError, sqlite3.Error, ValueError) as error:
        print(f"Could not save download history: {error}", flush=True)
        yield download_event("complete", download_complete, history_error=True)
    else:
        yield download_event("complete", download_complete, history_saved=True)


def mixed_download_events(source, download_id, cancel_url, selected_positions):
    pause = platform_pause_message(source.platform)
    if pause:
        yield download_event("error", pause, http_status=429)
        return
    key = (source.platform, source.key)
    if not reserve_download(key):
        yield download_event("error", "This post is already downloading.", http_status=409)
        return

    job = ActiveDownload(batch=True)
    try:
        with active_downloads_lock:
            active_downloads[download_id] = job
        yield download_event("starting", "Starting download…", cancel_url=cancel_url)
        preview, _, _, _ = lookup_preview(source.key, "mixed", source.url)
        items = preview["media_items"]
        if selected_positions is not None:
            positions = set(selected_positions)
            if not positions or not positions <= {str(item["position"]) for item in items}:
                yield download_event("error", "Select at least one available item from this post.", http_status=400)
                return
            items = [item for item in items if str(item["position"]) in positions]

        saved = 0
        count = len(items)
        history_error = False
        failures = []
        rate_limited = False
        downloads.mkdir(exist_ok=True)
        opener = build_opener(MediaCDNRedirect(source.platform))
        for item in items:
            if job.cancelled:
                break
            kind = item["kind"]
            position = item["position"]
            if find_existing_download(item["key"], "image" if kind == "photo" else "mp4"):
                saved += 1
                continue
            message = f"Saving {kind} on slide {position}…"
            yield download_event("downloading", message, item_count=count, items_saved=saved)
            with closing(mixed_item_events(source, item, preview["title"], download_id, cancel_url, job, opener)) as events:
                for raw in events:
                    update = json.loads(raw)
                    state = update["state"]
                    if state == "complete":
                        saved += 1
                        history_error |= update.get("history_error", False)
                        yield download_event(
                            "downloading", message, item_count=count, items_saved=saved,
                            history_saved=update.get("history_saved", False),
                        )
                    elif state == "error":
                        failures.append(f"Slide {position}: {update['message']}")
                        rate_limited = update.get("http_status") == 429
                    elif state in {"downloading", "processing"}:
                        yield download_event(
                            state, message, item_count=count, items_saved=saved,
                            downloaded=update.get("downloaded"), total=update.get("total"),
                        )
            if rate_limited:
                break

        summary = f"{saved} of {count} selected items ready."
        if history_error:
            summary += " Some download history could not be saved."
        if job.cancelled:
            yield download_event("cancelled", f"Download cancelled. {summary}")
        elif failures:
            yield download_event("error", f"{summary} {' '.join(failures)}", http_status=429 if rate_limited else 200)
        else:
            yield download_event("complete", f"{summary} Open your Downloads folder.", item_count=count)
    except PreviewRequestError as error:
        yield download_event("error", str(error), http_status=error.http_status)
    except OSError as error:
        print(f"Mixed post download failed: {error}", flush=True)
        yield download_event("error", download_failed)
    finally:
        with active_downloads_lock:
            active_downloads.pop(download_id, None)
            active_video_ids.remove(key)


@app.route("/", methods=["GET", "POST"])
def index():
    link = request.form.get("url", "").strip()
    quality = request.form.get("quality", "720")
    media_type = request.form.get("format", "mp4")
    if media_type not in {"mp4", "mp3", "image", "mixed"}:
        media_type = "mp4"
    action = request.form.get("action")
    downloading = action in {"download", "download-selected"}
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
    best_only = False
    http_status = 200
    streaming = request.headers.get("Accept") == progress_type
    if request.method == "POST":
        source = video_source_from(link)
        if not source:
            message = "Paste a YouTube video, Instagram post or Reel, or Twitter/X post link."
        elif media_type in {"image", "mixed"} and source.platform == "YouTube":
            message = "Choose a video or audio format for YouTube links."
        elif media_type == "image" and source.platform == "Instagram" and not source.url.startswith("https://www.instagram.com/p/"):
            message = "Paste a public Instagram photo post link."
        elif action == "download-selected" and media_type not in {"image", "mixed"}:
            message, http_status = "Choose a photo or mixed post format to select individual items.", 400
        else:
            best_only = source.platform != "YouTube"
            if best_only:
                quality = "best"
            options = download_options(source.platform, media_type, quality)
            history_key = source.key
            if best_only:
                prefix = "instagram" if source.platform == "Instagram" else "twitter"
                history_key = f"{prefix}-{source.key}"
                if source.video_position and media_type in {"mp4", "mp3"}:
                    history_key += f"-video-{source.video_position}"
                options += ["-o", f"%(title)s [{history_key}].%(ext)s"]
            command = [*yt_dlp_command, *options]
            selection = f"{history_key}:{quality}:{media_type}"
            if downloading and selection in request.form.getlist("checked"):
                download_id = uuid4().hex
                cancel_url = url_for("cancel_download", download_id=download_id)
                if media_type == "image":
                    positions = request.form.getlist("photo_position") if action == "download-selected" else None
                    events = photo_download_events(source, download_id, cancel_url, positions)
                elif media_type == "mixed":
                    positions = request.form.getlist("media_position") if action == "download-selected" else None
                    events = mixed_download_events(source, download_id, cancel_url, positions)
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
                if media_type != "mixed":
                    existing = find_existing_download(history_key, media_type)
                if media_type in {"image", "mixed"} or not existing or action == "check-online":
                    try:
                        preview, sizes, comparisons, message = lookup_preview(source.key, media_type, source.url)
                    except PreviewRequestError as error:
                        http_status, message = error.http_status, str(error)
                    else:
                        if media_type == "mp4" and not best_only:
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
        comparisons=comparisons, best_only=best_only,
    ), http_status
