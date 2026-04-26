import asyncio
from playwright.async_api import async_playwright
import subprocess
import os
import datetime

# Obtener la carpeta donde está guardado este script (la carpeta 'recorder')
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

async def grabar_stream(url_perfil):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        print(f"\nInyectando el 'hack' de MediaSource en el navegador...")

        # Aquí almacenaremos los archivos de video y audio temporalmente
        archivos_crudos = {}
        import base64

        async def python_append_chunk(buffer_id, mime_type, b64_data):
            if buffer_id not in archivos_crudos:
                # Determinar si es video o audio
                ext = "mp4" if "video" in mime_type else "m4a"
                nombre_tmp = os.path.join(SCRIPT_DIR, f"tmp_{buffer_id}.{ext}")
                archivos_crudos[buffer_id] = {"file": open(nombre_tmp, "wb"), "nombre": nombre_tmp, "tipo": ext}
                print(f"[+] Nuevo flujo detectado: {ext} (Mime: {mime_type[:30]}...)")
            
            try:
                data = base64.b64decode(b64_data)
                archivos_crudos[buffer_id]["file"].write(data)
            except Exception as e:
                pass

        await page.expose_function("python_append_chunk", python_append_chunk)

        # Inyectamos el mismo código exacto que usa la extensión de Chrome
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
                        } catch (e) {}
                    }
                    return originalAppendBuffer.apply(this, arguments);
                };
                return sourceBuffer;
            }
        };
        """
        await page.add_init_script(js_hook)

        print(f"Entrando a: {url_perfil} ...")
        try:
            await page.goto(url_perfil, wait_until="domcontentloaded", timeout=45000)
            
            try:
                btn = page.locator("button:has-text('I Agree'), button:has-text('Estoy de acuerdo')")
                if await btn.count() > 0:
                    await btn.first.click(timeout=2000)
            except Exception:
                pass
                
            await page.mouse.wheel(0, 500)
            
            # Buscamos el botón de Play si es que no inició solo
            try:
                play_btn = page.locator(".video-player-play-button, button:has-text('Play')")
                if await play_btn.count() > 0:
                    await play_btn.first.click(timeout=2000)
            except Exception:
                pass

            print(f"\n¡Grabando directamente desde la pantalla de forma continua!")
            print(f"El bot grabará hasta que el stream termine o la modelo se desconecte.")
            
            # Loop infinito monitoreando el estado del stream
            # Usamos un contador para asegurar que si no se reciben datos nuevos por un tiempo, se asume que terminó.
            # También revisaremos si aparece la pantalla de "Offline"
            segundos_sin_datos = 0
            tamano_anterior = 0
            
            while True:
                await asyncio.sleep(5) # Revisar cada 5 segundos
                
                # Calcular cuánto ha crecido el archivo para saber si sigue transmitiendo
                tamano_actual = sum(os.path.getsize(info["nombre"]) for info in archivos_crudos.values() if os.path.exists(info["nombre"]))
                
                if tamano_actual > tamano_anterior:
                    segundos_sin_datos = 0
                    tamano_anterior = tamano_actual
                else:
                    segundos_sin_datos += 5
                    
                # Si pasa 30 segundos sin recibir datos de video nuevos, o aparece texto de "offline"
                if segundos_sin_datos >= 30:
                    print("\n[!] El flujo de video se ha detenido. Finalizando grabación...")
                    break
                    
                # Verificar visualmente si la página indica que la modelo se fue
                try:
                    offline_text = page.locator("text='Offline', text='is offline', .offline-screen")
                    if await offline_text.count() > 0:
                        print("\n[!] Se detectó la pantalla de Offline. Finalizando grabación...")
                        break
                except Exception:
                    pass
                    
                # Mostrar progreso en MB
                mb_descargados = tamano_actual / (1024 * 1024)
                print(f"Grabando... Tamaño actual: {mb_descargados:.2f} MB", end="\r")
                
        except Exception as e:
            print(f"Aviso: Error en navegación: {e}")

        await browser.close()

        # Cerramos los archivos
        archivos_validos = []
        for buf_id, info in archivos_crudos.items():
            info["file"].close()
            # Si el archivo tiene peso, lo guardamos para unir
            if os.path.exists(info["nombre"]) and os.path.getsize(info["nombre"]) > 1000:
                archivos_validos.append(info["nombre"])
            else:
                try: os.remove(info["nombre"])
                except: pass

        if not archivos_validos:
            print("\nError: No se capturaron fragmentos de video. ¿La modelo está online?")
            return

        print("\nEnsamblando el video y el audio con FFmpeg...")
        
        # Generar nombre dinámico basado en la URL y la fecha
        nombre_modelo = url_perfil.rstrip('/').split('/')[-1]
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        nombre_video = f"{nombre_modelo}_{timestamp}.mp4"
        
        # Unimos el video y el audio en la carpeta recorder
        archivo_final = os.path.join(SCRIPT_DIR, nombre_video)
        comando_ffmpeg = ['ffmpeg', '-y']
        for f in archivos_validos:
            comando_ffmpeg.extend(['-i', f])
        
        comando_ffmpeg.extend(['-c', 'copy', archivo_final])
        
        subprocess.run(comando_ffmpeg, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Limpiamos los archivos temporales
        for f in archivos_validos:
            try: os.remove(f)
            except: pass

        if os.path.exists(archivo_final):
            print(f"\n¡ÉXITO! Archivo guardado como {archivo_final} ({os.path.getsize(archivo_final)} bytes).")
        else:
            print("\nError al ensamblar el video final.")

if __name__ == "__main__":
    # Obtener la URL desde las variables de entorno de GitHub Actions
    # Si no recibe ninguna, usará la de por defecto.
    url_objetivo = os.environ.get("STREAM_URL", "https://es.stripchat.com/Girls_hot_2")
    asyncio.run(grabar_stream(url_objetivo))
