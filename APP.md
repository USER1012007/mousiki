# mousiki — Diseño

Un servicio en mi servidor descarga y organiza la música: mis "Me gusta" de YT Music, links de YT Music y, más adelante, códigos de Spotify. Una app Android se sincroniza con él cuando prendo Tailscale y deja los archivos donde **Musicolet** los reproduce sin conexión.

> Estado: **diseño acordado; `pipeline.py` probado con links de YT Music (canción y playlist)**. Siguiente paso: fase 1. Ver [README.md](README.md) para el uso del script.

---

## Decisiones tomadas

| Tema | Decisión |
|---|---|
| Servidor | Void Linux, Celeron, 4 GB de RAM, sin sesión gráfica, siempre encendido |
| Instalación | **Nix sobre Void** (el mismo flake que en desarrollo) + servicio **runit** |
| Red | Solo **Tailscale**. La API escucha únicamente en la IP de Tailscale. En el teléfono, Tailscale casi siempre está apagado. |
| Lenguajes | **Go** para el servidor (API, base de datos, cola, sondeo, `.m3u8`). **Python** solo para el pipeline de medios (yt-dlp, tags con mutagen, ytmusicapi). Se comunican por línea de comandos con JSON. |
| Fuentes | **YT Music primero.** Spotify queda para una fase posterior (el código ya existe en `pipeline.py`, pero no se conecta al servidor todavía). |
| Reproductor | **Musicolet**. La app no reproduce: solo gestiona y sincroniza. |
| Almacenamiento | **Biblioteca única**: cada canción existe una sola vez en disco. Las playlists son archivos `.m3u8`. |
| Fuente de verdad | **SQLite.** Los archivos de `library/` y los `.m3u8` se derivan de la base de datos y se pueden regenerar. |
| "Me gusta" | Solo los de **YT Music**, leídos por el servidor |
| Playlists | Viven en el **servidor y la app**. Si quiero algo en YT Music, lo hago a mano allá. |
| Links importados | Se descargan **una sola vez**, sin espejo ni actualización automática |
| Borrado | Si una canción ya no la usa nada, **se borra** (pasa antes por una papelera) |
| Calidad | Audio original de YouTube **sin recodificar** (Opus, ~130–160 kbps) |
| Música existente | No se migra nada: la biblioteca empieza vacía |
| Repo | Este repo, **`mousiki`** |

---

## Arquitectura

```
┌──────────────── Servidor (Void + Nix, runit) ─────────────────┐
│                                                               │
│  Servicio Go (un proceso)                                     │
│  ├─ API HTTP (solo IP de Tailscale, token)                    │
│  ├─ Importador: link → playlist + canciones "pendiente"       │
│  ├─ Worker: descarga canciones pendientes, 1 a la vez         │
│  ├─ Sondeo de likes cada N min                (fase 2)        │
│  ├─ Limpieza / papelera                       (fase 2)        │
│  ├─ Generador de .m3u8                                        │
│  └─ SQLite + registro de cambios                              │
│        │                                                      │
│        │ ejecuta (proceso externo, JSON por stdout)           │
│        ▼                                                      │
│  pipeline.py (Python)                                         │
│  ├─ resolve  → lista de canciones de un link                  │
│  ├─ fetch    → descarga + tags + ReplayGain de UNA canción    │
│  └─ likes    → "Me gusta" con ytmusicapi      (fase 2)        │
│        │                                                      │
│        ▼                                                      │
│  yt-dlp, ffmpeg, rsgain                                       │
│                                                               │
│  <data>/library/   <data>/playlists/*.m3u8                    │
└───────────────────────────┬───────────────────────────────────┘
                            │ Tailscale (cuando lo prendo)
┌───────────────────────────▼───────────────────────────────────┐
│  App Android                                                  │
│  ├─ Me gusta  (lista + "agregar a playlists")                 │
│  ├─ Playlists (vistas como carpetas)                          │
│  ├─ Descargas (pegar link / Compartir → cola)                 │
│  └─ Sincroniza archivos → Music/Mousiki/                      │
│                                       │                       │
│                Musicolet lee esa carpeta y reproduce          │
└───────────────────────────────────────────────────────────────┘
```

**Sincronización por consulta, no por envío.** Como el teléfono casi nunca está en la tailnet, el servidor no le manda nada. En su lugar, guarda cada cambio en un registro con un número consecutivo. Cuando la app vuelve a alcanzar el servidor, pide "cambios desde N" y aplica solo eso. Ya no se necesita Syncthing.

