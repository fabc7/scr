import asyncio
from playwright.async_api import async_playwright
import subprocess
import os
import datetime
import shutil
import base64
import logging

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Configurar logging
logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

async def record_stream(profile_url):
    if not shutil.which("ffmpeg"):
        logger.error("FFmpeg is not installed on the system.")
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

            logger.info("Injecting MediaSource interceptor into the browser...")

            async def python_append_chunk(buffer_id, mime_type, b64_data):
                if buffer_id not in raw_files:
                    ext = "mp4" if "video" in mime_type else "m4a"
                    tmp_name = os.path.join(SCRIPT_DIR, f"tmp_{buffer_id}.{ext}")
                    
                    try:
                        raw_files[buffer_id] = {"file": open(tmp_name, "wb"), "name": tmp_name, "type": ext}
                        logger.info(f"New stream detected: {ext} (Mime: {mime_type[:30]}...)")
                    except Exception as e:
                        logger.error(f"Failed to create temp file {tmp_name}: {e}")
                        return
                
                try:
                    data = base64.b64decode(b64_data)
                    raw_files[buffer_id]["file"].write(data)
                except Exception as e:
                    logger.warning(f"Failed to decode or write chunk: {e}")

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

            logger.info(f"Navigating to: {profile_url}")
            
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

                logger.info("Recording started. Target limit: 5 GB or stream end.")
                
                seconds_without_data = 0
                previous_size = 0
                MAX_BYTES = 5 * 1024 * 1024 * 1024
                
                while True:
                    await asyncio.sleep(5)
                    
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
                    print(f"Status: Recording... Current size: {downloaded_mb:.2f} MB / 10.00 MB", end="\r")

                    if current_size >= MAX_BYTES:
                        logger.info(f"\nTarget size of 10 MB reached ({downloaded_mb:.2f} MB). Stopping recording.")
                        break
                        
                    if seconds_without_data >= 30:
                        if current_size == 0:
                            logger.warning("\nStream never started or the model is currently offline (0 bytes captured).")
                        else:
                            logger.info("\nVideo stream stopped receiving data. Stopping recording.")
                        break
                        
                    try:
                        if await page.locator("text='Offline', text='is offline', .offline-screen").count() > 0:
                            logger.info("\nOffline screen detected. Stopping recording.")
                            break
                    except Exception:
                        pass
                    
            except Exception as e:
                logger.error(f"Navigation or recording interrupted: {str(e)}")

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
            logger.warning("No valid video chunks were captured. Aborting merge process.")
            return

        logger.info("Merging video and audio streams using FFmpeg...")
        
        model_name = profile_url.rstrip('/').split('/')[-1]
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        video_filename = f"{model_name}_{timestamp}.mp4"
        final_output_path = os.path.join(SCRIPT_DIR, video_filename)
        
        # ============ FIX: COMANDO FFmpeg CORRECTO ============
        # ANTES (INCORRECTO):
        # ffmpeg_cmd = ['ffmpeg', '-y']
        # for f in valid_files:
        #     ffmpeg_cmd.extend(['-i', f])
        # ffmpeg_cmd.extend(['-c', 'copy', final_output_path])
        #
        # PROBLEMA: '-c copy' es ambiguo y FFmpeg lo interpreta como:
        # - Copiar SOLO video, sin audio
        # - El resultado es un archivo truncado/corrupto
        
        # DESPUÉS (CORRECTO):
        # Separar explícitamente: -c:v copy (video) y -c:a copy (audio)
        
        # Detectar archivos de video y audio
        video_files = [f for f in valid_files if f.endswith('.mp4')]
        audio_files = [f for f in valid_files if f.endswith('.m4a')]
        
        if not video_files or not audio_files:
            logger.error(f"Missing streams. Video files: {len(video_files)}, Audio files: {len(audio_files)}")
            return
        
        # Comando FFmpeg mejorado
        ffmpeg_cmd = [
            'ffmpeg',
            '-y',  # Overwrite output file
            '-i', video_files[0],  # Video input
            '-i', audio_files[0],  # Audio input
            '-c:v', 'copy',        # Copy video codec (no re-encode)
            '-c:a', 'aac',         # Re-encode audio to AAC for compatibility
            '-map', '0:v:0',       # Map video from first input
            '-map', '1:a:0',       # Map audio from second input
            '-movflags', '+faststart',  # Move moov atom to beginning (YouTube optimization)
            final_output_path
        ]
        
        logger.info(f"Running FFmpeg: {' '.join(ffmpeg_cmd)}")
        
        try:
            result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True, timeout=3600)
            
            if result.returncode != 0:
                logger.error(f"FFmpeg failed with return code {result.returncode}")
                logger.error(f"STDERR:\n{result.stderr}")
                return
            
            # ============ VALIDACIÓN CRÍTICA ============
            if os.path.exists(final_output_path):
                final_size_mb = os.path.getsize(final_output_path) / (1024 * 1024)
                
                # Verificar que el archivo no está truncado
                if final_size_mb < 1:
                    logger.error(f"Output file is suspiciously small ({final_size_mb:.2f} MB). Merge likely failed.")
                    logger.error(f"FFmpeg STDOUT:\n{result.stdout}")
                    return
                
                # Verificar duración del video
                try:
                    ffprobe_cmd = [
                        'ffprobe',
                        '-v', 'error',
                        '-show_entries', 'format=duration',
                        '-of', 'default=noprint_wrappers=1:nokey=1',
                        final_output_path
                    ]
                    duration_result = subprocess.run(ffprobe_cmd, capture_output=True, text=True, timeout=30)
                    duration = float(duration_result.stdout.strip()) if duration_result.stdout.strip() else 0
                    
                    if duration < 10:  # Menos de 10 segundos = sospechoso
                        logger.error(f"Output video duration is too short ({duration:.2f}s). Merge likely failed.")
                        return
                    
                    logger.info(f"File successfully saved: {final_output_path}")
                    logger.info(f"  - Size: {final_size_mb:.2f} MB")
                    logger.info(f"  - Duration: {duration:.0f}s ({duration/60:.1f} min)")
                except Exception as e:
                    logger.warning(f"Could not verify video duration: {e}")
                    logger.info(f"File saved as {final_output_path} ({final_size_mb:.2f} MB)")
            else:
                logger.error("FFmpeg execution completed, but the output file is missing.")
                
        except subprocess.TimeoutExpired:
            logger.error("FFmpeg timeout after 1 hour. Video may be too large or system too slow.")
        except Exception as e:
            logger.error(f"Exception occurred while running FFmpeg: {e}")
            return

        # Guarantee cleanup of temporary chunks
        logger.info("Cleaning up temporary chunk files...")
        for f in valid_files:
            try: 
                if os.path.exists(f):
                    os.remove(f)
            except Exception as e: 
                logger.warning(f"Could not delete temporary file {f}: {e}")

if __name__ == "__main__":
    target_url = os.environ.get("STREAM_URL")
    
    if not target_url:
        logger.error("No STREAM_URL provided. Exiting.")
    else:
        asyncio.run(record_stream(target_url))
