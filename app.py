import os
import time
import requests
from tqdm import tqdm
from flask import Flask, request, send_file, jsonify
from urllib.parse import urlparse
import concurrent.futures
import tempfile

app = Flask(__name__)

API_ENDPOINT = "https://kustbots.frozenbotsweb.workers.dev/?url={url}&type=audio"


def get_file_extension(url):
    if "mime=audio%2Fmp4" in url or "mime=audio/mp4" in url:
        return ".m4a"
    elif "mime=video%2Fmp4" in url or "mime=video/mp4" in url:
        return ".mp4"
    elif "mime=audio%2Fwebm" in url or "mime=audio/webm" in url:
        return ".webm"
    return ".bin"


def resolve_fastest_cdn(original_url):
    parsed = urlparse(original_url)
    host = parsed.hostname
    if not host or "googlevideo.com" not in host:
        return original_url

    base_host = host.split(".googlevideo.com")[0]
    prefix = base_host.split("---")[0]
    sn = base_host.split("---")[-1]
    alt_hosts = [f"{prefix[0:2]}{i}---{sn}.googlevideo.com" for i in range(2, 7)]
    alt_urls = [original_url.replace(host, h) for h in alt_hosts]

    def timed_head(url):
        try:
            start = time.time()
            r = requests.head(url, timeout=2)
            if r.status_code == 200:
                return (time.time() - start, url)
        except:
            pass
        return (float("inf"), url)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(timed_head, alt_urls))

    best_time, best_url = min(results, key=lambda x: x[0])
    return best_url


def download_with_progress(url, output_path, num_workers=4):
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Connection": "keep-alive"
    }

    # Try HEAD request for content length, fallback to GET if not supported
    try:
        head = requests.head(url, headers=headers, timeout=5)
        if 'content-length' in head.headers:
            total_size = int(head.headers['content-length'])
        elif head.status_code in (200, 206) and 'content-range' in head.headers:
            total_size = int(head.headers['content-range'].split('/')[-1])
        else:
            raise Exception
    except:
        # Fallback to GET without range
        r0 = requests.get(url, headers=headers, stream=True, timeout=10)
        total_size = int(r0.headers.get('content-length', 0))
        if total_size == 0:
            raise Exception("Couldn't determine file size.")

    filename = os.path.basename(urlparse(url).path)
    ext = get_file_extension(url)
    full_path = os.path.join(output_path, filename + ext)

    with open(full_path, 'wb') as f:
        f.truncate(total_size)

    def download_range(start, end, index):
        part_headers = headers.copy()
        part_headers['Range'] = f"bytes={start}-{end}"
        try:
            res = requests.get(url, headers=part_headers, stream=True)
            with open(full_path, 'r+b') as f:
                f.seek(start)
                for chunk in res.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        progress_bars[index].update(len(chunk))
        except Exception as e:
            print(f"❌ Thread {index} failed: {e}")

    part_size = total_size // num_workers
    ranges = [(i * part_size, total_size - 1 if i == num_workers - 1 else ((i + 1) * part_size - 1)) for i in range(num_workers)]

    global progress_bars
    progress_bars = [tqdm(
        desc=f"Thread {i+1}",
        total=(r[1] - r[0] + 1),
        unit='B',
        unit_scale=True,
        unit_divisor=1024,
        ncols=80,
        position=i
    ) for i, r in enumerate(ranges)]

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = [executor.submit(download_range, start, end, idx) for idx, (start, end) in enumerate(ranges)]
        concurrent.futures.wait(futures)

    return full_path


def fetch_direct_link(video_url):
    print("🔍 Fetching direct CDN link...")
    try:
        r = requests.get(API_ENDPOINT.format(url=video_url), timeout=10)
        data = r.json()
        if r.status_code == 200 and "response" in data and "direct_link" in data["response"]:
            return data["response"]["direct_link"]
    except Exception as e:
        print(f"❌ API Error: {e}")
    return None


@app.route('/down')
def download():
    input_url = request.args.get("url", "").strip()
    if not input_url:
        return jsonify({"error": "No URL provided"}), 400

    direct_link = fetch_direct_link(input_url)
    if not direct_link:
        return jsonify({"error": "Invalid or missing CDN link"}), 500

    resolved_url = resolve_fastest_cdn(direct_link)
    print(f"🚀 Using CDN: {resolved_url}")
    print("💾 Downloading to temp dir...")

    temp_dir = tempfile.mkdtemp()
    try:
        start_time = time.time()
        file_path = download_with_progress(resolved_url, temp_dir)
        elapsed = time.time() - start_time
        print(f"\n✅ Download complete in {elapsed:.2f} seconds")
        print(f"📍 File location: {file_path}")
        return send_file(file_path, as_attachment=True)
    except Exception as e:
        print(f"❌ Download error: {e}")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True, port=8000)  # Port unchanged