---

## Servidor (Go)

### Componentes (un proceso, un servicio runit)

| Componente | Responsabilidad |
|---|---|
| **API** | Endpoints HTTP que usa la app. Escucha solo en la IP de Tailscale. |
| **Importador** | Recibe un link (`POST /jobs`), ejecuta `pipeline.py resolve`, crea la playlist (si aplica) y registra cada canción como `pendiente`. No descarga nada. |
| **Worker** | Toma la siguiente canción `pendiente` y ejecuta `pipeline.py fetch`, **de una en una**, para no saturar el Celeron ni provocar bloqueos de YouTube. Reintenta los errores pasajeros con espera creciente. |
| **Sondeo de likes** (fase 2) | Cada N minutos ejecuta `pipeline.py likes`. Registra las canciones con like nuevo y marca las que lo perdieron. |
| **Limpieza** (fase 2) | Manda a la papelera las canciones que ya nada usa y vacía la papelera después de 7 días. |
| **Avisos** (fase 6) | **ntfy** cuando caducan las cookies o falla una descarga |

La cola de descargas **son las canciones en estado `pendiente`**: si el servidor se reinicia, sigue donde iba, y una canción que está en dos playlists se descarga una sola vez.

### Stack

| Pieza | Elección | Por qué |
|---|---|---|
| HTTP | `net/http` de la biblioteca estándar (rutas con método, Go ≥ 1.22) | Sin frameworks |
| SQLite | `modernc.org/sqlite` (Go puro, sin cgo), modo WAL | Compila fácil con Nix |
| Migraciones | Archivos `.sql` embebidos con `embed` | Sin herramientas extra |
| Procesos externos | `os/exec` con grupo de procesos propio y timeout | Se mata a yt-dlp y a ffmpeg juntos |
| Paquete | `buildGoModule` en el flake | Mismo flake para desarrollo y servidor |

### Contrato con `pipeline.py`

Go ejecuta `pipeline.py` como proceso externo. **stdout es solo JSON**; los mensajes de progreso van a stderr (Go los guarda en el log del trabajo).

| Comando | Salida (JSON) |
|---|---|
| `resolve <url>` | `{"name": ..., "kind": "song" \| "playlist" \| "album", "tracks": [{"youtube_id", "title", "duration"}]}` |
| `fetch <youtube_id> --out <dir>` | `{"youtube_id", "title", "artist", "album", "duration", "file", "codec"}` — `file` queda dentro de `--out` |
| `likes` (fase 2) | `{"tracks": [{"youtube_id", "title", "artist", "album", "duration", "cover_url"}]}` |

Códigos de salida: `0` bien, `1` error pasajero (se reintenta), `2` error permanente (video no disponible, sin resultados; no se reintenta).

**Go decide dónde vive cada archivo:** `fetch` escribe en un directorio temporal dentro de `<data>/` y Go lo mueve a su ruta final en `library/`. Así las reglas de nombres viven junto a la base de datos.

El modo actual del script (`pipeline.py <URL> [opciones]`) se mantiene para usarlo a mano.

### ¿Cuándo vive una canción?

Una canción se queda mientras se cumpla **al menos una** de estas condiciones:

1. Tiene like en YT Music.
2. Está en alguna playlist de la app.
3. Llegó como **link suelto** (una sola canción) y todavía no la he borrado a mano.

Si ninguna se cumple, va a la **papelera por 7 días** y después se borra del servidor y del teléfono. Ejemplo: le quito el like a una canción que también está en la playlist "Gym" → se queda.

### Qué pasa con cada tipo de entrada

| Entrada | Resultado |
|---|---|
| Like en YT Music | La canción entra a la biblioteca y aparece en **Me gusta** |
| Link de **playlist o álbum** (YT Music) | Crea una **playlist de la app** con el nombre original, como copia en ese momento (no se vuelve a sincronizar). Después la puedo editar. |
| Link de **una canción** | Entra a la biblioteca como "suelta". La app me deja agregarla a playlists. |
| Código de **Spotify** (fase 5) | Igual que una playlist de YT Music, pero cada canción se busca en YT Music filtrando por duración |

### Modelo de datos (SQLite)

