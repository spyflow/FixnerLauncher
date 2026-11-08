# FixnerLauncher

**FixnerLauncher** es un lanzador ligero, rápido y completamente aislado para **Minecraft: Java Edition**, desarrollado en Python con **PySide6 (Qt6)** por **Spyflow**.

Permite gestionar e iniciar instalaciones **Vanilla**, **OptiFine** y **Sodium (Fabric)** sin interferir con la carpeta `.minecraft` oficial, gestionando automáticamente el runtime de Java adecuado para cada generación del juego (desde Java 8 hasta Java 25).

---

## Características principales

- **Directorio 100 % aislado (`%APPDATA%\.fixnerlauncher`)**:
  No modifica ni ensucia `%APPDATA%\.minecraft`. Tus mundos, capturas, opciones y servidores se comparten entre versiones dentro del entorno de FixnerLauncher.
- **Tres variantes integradas**:
  - **Vanilla**: Descarga directa desde los manifiestos oficiales de Mojang.
  - **OptiFine**: Descarga e instalación desatendida (incluyendo soporte nativo para versiones clásicas como `1.8.9` y modernas).
  - **Sodium (Fabric)**: Instala automáticamente el perfil de **Fabric Loader**, sus librerías Maven y el mod **Sodium** desde Modrinth en una carpeta de mods exclusiva por versión (`mods/<version_mc>/`), permitiendo coexistir múltiples versiones de Minecraft con mods sin conflictos (`-Dfabric.addMods`).
- **Gestión automática de Java (Mojang Runtimes)**:
  Detecta el componente exacto requerido por cada versión (`jre-legacy` / Java 8, `java-runtime-alpha` / Java 16, `java-runtime-gamma` / Java 17, `java-runtime-delta` / Java 21 y `java-runtime-epsilon` / Java 25) y lo descarga automáticamente si no está presente.
- **Autenticación Microsoft (Online) y Modo Offline**:
  - Inicio de sesión oficial mediante flujo OAuth2 -> Xbox Live -> XSTS -> Minecraft Services.
  - **Seguridad de credenciales**: Tanto el `access_token` como el `refresh_token` se almacenan cifrados en el **Administrador de credenciales de Windows** (`keyring` / DPAPI), nunca en texto plano en disco.
  - Renovación automática de sesión cuando el JWT expira y soporte inmediato para perfiles **Offline**.
- **Bandeja del sistema y control de ciclo de vida**:
  - Liberación de memoria de la interfaz al iniciar el juego.
  - Reapertura automática opcional al cerrar Minecraft y captura detallada de reportes en caso de cierre inesperado (*crash report*).

---

## Estructura del proyecto

```text
FixnerLauncher/
├── assets/                 # Iconos (.ico) y tipografía (Minecraft.ttf)
├── config/
│   └── settings.py         # Rutas aisladas, versión única y constantes globales
├── core/
│   ├── auth_manager.py     # Autenticación OAuth2 Microsoft, keyring y perfiles offline
│   ├── java_manager.py     # Selección e instalación de runtimes Java de Mojang
│   ├── minecraft_manager.py# Instalación y lanzamiento de versiones y librerías
│   ├── optifine_manager.py # Catálogo, extracción e instalación de OptiFine
│   ├── runner.py           # Fachada de ejecución para Vanilla, OptiFine y Sodium
│   └── sodium_manager.py   # Integración con Fabric Meta API y Modrinth (Sodium)
├── src/
│   ├── controller.py       # Controlador principal, System Tray y monitor de proceso
│   ├── ui/
│   │   ├── main_window.py  # Ventana principal y diálogos Qt6
│   │   └── styles.qss      # Hoja de estilos visual del launcher
│   └── workers/
│       └── tasks.py        # Hilos de trabajo asíncronos (QThread)
├── utils/
│   └── logger.py           # Configuración de logs rotativos
├── VERSION                 # Archivo único de versión (ej. 0.3.1)
├── installer.iss           # Script de empaquetado para Inno Setup
├── LICENSE.txt             # Términos de uso y avisos legales
└── main.py                 # Punto de entrada (GUI y CLI)
```

---

## Requisitos e instalación (Desarrollo)

1. **Python 3.11+** en Windows.
2. Instalar dependencias:
   ```powershell
   pip install -r requirements.txt
   ```
3. Ejecutar la interfaz gráfica:
   ```powershell
   python main.py
   ```

---

## Uso por línea de comandos (CLI)

Además de la interfaz gráfica (`python main.py` o `python main.py gui`), FixnerLauncher incluye comandos directos:

```powershell
# Ver versión actual del launcher
python main.py --version

# Listar versiones release de Minecraft disponibles
python main.py list

# Instalar una versión Vanilla
python main.py install 1.20.4

# Listar e instalar OptiFine para una versión base
python main.py optifine-list 1.8.9
python main.py optifine-install 1.8.9 --index 0

# Listar e instalar Sodium (Fabric) aislado en mods/<version>/
python main.py sodium-list 1.20.4
python main.py sodium-install 1.20.4 --index 0

# Lanzar una versión instalada (Vanilla, OptiFine o Sodium)
python main.py launch 1.20.4 --variant Sodium --username Player --memory 4096
```

---

## Compilación y creación del instalador

1. **Generar ejecutable con PyInstaller**:
   ```powershell
   pyinstaller FixnerLauncher.spec --noconfirm
   ```
   El ejecutable autónomo se generará en `dist/FixnerLauncher/FixnerLauncher.exe`.

2. **Generar instalador con Inno Setup**:
   ```powershell
   "C:\Program Files\Inno Setup 7\ISCC.exe" installer.iss
   ```
   El instalador se generará en `installer_output/FixnerLauncher_Setup.exe` tomando automáticamente la versión definida en el archivo [`VERSION`](VERSION).

---

## Aviso legal

* **Autor**: Spyflow.
* **No afiliación**: Este proyecto es una herramienta independiente y **no es un producto oficial de Minecraft**, ni está aprobado ni asociado con **Mojang AB** ni **Microsoft Corporation**.
