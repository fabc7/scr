import asyncio
from playwright.async_api import async_playwright
import subprocess
import os
import datetime
import shutil
import base64
import requests
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def log(message, end="\n"):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}", end=end)

def is_stream_online(username):
    try:
        response = requests.get(
            f"https://stripchat.com/api/front/v2/models/username/{username}/cam",
            timeout=10,
            headers={
                "User-Agent": "Mozilla/5.0"
            }
        )

        if response.status_code != 200:
            return True

        data = response.json()

        return (
            data["user"]["user"]["status"] == "public"
            and data["cam"]["isCamAvailable"]
            and data["cam"]["isCamActive"]
        )

    except Exception as e:
        log(f"[WARN] API check failed: {e}")
        return True
        
async def record_stream(profile_url):
    if not shutil.which("ffmpeg"):
        log("[ERROR] FFmpeg is not installed on the system.")
        return

    raw_files = {}
    browser = None

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
                            "flush_counter": 0
                        }
                        log(f"[STREAM INFO]")
                        log(f"  MIME: {mime_type}")
                        log(f"  EXT: {ext}")
                    except Exception as e:
                        log(f"[ERROR] Failed to create temp file {tmp_name}: {e}")
                        return
                
                try:
                    data = base64.b64decode(b64_data)
                    f = raw_files[buffer_id]["file"]
                    f.write(data)
                    raw_files[buffer_id]["flush_counter"] += len(data)
                    if raw_files[buffer_id]["flush_counter"] >= 5 * 1024 * 1024:
                        f.flush()
                        raw_files[buffer_id]["flush_counter"] = 0
                        
                except Exception as e:
                    log(f"\n[WARN] Failed to decode or write chunk: {e}")

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
                            const uint8 = new Uint8Array(buffer);
                    
                            try {
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
            
            try:
                await page.goto(profile_url, wait_until="domcontentloaded", timeout=45000)
                
                # Attempt to bypass age restrictions if present
                try:
                    await page.locator("button:has-text('I Agree'), button:has-text('Estoy de acuerdo')").first.click(timeout=3000)
                except Exception:
                    pass 
                    
                await page.mouse.wheel(0, 500)
                
                # Attempt to click play if autoplay is disabled
                try:
                    await page.locator(".video-player-play-button, button:has-text('Play')").first.click(timeout=3000)
                except Exception:
                    pass

                log("[INFO] Recording started. Target limit: 15 GB or stream end.")
                
                seconds_without_video = 0
                previous_video_size = 0
                
                last_api_check = 0
                
                MIN_REAL_GROWTH = 512 * 1024
                API_CHECK_INTERVAL = 60
                VIDEO_TIMEOUT = 60
                # MAX_BYTES = 30 * 1024 * 1024 * 1024 # 30 GB
                MAX_BYTES = 20 * 1024 * 1024 # Test 20 mb
                
                while True:
                    await asyncio.sleep(5)
                    
                    video_size = sum(
                        os.path.getsize(info["name"])
                        for info in raw_files.values()
                        if info["type"] == "mp4" and os.path.exists(info["name"])
                    )
                    
                    growth = video_size - previous_video_size

                    if growth >= MIN_REAL_GROWTH:
                        seconds_without_video = 0
                    else:
                        seconds_without_video += 5
                    
                    previous_video_size = video_size
                        
                    downloaded_mb = video_size / (1024 * 1024)
                    log(
                        f"Recording... "
                        f"Video: {downloaded_mb:.2f} MB | "
                        f"No video: {seconds_without_video}s",
                        end="\r"
                    )

                    current_time = time.time()
                    if current_time - last_api_check >= API_CHECK_INTERVAL:
                        username = profile_url.rstrip('/').split('/')[-1]
                        if not is_stream_online(username):
                            log("\n\n[INFO] API reports stream offline. Stopping recording.")
                            break
                    
                        last_api_check = current_time

                    if video_size >= MAX_BYTES:
                        log(f"\n\n[INFO] Target size size 30000 MB reached ({downloaded_mb:.2f} MB). Stopping recording.")
                        break
                        
                    if seconds_without_video >= VIDEO_TIMEOUT:
                        if video_size == 0:
                            log("\n\n[WARN] Stream never started or the model is currently offline (0 bytes captured).")
                        else:
                            log("\n\n[INFO] Video stream stopped receiving data. Stopping recording.")
                        break
                        
                    try:
                        if await page.locator("text='Offline', text='is offline', .offline-screen").count() > 0:
                            log("\n\n[INFO] Offline screen detected. Stopping recording.")
                            break
                    except Exception:
                        pass
                    
            except Exception as e:
                log(f"\n[ERROR] Navigation or recording interrupted: {str(e)}")

    finally:
        # Guarantee browser closure
        if browser:
            try:
                await browser.close()
            except Exception:
                pass

        # Guarantee safe closure and validation of raw files
        valid_files = []
        for buf_id, info in raw_files.items():
            try:
                info["file"].flush()
                info["file"].close()
            except Exception:
                pass
            
            if os.path.exists(info["name"]) and os.path.getsize(info["name"]) > 1000:
                valid_files.append(info["name"])
            else:
                try: 
                    os.remove(info["name"])
                except Exception: 
                    pass

        if not valid_files:
            print("\n[WARN] No valid video chunks were captured. Aborting merge process.")
            return

        log("\n[INFO] Merging video and audio streams using FFmpeg...")
        
        model_name = profile_url.rstrip('/').split('/')[-1]
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        video_filename = f"{model_name}_{timestamp}.mkv"
        final_output_path = os.path.join(SCRIPT_DIR, video_filename)

        largest_file = max(valid_files, key=os.path.getsize)

        log(f"[INFO] Using stream file: {largest_file}")

        try:
            probe_cmd = [
                'ffprobe',
                '-v', 'error',
                '-select_streams', 'v:0',
                '-show_entries',
                'stream=width,height:format=duration,size',
                '-of',
                'default=noprint_wrappers=1:nokey=1',
                largest_file
            ]
        
            result = subprocess.run(probe_cmd, capture_output=True, text=True)
        
            if result.returncode == 0:
                lines = result.stdout.strip().splitlines()
        
                width = lines[0]
                height = lines[1]
                duration_seconds = float(lines[2])
                size_bytes = int(lines[3])
        
                hours = int(duration_seconds // 3600)
                minutes = int((duration_seconds % 3600) // 60)
                seconds = duration_seconds % 60
        
                size_mb = size_bytes / (1024 * 1024)
                size_gb = size_mb / 1024
        
                log(f"[INFO] Resolution : {width}x{height}")
                log(f"[INFO] Duration   : {hours:02}:{minutes:02}:{seconds:05.2f}")
                log(f"[INFO] Size       : {size_mb:.2f} MB ({size_gb:.2f} GB)")
        
            else:
                log("[WARN] Could not determine video information")
        
        except Exception as e:
            log(f"[WARN] Error getting video info: {e}")

        """
        ffmpeg_cmd = [
            'ffmpeg',
            '-y',
            '-i', largest_file,
            '-c', 'copy',
            final_output_path
        ]
        """
        
        ffmpeg_cmd = [
            'ffmpeg',
            '-y',
            '-i', largest_file,
            '-vf', 'scale=1920:1080:flags=bicubic',
            '-c:v', 'libx264',
            '-preset', 'faster',
            '-crf', '16',
            '-profile:v', 'high',
            '-pix_fmt', 'yuv420p',
            '-movflags', '+faststart',
            '-c:a', 'copy',
            final_output_path
        ]
        try:
            result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)
            
            if result.returncode != 0:
                log(f"\n[ERROR] FFmpeg failed to merge files. STDERR details:\n{result.stderr}")
            elif os.path.exists(final_output_path):
                final_size_mb = os.path.getsize(final_output_path) / (1024 * 1024)
                log(f"\n[SUCCESS] File successfully saved as {final_output_path} ({final_size_mb:.2f} MB).")
            else:
                log("\n[ERROR] FFmpeg execution completed, but the output file is missing.")
                
        except Exception as e:
             log(f"\n[ERROR] Exception occurred while running FFmpeg: {e}")

        # Guarantee cleanup of temporary chunks
        log("[INFO] Cleaning up temporary chunk files...")
        for f in valid_files:
            try: 
                if os.path.exists(f):
                    os.remove(f)
            except Exception as e: 
                log(f"[WARN] Could not delete temporary file {f}: {e}")

if __name__ == "__main__":
    target_url = os.environ.get("STREAM_URL")
    
    if not target_url:
        log("[ERROR] No STREAM_URL provided. Exiting.")
    else:
        asyncio.run(record_stream(target_url))