| Tabla | Contenido |
|---|---|
| `songs` | `id` (entero, clave), `youtube_id` (único, vacío mientras no se resuelve), `spotify_id` (opcional), título, artista, álbum, duración, ruta, tamaño, estado (`pendiente` / `descargando` / `lista` / `falló` / `papelera`), intentos, último error, `suelta` |
| `likes` | Qué canciones tienen like ahora mismo |
| `playlists` | id, nombre, origen (creada en la app / importada de un link) |
| `playlist_items` | playlist, canción, posición |
| `jobs` | Entrada (link), estado, error, fechas. Solo representa la **importación**; las descargas se siguen en `songs`. |
| `changes` | Registro de cambios con número consecutivo, escrito **en la misma transacción** que el cambio. Es lo que consulta la app. |

La clave es un id interno, no `youtube_id`, porque una canción de Spotify existe (y puede estar en una playlist) antes de saber qué video le toca, y porque corregir una coincidencia equivocada cambia el `youtube_id`.

**Duplicados aceptados:** la misma canción puede llegar con dos videos distintos (el like apunta a la versión "Topic" y otra fuente trae el video oficial). Con `youtube_id` no se detectan. Por ahora se aceptan; si molestan, se agrega una opción "corregir coincidencia".

### API (borrador)

| Método y ruta | Para qué |
|---|---|
| `GET /health` | La app comprueba si el servidor está alcanzable |
| `GET /sync?since=N` | Cambios desde N: canciones, likes, playlists y trabajos. Si N es más viejo que el registro más antiguo, responde `full: true` con el estado completo. |
| `GET /songs/{id}/file` | Descarga del audio, con soporte de *Range* para reanudar |
| `GET /songs/{id}/cover` | Portada |
| `DELETE /songs/{id}` | Borrar una canción suelta |
| `GET /likes` | Lista de "Me gusta" |
| `GET /playlists` · `POST /playlists` | Listar y crear |
| `PATCH /playlists/{id}` · `DELETE /playlists/{id}` | Renombrar y borrar |
| `POST /songs/{id}/playlists` | `{agregar: [...], quitar: [...]}`, para meter una canción en varias playlists a la vez |
| `POST /jobs` | Encolar un link de YT Music |
| `GET /jobs` | Estado de las importaciones y de las descargas pendientes |

**Autenticación:** la API solo escucha en Tailscale, y además usa un token simple que se configura en la app.

### Fuentes externas

- **Descarga:** yt-dlp con el formato Opus original, como hace `pipeline.py` hoy.
- **Likes (fase 2):** **ytmusicapi**, que trae en una sola llamada los likes con álbum, artistas y portada cuadrada. yt-dlp con `list=LM` queda como respaldo.
- **Spotify (fase 5):** spotdl solo para leer la playlist; la búsqueda se hace en YT Music filtrando por duración.
- **Credenciales:** las cookies o los encabezados de YT Music se exportan desde el navegador de la lap y se copian al servidor (`tailscale file cp`). Cuando caducan, el servidor avisa a la app y por ntfy.
- **yt-dlp al día:** la versión de nixpkgs suele quedarse atrás. yt-dlp va como entrada propia del flake (release de GitHub) y una tarea semanal la actualiza sin mover todo nixpkgs.

### Archivos en el servidor

```
<data>/
├── mousiki.db
├── library/<Artista>/<Artista> - <Título> [<youtube_id>].opus
├── playlists/<Nombre>.m3u8        # rutas relativas: ../library/...
├── tmp/                           # descargas en curso (mismo disco que library/)
├── trash/                         # papelera de 7 días
└── secrets/                       # cookies / auth de YT Music (fuera de git)
```

El `[<youtube_id>]` en el nombre evita choques entre versiones con el mismo artista y título. Musicolet muestra los tags, no el nombre del archivo.

---

## App Android

### Pantallas

1. **Me gusta**
   - Lista sencilla: portada, título, artista e ícono de estado (en el teléfono, descargando o pendiente en el servidor).
   - Al tocar o mantener presionada una canción se abre una **ventana inferior** con casillas de las playlists existentes y el botón **"+ Nueva playlist"**.
   - Selección múltiple: varias canciones a varias playlists de una vez.
2. **Playlists**
   - Se ven como **carpetas**. Al entrar se ven sus canciones.
   - Quitar canciones, renombrar y borrar la playlist.
3. **Descargas**
   - Un campo para pegar un link de YT Music (y, desde la fase 5, un código o URL de Spotify).
   - La app aparece en el menú **"Compartir"** de Android, así que desde YT Music se comparte directo.
   - La cola con estado, progreso y errores.
