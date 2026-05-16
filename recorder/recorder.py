import asyncio
from playwright.async_api import async_playwright
import subprocess
import os
import datetime
import shutil
import base64
import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def log(message, end="\n"):
    """Log con timestamp detallado"""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}", end=end)

def is_stream_online(username):
    """Verifica si el stream está en vivo mediante API de Stripchat"""
    try:
        response = requests.get(
            f"https://stripchat.com/api/front/v2/models/username/{username}/cam",
            timeout=10,
            headers={
                "User-Agent": "Mozilla/5.0"
            }
        )
        if response.status_code != 200:
            log(f"[API CHECK] Status code {response.status_code}, asumiendo stream activo")
            return True
        
        data = response.json()
        status = data["user"]["user"]["status"]
        is_cam_available = data["cam"]["isCamAvailable"]
        is_cam_active = data["cam"]["isCamActive"]
        
        online = (
            status == "public"
            and is_cam_available
            and is_cam_active
        )
        
        log(f"[API CHECK] Status: {status} | Available: {is_cam_available} | Active: {is_cam_active} | Online: {online}")
        return online
        
    except requests.exceptions.Timeout:
        log(f"[WARN] API timeout, asumiendo stream activo")
        return True
    except requests.exceptions.RequestException as e:
        log(f"[WARN] API error: {e}")
        return True
    except (KeyError, ValueError) as e:
        log(f"[WARN] API response parsing error: {e}")
        return True

