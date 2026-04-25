import asyncio
from playwright.async_api import async_playwright
import subprocess
import os
import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

async def grabar_stream(url_perfil):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
        page = await context.new_page()

        archivos_crudos = {}
        import base64

        async def python_append_chunk(buffer_id, mime_type, b64_data):
            if buffer_id not in archivos_crudos:
                ext = "mp4" if "video" in mime_type else "m4a"
                nombre_tmp = os.path.join(SCRIPT_DIR, f"tmp_{buffer_id}.{ext}")
                archivos_crudos[buffer_id] = {"file": open(nombre_tmp, "wb"), "nombre": nombre_tmp, "tipo": ext}
            
            try:
                data = base64.b64decode(b64_data)
                archivos_crudos[buffer_id]["file"].write(data)
            except:
                pass

        await page.expose_function("python_append_chunk", python_append_chunk)

        js_hook = """/* mismo JS que ya tienes */"""
        await page.add_init_script(js_hook)

        await page.goto(url_perfil, wait_until="domcontentloaded")

        segundos_sin_datos = 0
        tamano_anterior = 0

        while True:
            await asyncio.sleep(5)
            tamano_actual = sum(
                os.path.getsize(info["nombre"])
                for info in archivos_crudos.values()
                if os.path.exists(info["nombre"])
            )

            if tamano_actual > tamano_anterior:
                segundos_sin_datos = 0
                tamano_anterior = tamano_actual
            else:
                segundos_sin_datos += 5

            if segundos_sin_datos >= 30:
                break

        await browser.close()

        archivos_validos = []
        for info in archivos_crudos.values():
            info["file"].close()
            if os.path.exists(info["nombre"]) and os.path.getsize(info["nombre"]) > 1000:
                archivos_validos.append(info["nombre"])

        if not archivos_validos:
            print("No hay datos")
            return

        nombre_modelo = url_perfil.rstrip('/').split('/')[-1]
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        archivo_final = os.path.join(SCRIPT_DIR, f"{nombre_modelo}_{timestamp}.mp4")

        comando = ['ffmpeg', '-y']
        for f in archivos_validos:
            comando.extend(['-i', f])

        comando.extend(['-c', 'copy', archivo_final])
        subprocess.run(comando)

        print(f"Archivo generado: {archivo_final}")


if __name__ == "__main__":
    url = os.getenv("STREAM_URL")
    if not url:
        raise ValueError("Debes definir STREAM_URL")

    asyncio.run(grabar_stream(url))