4. **Estado / Ajustes**
   - Dirección del servidor y token, última sincronización, espacio usado y aviso de cookies caducadas.

### Sincronización

- Lo que hago sin conexión (agregar a playlists, pegar links) se guarda en una **cola local** y se envía cuando hay conexión.
- Al abrir la app o cuando cambia la red, consulta `GET /health`. Si responde:
  1. envía la cola local,
  2. pide `GET /sync?since=N` (si la respuesta trae `full: true`, reconstruye su estado),
  3. descarga los audios nuevos y borra los eliminados,
  4. reescribe los `.m3u8` que cambiaron.
- En caso de conflicto manda el servidor (la última escritura gana). Si una operación de la cola local ya no aplica (por ejemplo, la playlist se borró), el servidor la ignora y la app lo avisa.

### Archivos en el teléfono

```
Music/Mousiki/
├── library/<Artista>/<Artista> - <Título> [<youtube_id>].opus
└── playlists/<Nombre>.m3u8        # mismas rutas relativas que en el servidor
```

### Tecnología

**Kotlin + Jetpack Compose**: WorkManager para sincronizar, MediaStore o SAF para escribir en `Music/`, y un *intent* de compartir para recibir links.

---

## Repo

```
mousiki/
├── flake.nix / flake.lock   # entorno de desarrollo + paquetes del servidor
├── server/                  # servicio Go: api, db, worker, importador
├── pipeline/                # pipeline.py (Python) + pruebas
├── app/                     # proyecto Android
├── deploy/void/             # servicio runit + instalación de Nix en Void
└── docs/                    # este documento, API
```

`cookies.txt` y `secrets/` nunca entran a git.

---

## Fases

| Fase | Entregable | Cómo se prueba |
|---|---|---|
| **1. Núcleo del servidor** | Repo reorganizado; `pipeline.py` con `resolve` y `fetch`; servicio Go con SQLite, importador, worker, `POST /jobs`, `GET /jobs`, `GET /sync`, `GET /songs/{id}/file`, biblioteca única + `.m3u8`; paquete Nix + servicio runit | `curl` desde la lap con links de YT Music |
| **2. Likes y borrado** | `pipeline.py likes`, sondeo, reglas de vida de canciones, papelera | Dar y quitar likes en YT Music |
| **3. App mínima** | Conexión, lista de Me gusta, sincronización de archivos, sección de Descargas | Ver las canciones en Musicolet |
| **4. Playlists** | Ventana para agregar a playlists, crear, renombrar y borrar, vista de carpetas | — |
| **5. Spotify** | Importar códigos/URLs de Spotify (búsqueda en YT Music por duración) | Importar una playlist propia de Spotify |
| **6. Extras** | Menú "Compartir", ntfy, aviso de cookies | — |

### Fase 1 en pasos

1. **Repo:** commit inicial con `flake.lock`, `.gitignore` limpio, estructura `server/` + `pipeline/`, Go en el flake, pytest.
2. **Pipeline:** subcomandos `resolve` y `fetch` con el contrato JSON; pruebas de las funciones puras.
3. **Base de datos:** esquema y migraciones; registro `changes` en la misma transacción.
4. **Importador y worker:** canciones `pendiente` → `fetch` → mover a `library/`; reintentos con espera creciente; timeout que mata el grupo de procesos.
5. **API:** `/health`, `POST /jobs`, `GET /jobs`, `GET /sync`, `GET /songs/{id}/file`, token.
6. **`.m3u8`** generados desde la base de datos.
7. **Despliegue:** paquete en el flake, servicio runit en Void, prueba con `curl` desde la lap.

---

## Riesgos a validar temprano

- **Musicolet y los `.m3u8`:** hay que comprobar si los vuelve a leer automáticamente cuando cambian o si hay que importarlos a mano. Afecta el diseño de la fase 4.
- **Credenciales de YT Music en un servidor sin navegador:** cuánto duran y qué tan fácil es renovarlas.
- **yt-dlp:** YouTube lo rompe seguido. Por eso va como entrada propia del flake con actualización semanal.
- **Almacenamiento en Android:** permisos para escribir en `Music/` (los `.opus` y también los `.m3u8`) de forma que Musicolet vea los archivos.
- **Spotify (fase 5):** la API restringe las playlists editoriales y algorítmicas; probar spotdl con playlists propias antes de comprometerse.
- **Disco del servidor:** está al 84 %. Conviene vigilarlo y mostrar el espacio usado en la app.
