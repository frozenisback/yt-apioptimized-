from flask import Flask, request, jsonify, send_file
import yt_dlp
import os
import uuid
import requests
import hashlib
import glob
import shutil
import urllib.parse

app = Flask(__name__)

# Base directory using /tmp (Render free plan uses ephemeral storage)
BASE_TEMP_DIR = "/tmp"

# Directory for storing temporary download files (will be cleared after each request)
TEMP_DOWNLOAD_DIR = os.path.join(BASE_TEMP_DIR, "download")
os.makedirs(TEMP_DOWNLOAD_DIR, exist_ok=True)

# Directory for storing cached audio files (persists until container restart)
CACHE_DIR = os.path.join(BASE_TEMP_DIR, "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# Directory for storing cached video files separately
CACHE_VIDEO_DIR = os.path.join(BASE_TEMP_DIR, "cache_video")
os.makedirs(CACHE_VIDEO_DIR, exist_ok=True)

# Maximum cache size in bytes (adjusted to 500MB for Render free plan)
MAX_CACHE_SIZE = 500 * 1024 * 1024  # 500MB

# Path to your cookies file (if needed). If not needed, you can leave it None or an empty string.
COOKIES_FILE = "cookies.txt"  # Replace with your actual cookies file path if required

# External Search API URL (used for searching YouTube by title or resolving Spotify links).
# It should accept a query parameter like ?title=<encoded title or URL>.
SEARCH_API_URL = "https://odd-block-a945.tenopno.workers.dev/search"

def get_cache_key(video_url: str) -> str:
    """Generate a cache key from the video URL."""
    return hashlib.md5(video_url.encode('utf-8')).hexdigest()

def get_directory_size(directory: str) -> int:
    """Return total size (in bytes) of files under the directory."""
    total_size = 0
    for dirpath, dirnames, filenames in os.walk(directory):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            if os.path.isfile(fp):
                total_size += os.path.getsize(fp)
    return total_size

def check_cache_size_and_cleanup():
    """
    Check combined cache size and remove all cache files if it exceeds the threshold.
    This is a simple ‘flush all’ policy to stay within storage limits.
    """
    total_size = get_directory_size(CACHE_DIR) + get_directory_size(CACHE_VIDEO_DIR)
    if total_size > MAX_CACHE_SIZE:
        for cache_dir in [CACHE_DIR, CACHE_VIDEO_DIR]:
            for file in os.listdir(cache_dir):
                file_path = os.path.join(cache_dir, file)
                try:
                    os.remove(file_path)
                except Exception as e:
                    app.logger.warning(f"Error deleting cache file {file_path}: {e}")

def resolve_spotify_link(url: str) -> str:
    """
    If the URL is a Spotify link, use the search API to find the corresponding YouTube link.
    Otherwise, return the URL unchanged.
    """
    if "spotify.com" in url:
        # Use requests to query SEARCH_API_URL with parameter 'title' or similar,
        # depending on your API design. Here we assume ?title=<encoded URL> returns JSON with 'link'.
        params = {"title": url}
        resp = requests.get(SEARCH_API_URL, params=params, timeout=15)
        if resp.status_code != 200:
            raise Exception("Failed to fetch search results for the Spotify link")
        search_result = resp.json()
        if not search_result or 'link' not in search_result:
            raise Exception("No YouTube link found for the given Spotify link")
        return search_result['link']
    return url

def download_audio(video_url: str) -> str:
    """
    Download audio from the given YouTube video URL with caching.
    If already cached, return the cached file path.
    """
    cache_key = get_cache_key(video_url)
    # look for any cached file matching cache_key.*
    cached_files = glob.glob(os.path.join(CACHE_DIR, f"{cache_key}.*"))
    if cached_files:
        return cached_files[0]

    unique_id = str(uuid.uuid4())
    # temp output template; yt_dlp will append extension
    output_template = os.path.join(TEMP_DOWNLOAD_DIR, f"{unique_id}.%(ext)s")

    # Determine ffmpeg location: allow override via env var FFMPEG_PATH or default to '/usr/bin/ffmpeg'
    ffmpeg_path = os.getenv("FFMPEG_PATH", "/usr/bin/ffmpeg")

    ydl_opts = {
        'format': 'worstaudio',  # select the worst-quality audio-only stream
        'outtmpl': output_template,
        'noplaylist': True,
        'quiet': True,
        'socket_timeout': 60,
        # If cookies file is needed and exists, pass it; else omit
        **({'cookiefile': COOKIES_FILE} if COOKIES_FILE and os.path.isfile(COOKIES_FILE) else {}),
        # specify ffmpeg location so merging/extraction works:
        'ffmpeg_location': ffmpeg_path,
        # no postprocessors needed for audio-only; yt-dlp will handle extraction
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(video_url, download=True)
            # Prepare filename: this yields the temp file path with correct extension
            downloaded_file = ydl.prepare_filename(info)
            # Determine actual extension from info
            ext = info.get("ext", os.path.splitext(downloaded_file)[1].lstrip(".")) or "m4a"
            cached_file_path = os.path.join(CACHE_DIR, f"{cache_key}.{ext}")
            # Move to cache
            shutil.move(downloaded_file, cached_file_path)
            # Cleanup cache if too large
            check_cache_size_and_cleanup()
            return cached_file_path
        except Exception as e:
            # Clean up any partial temp file
            app.logger.error(f"Error downloading audio for {video_url}: {e}")
            raise Exception(f"Error downloading audio: {e}")

def download_video(video_url: str) -> str:
    """
    Download video (with audio) from the given YouTube video URL in <=240p and worst audio, with caching.
    If cached, return the cached file path.
    """
    # use a different cache key namespace for video
    cache_key = hashlib.md5((video_url + "_video").encode('utf-8')).hexdigest()
    cached_files = glob.glob(os.path.join(CACHE_VIDEO_DIR, f"{cache_key}.*"))
    if cached_files:
        return cached_files[0]

    unique_id = str(uuid.uuid4())
    output_template = os.path.join(TEMP_DOWNLOAD_DIR, f"{unique_id}.%(ext)s")

    ffmpeg_path = os.getenv("FFMPEG_PATH", "/usr/bin/ffmpeg")

    ydl_opts = {
        # select worst video up to 240p + worst audio-only, then merge
        'format': 'worstvideo[height<=240]+worstaudio',
        'merge_output_format': 'mp4',
        'outtmpl': output_template,
        'noplaylist': True,
        'quiet': True,
        'socket_timeout': 60,
        **({'cookiefile': COOKIES_FILE} if COOKIES_FILE and os.path.isfile(COOKIES_FILE) else {}),
        'ffmpeg_location': ffmpeg_path,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(video_url, download=True)
            # After download+merge, prepare_filename(info) may point to the merged file.
            downloaded_file = ydl.prepare_filename(info)
            # Ensure .mp4 extension (merge_output_format was mp4)
            cached_file_path = os.path.join(CACHE_VIDEO_DIR, f"{cache_key}.mp4")
            # If the temp downloaded_file isn't already at .mp4, move/rename it
            # Some cases yt-dlp may produce .mp4 directly; handle both
            if os.path.abspath(downloaded_file) != os.path.abspath(cached_file_path):
                # Move to cache; overwrite if exists
                shutil.move(downloaded_file, cached_file_path)
            check_cache_size_and_cleanup()
            return cached_file_path
        except Exception as e:
            app.logger.error(f"Error downloading video for {video_url}: {e}")
            raise Exception(f"Error downloading video: {e}")

@app.route('/search', methods=['GET'])
def search_video():
    """
    Search for a YouTube video using the external API.
    Expects query parameter: ?title=<search terms>
    Returns JSON with title, url (YouTube link), duration (if provided).
    """
    try:
        query = request.args.get('title')
        if not query:
            return jsonify({"error": "The 'title' parameter is required"}), 400
        # URL-encode the query
        # Using requests.get with params so encoding is handled:
        params = {"title": query}
        resp = requests.get(SEARCH_API_URL, params=params, timeout=15)
        if resp.status_code != 200:
            app.logger.error(f"Search API returned {resp.status_code} for query {query}")
            return jsonify({"error": "Failed to fetch search results"}), 500
        search_result = resp.json()
        if not search_result or 'link' not in search_result:
            return jsonify({"error": "No videos found for the given query"}), 404
        return jsonify({
            "title": search_result.get("title"),
            "url": search_result["link"],
            "duration": search_result.get("duration"),
        })
    except Exception as e:
        app.logger.error(f"Exception in /search: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/vdown', methods=['GET'])
def download_video_endpoint():
    """
    Download video from a YouTube video URL (or search by title) in <=240p + worst audio.
    Usage: /vdown?url=<YouTube URL>  OR /vdown?title=<search terms>
    """
    try:
        video_url = request.args.get('url')
        video_title = request.args.get('title')

        if not video_url and not video_title:
            return jsonify({"error": "Either 'url' or 'title' parameter is required"}), 400

        # If title provided, resolve via search API
        if video_title and not video_url:
            params = {"title": video_title}
            resp = requests.get(SEARCH_API_URL, params=params, timeout=15)
            if resp.status_code != 200:
                app.logger.error(f"Search API error for title {video_title}: {resp.status_code}")
                return jsonify({"error": "Failed to fetch search results"}), 500
            search_result = resp.json()
            if not search_result or 'link' not in search_result:
                return jsonify({"error": "No videos found for the given query"}), 404
            video_url = search_result['link']

        # If Spotify link, resolve to YouTube
        if video_url and "spotify.com" in video_url:
            video_url = resolve_spotify_link(video_url)

        cached_file_path = download_video(video_url)

        return send_file(
            cached_file_path,
            as_attachment=True,
            download_name=os.path.basename(cached_file_path)
        )
    except Exception as e:
        app.logger.error(f"Exception in /vdown: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        # Clean up temporary download files only (not the caches)
        for file in os.listdir(TEMP_DOWNLOAD_DIR):
            file_path = os.path.join(TEMP_DOWNLOAD_DIR, file)
            try:
                os.remove(file_path)
            except Exception as cleanup_error:
                app.logger.warning(f"Error deleting temp file {file_path}: {cleanup_error}")

@app.route('/download', methods=['GET'])
def download_audio_endpoint():
    """
    Download audio from a YouTube video URL or search for it by title and download.
    Usage: /download?url=<YouTube URL>  OR /download?title=<search terms>
    """
    try:
        video_url = request.args.get('url')
        video_title = request.args.get('title')

        if not video_url and not video_title:
            return jsonify({"error": "Either 'url' or 'title' parameter is required"}), 400

        if video_title and not video_url:
            params = {"title": video_title}
            resp = requests.get(SEARCH_API_URL, params=params, timeout=15)
            if resp.status_code != 200:
                app.logger.error(f"Search API error for title {video_title}: {resp.status_code}")
                return jsonify({"error": "Failed to fetch search results"}), 500
            search_result = resp.json()
            if not search_result or 'link' not in search_result:
                return jsonify({"error": "No videos found for the given query"}), 404
            video_url = search_result['link']

        if video_url and "spotify.com" in video_url:
            video_url = resolve_spotify_link(video_url)

        cached_file_path = download_audio(video_url)

        return send_file(
            cached_file_path,
            as_attachment=True,
            download_name=os.path.basename(cached_file_path)
        )
    except Exception as e:
        app.logger.error(f"Exception in /download: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        # Clean up temporary download files only (not the caches)
        for file in os.listdir(TEMP_DOWNLOAD_DIR):
            file_path = os.path.join(TEMP_DOWNLOAD_DIR, file)
            try:
                os.remove(file_path)
            except Exception as cleanup_error:
                app.logger.warning(f"Error deleting temp file {file_path}: {cleanup_error}")

@app.route('/')
def home():
    return """
    <h1>🎶 YouTube Audio/Video Downloader API</h1>
    <p>Use this API to search and download audio or video from YouTube.</p>
    <p><strong>Endpoints:</strong></p>
    <ul>
        <li><strong>/search</strong>: Search for a video by title. Query parameter: <code>?title=</code></li>
        <li><strong>/download</strong>: Download audio by URL or search by title. Query parameters: <code>?url=</code> or <code>?title=</code></li>
        <li><strong>/vdown</strong>: Download video (≤240p + worst audio) by URL or search by title. Query parameters: <code>?url=</code> or <code>?title=</code></li>
    </ul>
    <p>Examples:</p>
    <ul>
        <li>Search: <code>/search?title=Your%20Favorite%20Song</code></li>
        <li>Download by URL (audio): <code>/download?url=https://www.youtube.com/watch?v=dQw4w9WgXcQ</code></li>
        <li>Download by Title (audio): <code>/download?title=Your%20Favorite%20Song</code></li>
        <li>Download by URL (video): <code>/vdown?url=https://www.youtube.com/watch?v=dQw4w9WgXcQ</code></li>
        <li>Download from Spotify: <code>/download?url=https://open.spotify.com/track/...</code></li>
    </ul>
    """

if __name__ == '__main__':
    # In production under Render, you may not use app.run; Render manages the entrypoint.
    # But for local testing:
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))