async def record_stream(profile_url):
    if not shutil.which("ffmpeg"):
        log("[ERROR] FFmpeg is not installed on the system.")
        return

    raw_files = {}
    browser = None
    last_api_check = datetime.datetime.now()
    api_check_interval = 120  # 2 minutos

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 720}
            )
            page = await context.new_page()

            log("[INFO] Injecting MediaSource interceptor into the browser...")

            async def python_append_chunk(buffer_id, mime_type, b64_data):
                if buffer_id not in raw_files:
                    ext = "mp4" if "video" in mime_type else "m4a"
                    tmp_name = os.path.join(SCRIPT_DIR, f"tmp_{buffer_id}.{ext}")
                    
                    try:
                        raw_files[buffer_id] = {
                            "file": open(tmp_name, "wb"),
                            "name": tmp_name,
                            "type": ext,
                            "flush_counter": 0,
                            "total_bytes": 0
                        }
                        log(f"[STREAM INFO] Buffer ID: {buffer_id} | MIME: {mime_type} | EXT: {ext} | Path: {tmp_name}")
                    except Exception as e:
                        log(f"[ERROR] Failed to create temp file {tmp_name}: {e}")
                        return
                
                try:
                    data = base64.b64decode(b64_data)
                    f = raw_files[buffer_id]["file"]
                    f.write(data)
                    raw_files[buffer_id]["flush_counter"] += len(data)
                    raw_files[buffer_id]["total_bytes"] += len(data)
                    
                    if raw_files[buffer_id]["flush_counter"] >= 5 * 1024 * 1024:
                        f.flush()
                        log(f"[BUFFER FLUSH] Buffer {buffer_id} flushed ({raw_files[buffer_id]['flush_counter'] / (1024*1024):.2f} MB)")
                        raw_files[buffer_id]["flush_counter"] = 0
                        
                except Exception as e:
                    log(f"[WARN] Failed to decode or write chunk to buffer {buffer_id}: {e}")

            await page.expose_function("python_append_chunk", python_append_chunk)

            js_hook = """
            const OriginalMediaSource = window.MediaSource;
            window.MediaSource = class extends OriginalMediaSource {
                addSourceBuffer(mimeType) {
                    const sourceBuffer = super.addSourceBuffer.apply(this, arguments);
                    const originalAppendBuffer = sourceBuffer.appendBuffer;
                    const bufferId = Math.random().toString(36).substring(7);
                    
                    sourceBuffer.appendBuffer = function(buffer) {
                        if (buffer && (buffer.length || buffer.byteLength)) {
                            try {
                                const uint8 = new Uint8Array(buffer);
                                let binary = '';
                                const chunkSize = 8192;
                                for (let i = 0; i < uint8.length; i += chunkSize) {
                                    binary += String.fromCharCode.apply(null, uint8.subarray(i, i + chunkSize));
                                }
                                const b64 = btoa(binary);
                                window.python_append_chunk(bufferId, mimeType, b64);
                            } catch (e) {
                                console.error("[JS Hook Error]", e);
                            }
                        }
                        return originalAppendBuffer.apply(this, arguments);
                    };
                    return sourceBuffer;
                }
            };
            """
            await page.add_init_script(js_hook)

            log(f"[INFO] Navigating to: {profile_url}")
            
            # Extraer username de la URL para verificar si está online
            username = profile_url.rstrip('/').split('/')[-1]
            log(f"[INFO] Username extracted: {username}")
            
            try:
                await page.goto(profile_url, wait_until="domcontentloaded", timeout=45000)
                log("[INFO] Page loaded successfully")
                
                # Attempt to bypass age restrictions if present
                try:
                    await page.locator("button:has-text('I Agree'), button:has-text('Estoy de acuerdo')").first.click(timeout=3000)
                    log("[INFO] Age restriction dialog bypassed")
                except Exception:
                    log("[DEBUG] No age restriction dialog found")
                    
                await page.mouse.wheel(0, 500)
                log("[DEBUG] Page scrolled")
                
                # Attempt to click play if autoplay is disabled
                try:
                    await page.locator(".video-player-play-button, button:has-text('Play')").first.click(timeout=3000)
                    log("[INFO] Play button clicked")
                except Exception:
                    log("[DEBUG] No play button found or autoplay enabled")

                log("[INFO] Recording started. Target limit: 30 GB or stream end.")
                log("[INFO] Will check if stream is online every 2 minutes via API")
                
                seconds_without_data = 0
                previous_size = 0
                MAX_BYTES = 30 * 1024 * 1024 * 1024  # 30 GB
                # MAX_BYTES = 30 * 1024 * 1024  # Test 30 mb
                
                while True:
                    await asyncio.sleep(5)
                    
                    # Verificar cada 2 minutos si el stream está online
                    current_time = datetime.datetime.now()
                    if (current_time - last_api_check).total_seconds() >= api_check_interval:
                        last_api_check = current_time
                        if not is_stream_online(username):
                            log("[INFO] Stream is OFFLINE according to API check. Stopping recording.")
                            break
                    
                    current_size = sum(
                        os.path.getsize(info["name"]) 
                        for info in raw_files.values() 
                        if os.path.exists(info["name"])
                    )
                    
                    if current_size > previous_size:
                        seconds_without_data = 0
                        previous_size = current_size
                    else:
                        seconds_without_data += 5
                        
                    downloaded_mb = current_size / (1024 * 1024)
                    log(f"[RECORDING] Size: {downloaded_mb:.2f} MB / 30000 MB | No data for: {seconds_without_data}s", end="\r")

                    if current_size >= MAX_BYTES:
                        log(f"\n[INFO] Target size 30000 MB reached ({downloaded_mb:.2f} MB). Stopping recording.")
                        break
                        
                    if seconds_without_data >= 30:
                        if current_size == 0:
                            log("\n[WARN] Stream never started or the model is currently offline (0 bytes captured).")
                        else:
                            log(f"\n[INFO] Video stream stopped receiving data for 30 seconds. Stopping recording.")
                        break
                        
                    try:
                        if await page.locator("text='Offline', text='is offline', .offline-screen").count() > 0:
                            log("\n[INFO] Offline screen detected on page. Stopping recording.")
                            break
                    except Exception:
                        pass
                    
            except Exception as e:
                log(f"\n[ERROR] Navigation or recording interrupted: {str(e)}")

    finally:
        log("[INFO] Entering cleanup phase...")
        
        # Guarantee browser closure
        if browser:
            try:
                await browser.close()
                log("[INFO] Browser closed successfully")
            except Exception as e:
                log(f"[WARN] Error closing browser: {e}")

        # Guarantee safe closure and validation of raw files
        valid_files = []
        log(f"[INFO] Closing and validating {len(raw_files)} buffer(s)...")
        
        for buf_id, info in raw_files.items():
            try:
                info["file"].flush()
                info["file"].close()
                log(f"[DEBUG] Buffer {buf_id} closed (total: {info['total_bytes'] / (1024*1024):.2f} MB)")
            except Exception as e:
                log(f"[WARN] Error closing buffer {buf_id}: {e}")
            
            if os.path.exists(info["name"]) and os.path.getsize(info["name"]) > 1000:
                valid_files.append(info["name"])
                log(f"[INFO] Buffer {buf_id} validated as valid ({os.path.getsize(info['name']) / (1024*1024):.2f} MB)")
            else:
                try: 
                    os.remove(info["name"])
                    log(f"[INFO] Buffer {buf_id} removed (size < 1KB)")
                except Exception:
                    log(f"[WARN] Could not delete buffer {buf_id}")

        if not valid_files:
            log("[WARN] No valid video chunks were captured. Aborting merge process.")
            return

        log(f"[INFO] Merging {len(valid_files)} stream file(s) using FFmpeg...")
        
        model_name = profile_url.rstrip('/').split('/')[-1]
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        video_filename = f"{model_name}_{timestamp}.mkv"
        final_output_path = os.path.join(SCRIPT_DIR, video_filename)

        largest_file = max(valid_files, key=os.path.getsize)
        log(f"[INFO] Using largest stream file: {largest_file} ({os.path.getsize(largest_file) / (1024*1024):.2f} MB)")

        try:
            probe_cmd = [
                'ffprobe',
                '-v', 'error',
                '-select_streams', 'v:0',
                '-show_entries', 'stream=width,height',
                '-of', 'csv=p=0',
                largest_file
            ]
            result = subprocess.run(probe_cmd, capture_output=True, text=True)
            if result.returncode == 0:
                resolution = result.stdout.strip()
                log(f"[INFO] Detected video resolution: {resolution}")
            else:
                log("[WARN] Could not determine stream video resolution")
        except Exception as e:
            log(f"[WARN] Error probing video: {e}")

        log("[INFO] Starting FFmpeg encoding (scale 1920x1080, libx264, preset slow)...")
        
        ffmpeg_cmd = [
            'ffmpeg',
            '-y',
            '-fflags', '+genpts',
            '-i', largest_file,
            '-vf', 'scale=1920:1080:flags=bilinear',
            '-c:v', 'libx264',
            '-preset', 'veryfast',
            '-crf', '19',
            '-profile:v', 'high',
            '-level', '4.2',
            '-pix_fmt', 'yuv420p',
            '-movflags', '+faststart',
            '-c:a', 'aac',
            '-b:a', '192k',
            '-ar', '48000',
            final_output_path
        ]
        
        try:
            log("[INFO] FFmpeg command: " + " ".join(ffmpeg_cmd))
            result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)
            
            if result.returncode != 0:
                log(f"[ERROR] FFmpeg failed to merge files. STDERR:\n{result.stderr}")
            elif os.path.exists(final_output_path):
                final_size_mb = os.path.getsize(final_output_path) / (1024 * 1024)
                log(f"[SUCCESS] File successfully saved as {final_output_path} ({final_size_mb:.2f} MB)")
            else:
                log("[ERROR] FFmpeg execution completed, but the output file is missing.")
                
        except Exception as e:
             log(f"[ERROR] Exception occurred while running FFmpeg: {e}")

        # Guarantee cleanup of temporary chunks
        log("[INFO] Cleaning up temporary chunk files...")
        for f in valid_files:
            try: 
                if os.path.exists(f):
                    os.remove(f)
                    log(f"[DEBUG] Deleted temporary file: {f}")
            except Exception as e: 
                log(f"[WARN] Could not delete temporary file {f}: {e}")
        
        log("[INFO] Recording and encoding process completed.")

if __name__ == "__main__":
    target_url = os.environ.get("STREAM_URL")
    
    if not target_url:
        log("[ERROR] No STREAM_URL provided. Exiting.")
    else:
        log(f"[START] Stream Recorder initialized")
        log(f"[START] Target URL: {target_url}")
        asyncio.run(record_stream(target_url))
        log(f"[END] Stream Recorder finished")
