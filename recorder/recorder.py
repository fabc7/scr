import asyncio
from playwright.async_api import async_playwright
import subprocess
import os
import datetime
import shutil
import base64

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

async def record_stream(profile_url):
    if not shutil.which("ffmpeg"):
        print("[ERROR] FFmpeg is not installed on the system.")
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

            print("[INFO] Injecting MediaSource interceptor into the browser...")

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
                        print(f"[STREAM INFO]")
                        print(f"  MIME: {mime_type}")
                        print(f"  EXT: {ext}")
                    except Exception as e:
                        print(f"[ERROR] Failed to create temp file {tmp_name}: {e}")
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
                    print(f"\n[WARN] Failed to decode or write chunk: {e}")

            await page.expose_function("python_append_chunk", python_append_chunk)

            # ========== CAPTURAR LOGS DE CONSOLA DEL NAVEGADOR ==========
            console_logs = []
            
            def handle_console_msg(msg):
                log_entry = f"[{msg.type.upper()}] {msg.text}"
                console_logs.append(log_entry)
                if "1080p" in msg.text.lower() or "hls" in msg.text.lower() or "nivel" in msg.text.lower():
                    print(f"[BROWSER_CONSOLE] {log_entry}")
            
            page.on("console", handle_console_msg)
            # ========== FIN CAPTURA DE LOGS ==========

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

            print(f"[INFO] Navigating to: {profile_url}")
            
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

                # ========== INYECTAR FUERZA 1080P EN TIEMPO DE EJECUCIÓN ==========
                await asyncio.sleep(3)  # Esperar a que el player cargue
                
                force1080p_runtime = """
                (function() {
                  console.log('🎬 Fuerza 1080p en tiempo de ejecución...');
                  console.log('window.hls disponible:', !!window.hls);
                  console.log('window.HlsPlayer disponible:', !!window.HlsPlayer);
                  
                  // Buscar instancias de HLS en variables globales
                  let hlsInstance = null;
                  
                  // Intentar múltiples ubicaciones
                  if (window.hls) {
                    hlsInstance = window.hls;
                    console.log('✅ Encontrado window.hls');
                  } else if (window.player && window.player.hls) {
                    hlsInstance = window.player.hls;
                    console.log('✅ Encontrado window.player.hls');
                  } else if (window.videojs && window.videojs.getPlayer) {
                    const player = window.videojs.getPlayer('video');
                    if (player && player.hls) {
                      hlsInstance = player.hls;
                      console.log('✅ Encontrado en videojs player');
                    }
                  }
                  
                  // Interceptar manifestos m3u8 Y segmentos individuales
                  const originalFetch = window.fetch;
                  window.fetch = function(resource, config) {
                    let url = typeof resource === 'string' ? resource : resource.url;
                    
                    if (url) {
                      // Cambiar manifests 720p -> 1080p
                      if (url.includes('_720p.m3u8')) {
                        const newUrl = url.replace('_720p.m3u8', '_1080p.m3u8');
                        console.log('📡 Manifest 720p -> 1080p:', url.substring(url.length - 50));
                        url = newUrl;
                      }
                      
                      // Cambiar segmentos individuales 720p -> 1080p (.ts, .mp4, etc)
                      if (url.includes('_720p') && (url.includes('.ts') || url.includes('.m4s') || url.includes('.mp4'))) {
                        const newUrl = url.replace('_720p', '_1080p');
                        console.log('📹 Segmento 720p -> 1080p interceptado');
                        url = newUrl;
                      }
                    }
                    
                    return originalFetch(url, config);
                  };
                  window.fetch_intercepted = true;
                  console.log('✅ Fetch interceptado para manifests y segmentos');
                  
                  // Intentar forzar 1080p si HLS está disponible
                  async function force() {
                    for (let i = 0; i < 60; i++) {
                      if (hlsInstance && hlsInstance.levels && hlsInstance.levels.length > 0) {
                        const levels = hlsInstance.levels;
                        console.log('📊 Niveles encontrados:', levels.map(l => ({ height: l.height, name: l.name })));
                        
                        // Buscar 1080p
                        const level1080p = levels.findIndex(l => l.height === 1080 || (l.name && l.name.includes('1080')));
                        if (level1080p !== -1) {
                          hlsInstance.currentLevel = level1080p;
                          console.log('✅ 1080p APLICADO - currentLevel:', level1080p);
                          return;
                        }
                        
                        // Si no hay 1080p, seleccionar el más alto disponible
                        const maxLevel = levels.length - 1;
                        hlsInstance.currentLevel = maxLevel;
                        console.log('⚠️ 1080p no disponible, aplicado máximo:', levels[maxLevel].height, 'p');
                        return;
                      }
                      await new Promise(r => setTimeout(r, 500));
                    }
                    console.log('❌ HLS no disponible después de 30 segundos');
                  }
                  
                  force();
                })();
                """
                
                try:
                    result = await page.evaluate(force1080p_runtime)
                    print("[INFO] 1080p quality injection attempted")
                except Exception as e:
                    print(f"[WARN] Could not inject 1080p script: {e}")
                
                # Esperar a que la inyección haga efecto
                await asyncio.sleep(2)
                # ========== FIN ==========

                print("[INFO] Recording started. Target limit: 15 GB or stream end.")
                
                seconds_without_data = 0
                previous_size = 0
                # MAX_BYTES = 30 * 1024 * 1024 * 1024 # 30 GB
                MAX_BYTES = 20 * 1024 * 1024 # Test 20 mb
                
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
                    print(f"Status: Recording... Current size: {downloaded_mb:.2f} MB 30000 MB", end="\r")

                    if current_size >= MAX_BYTES:
                        print(f"\n\n[INFO] Target size size 15000 MB reached ({downloaded_mb:.2f} MB). Stopping recording.")
                        break
                        
                    if seconds_without_data >= 30:
                        if current_size == 0:
                            print("\n\n[WARN] Stream never started or the model is currently offline (0 bytes captured).")
                        else:
                            print("\n\n[INFO] Video stream stopped receiving data. Stopping recording.")
                        break
                        
                    try:
                        if await page.locator("text='Offline', text='is offline', .offline-screen").count() > 0:
                            print("\n\n[INFO] Offline screen detected. Stopping recording.")
                            break
                    except Exception:
                        pass
                    
            except Exception as e:
                print(f"\n[ERROR] Navigation or recording interrupted: {str(e)}")

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

        print("\n[INFO] Merging video and audio streams using FFmpeg...")
        
        model_name = profile_url.rstrip('/').split('/')[-1]
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        video_filename = f"{model_name}_{timestamp}.mkv"
        final_output_path = os.path.join(SCRIPT_DIR, video_filename)

        largest_file = max(valid_files, key=os.path.getsize)

        print(f"[INFO] Using stream file: {largest_file}")

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
                print(f"[INFO] Stream video resolution: {resolution}")
            else:
                print("[WARN] Could not determine stream video resolution")
        except Exception as e:
            print(f"[WARN] Error getting resolution: {e}")

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
            '-vf', 'scale=1920:1080:flags=lanczos',
            '-c:v', 'libx264',
            '-preset', 'slow',
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
                print(f"\n[ERROR] FFmpeg failed to merge files. STDERR details:\n{result.stderr}")
            elif os.path.exists(final_output_path):
                final_size_mb = os.path.getsize(final_output_path) / (1024 * 1024)
                print(f"\n[SUCCESS] File successfully saved as {final_output_path} ({final_size_mb:.2f} MB).")
            else:
                print("\n[ERROR] FFmpeg execution completed, but the output file is missing.")
                
        except Exception as e:
             print(f"\n[ERROR] Exception occurred while running FFmpeg: {e}")

        # Guarantee cleanup of temporary chunks
        print("[INFO] Cleaning up temporary chunk files...")
        for f in valid_files:
            try: 
                if os.path.exists(f):
                    os.remove(f)
            except Exception as e: 
                print(f"[WARN] Could not delete temporary file {f}: {e}")

if __name__ == "__main__":
    target_url = os.environ.get("STREAM_URL")
    
    if not target_url:
        print("[ERROR] No STREAM_URL provided. Exiting.")
    else:
        asyncio.run(record_stream(target_url))
