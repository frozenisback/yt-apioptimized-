from flask import Flask, request, jsonify, send_file
import yt_dlp
import os
import uuid
import requests
import hashlib
import glob
import shutil

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

# Cookie file path: allow override via environment variable COOKIE_FILE_PATH
# Ensure this is a valid cookies.txt in Netscape format (export from browser)
COOKIE_FILE_PATH = os.getenv("COOKIE_FILE_PATH", "cookies.txt")
if COOKIE_FILE_PATH:
    # If relative path, make absolute under working dir or /tmp
    COOKIE_FILE_PATH = os.path.abspath(COOKIE_FILE_PATH)
# Check existence now:
if COOKIE_FILE_PATH and os.path.isfile(COOKIE_FILE_PATH):
    app.logger.info(f"Using cookie file at: {COOKIE_FILE_PATH}")
else:
    app.logger.warning(f"Cookie file not found or unreadable at: {COOKIE_FILE_PATH}. "
                       "Continuing without cookies.")
    COOKIE_FILE_PATH = None  # disable use if not valid

# External Search API URL (used for searching YouTube by title or resolving Spotify links).
SEARCH_API_URL = "https://odd-block-a945.tenopno.workers.dev/search"

def get_cache_key(video_url: str) -> str:
    """Generate a cache key from the video URL."""
    return hashlib.md5(video_url.encode('utf-8')).hexdigest()

def get_directory_size(directory: str) -> int:
    total_size = 0
    for dirpath, dirnames, filenames in os.walk(directory):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            if os.path.isfile(fp):
                total_size += os.path.getsize(fp)
    return total_size

def check_cache_size_and_cleanup():
    total_size = get_directory_size(CACHE_DIR) + get_directory_size(CACHE_VIDEO_DIR)
    if total_size > MAX_CACHE_SIZE:
        app.logger.info(f"Cache size {total_size} exceeds {MAX_CACHE_SIZE}, clearing caches.")
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
        params = {"title": url}
        resp = requests.get(SEARCH_API_URL, params=params, timeout=15)
        if resp.status_code != 200:
            raise Exception("Failed to fetch search results for the Spotify link")
        search_result = resp.json()
        if not search_result or 'link' not in search_result:
            raise Exception("No YouTube link found for the given Spotify link")
        return search_result['link']
    return url

def make_ydl_opts_audio(output_template: str):
    """
    Build optimized yt-dlp options for lightweight audio-only download with no post-processing.
    """
    ffmpeg_path = os.getenv("FFMPEG_PATH", "/usr/bin/ffmpeg")
    
    opts = {
        'format': 'worstaudio',
        'outtmpl': output_template,
        'noplaylist': True,
        'quiet': True,
        'socket_timeout': 60,
        'ffmpeg_location': ffmpeg_path,
        'nocheckcertificate': True,
        'forceipv4': True,
        'no_warnings': True,
        'postprocessors': [],  # disables all post-processing
        'writesubtitles': False,
        'writeautomaticsub': False,
        'writethumbnail': False,
        'writeinfojson': False,
        'extractor_args': {'youtube': ['player_client=web']},  # speeds up extraction
    }

    if COOKIE_FILE_PATH:
        opts['cookiefile'] = COOKIE_FILE_PATH

    return opts

def make_ydl_opts_video(output_template: str):
    """
    Build yt-dlp options for video+audio download, <=240p video + worst audio.
    """
    ffmpeg_path = os.getenv("FFMPEG_PATH", "/usr/bin/ffmpeg")
    opts = {
        'format': 'worstvideo[height<=240]+worstaudio',
        'merge_output_format': 'mp4',
        'outtmpl': output_template,
        'noplaylist': True,
        'quiet': True,
        'socket_timeout': 60,
        'ffmpeg_location': ffmpeg_path,
    }
    if COOKIE_FILE_PATH:
        opts['cookiefile'] = COOKIE_FILE_PATH
    return opts

def download_audio(video_url: str) -> str:
    cache_key = get_cache_key(video_url)
    cached_files = glob.glob(os.path.join(CACHE_DIR, f"{cache_key}.*"))
    if cached_files:
        return cached_files[0]

    unique_id = str(uuid.uuid4())
    output_template = os.path.join(TEMP_DOWNLOAD_DIR, f"{unique_id}.%(ext)s")
    ydl_opts = make_ydl_opts_audio(output_template)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(video_url, download=True)
            downloaded_file = ydl.prepare_filename(info)
            # Determine extension
            ext = info.get("ext", os.path.splitext(downloaded_file)[1].lstrip(".")) or "m4a"
            cached_file_path = os.path.join(CACHE_DIR, f"{cache_key}.{ext}")
            # Move to cache
            shutil.move(downloaded_file, cached_file_path)
            check_cache_size_and_cleanup()
            return cached_file_path
        except Exception as e:
            app.logger.error(f"Error downloading audio for {video_url}: {e}")
            raise Exception(f"Error downloading audio: {e}")

def download_video(video_url: str) -> str:
    cache_key = hashlib.md5((video_url + "_video").encode('utf-8')).hexdigest()
    cached_files = glob.glob(os.path.join(CACHE_VIDEO_DIR, f"{cache_key}.*"))
    if cached_files:
        return cached_files[0]

    unique_id = str(uuid.uuid4())
    output_template = os.path.join(TEMP_DOWNLOAD_DIR, f"{unique_id}.%(ext)s")
    ydl_opts = make_ydl_opts_video(output_template)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(video_url, download=True)
            downloaded_file = ydl.prepare_filename(info)
            cached_file_path = os.path.join(CACHE_VIDEO_DIR, f"{cache_key}.mp4")
            # Move/rename to ensure .mp4
            if os.path.abspath(downloaded_file) != os.path.abspath(cached_file_path):
                shutil.move(downloaded_file, cached_file_path)
            check_cache_size_and_cleanup()
            return cached_file_path
        except Exception as e:
            app.logger.error(f"Error downloading video for {video_url}: {e}")
            raise Exception(f"Error downloading video: {e}")

@app.route('/search', methods=['GET'])
def search_video():
    try:
        query = request.args.get('title')
        if not query:
            return jsonify({"error": "The 'title' parameter is required"}), 400
        resp = requests.get(SEARCH_API_URL, params={"title": query}, timeout=15)
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
    try:
        video_url = request.args.get('url')
        video_title = request.args.get('title')
        if not video_url and not video_title:
            return jsonify({"error": "Either 'url' or 'title' parameter is required"}), 400
        if video_title and not video_url:
            resp = requests.get(SEARCH_API_URL, params={"title": video_title}, timeout=15)
            if resp.status_code != 200:
                app.logger.error(f"Search API error for title {video_title}: {resp.status_code}")
                return jsonify({"error": "Failed to fetch search results"}), 500
            search_result = resp.json()
            if not search_result or 'link' not in search_result:
                return jsonify({"error": "No videos found for the given query"}), 404
            video_url = search_result['link']
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
    try:
        video_url = request.args.get('url')
        video_title = request.args.get('title')
        if not video_url and not video_title:
            return jsonify({"error": "Either 'url' or 'title' parameter is required"}), 400
        if video_title and not video_url:
            resp = requests.get(SEARCH_API_URL, params={"title": video_title}, timeout=15)
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
    # For local testing
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))









