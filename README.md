# pipeline.py

Descarga playlists de **Spotify** o **YouTube Music** con el audio original de YouTube (Opus, **sin recodificar**). También agrega título, artista, álbum, portada y ReplayGain.

> Este script es la base de **mousiki** (servidor en Go + app Android). El diseño está en [APP.md](APP.md).

## Requisitos

```sh
nix develop        # entra al shell con yt-dlp, ffmpeg, rsgain, spotdl y mutagen
```

Opcional: si pones un `cookies.txt` (formato Netscape) en el directorio actual o junto al script, yt-dlp lo usa para evitar bloqueos.

## Uso

```sh
python3 pipeline.py <ENTRADA> [opciones]
```

### Desde Spotify

Busca cada canción en YouTube Music y elige el resultado cuya duración coincide con la de Spotify. Si no lo encuentra, busca en YouTube.

```sh
python3 pipeline.py 37i9dQZF1DXcBWIGoYBM5M                          # id de playlist
python3 pipeline.py "https://open.spotify.com/playlist/37i9dQZF1D..." # URL
python3 pipeline.py mi_playlist.spotdl                                # archivo de spotdl
python3 pipeline.py mi_playlist.json                                  # JSON de la API de Spotify
```

### Desde YouTube Music / YouTube

Descarga directamente las canciones de la URL, sin búsqueda.

```sh
python3 pipeline.py "https://music.youtube.com/playlist?list=..."  # playlist
python3 pipeline.py "https://music.youtube.com/browse/MPREb_..."   # álbum
python3 pipeline.py "https://music.youtube.com/watch?v=..."        # una canción
```

## Opciones

| Opción | Qué hace | Default |
|---|---|---|
| `-n, --name NOMBRE` | Nombre de la carpeta de la playlist | id, nombre del archivo o título del álbum/playlist |
| `--base-dir DIR` | Carpeta base donde se guarda todo | `$SP_BASE_DIR` o `~/Music/spotify_playlists` |
| `--limit N` | Procesa solo las primeras N canciones | todas |
| `--duration-tolerance SEG` | Diferencia máxima de duración entre Spotify y YouTube (`0` = sin filtro). Solo aplica a Spotify | `10` |
| `--metadata-only` | Solo genera el `.txt` de metadata, sin descargar | — |

## Ejemplos

```sh
# Probar con una sola canción
python3 pipeline.py 37i9dQZF1DXcBWIGoYBM5M --limit 1

# Ponerle nombre a la carpeta
python3 pipeline.py 37i9dQZF1DXcBWIGoYBM5M -n rock

# Aceptar más diferencia de duración (radio edits, remasters)
python3 pipeline.py 37i9dQZF1DXcBWIGoYBM5M --duration-tolerance 20
```

## Dónde queda cada cosa

```
<base-dir>/
├── data/
│   ├── <nombre>.txt      # título|artista|álbum|cover_url
│   └── <nombre>.spotdl   # solo si la entrada fue un id/URL de Spotify
└── music/opus/<nombre>/
    └── Artista - Título.opus   # (.m4a si YouTube no tenía Opus)
```

Tags que se escriben: título, artista, álbum, portada, ReplayGain y el id del video de YouTube (`youtube_id`). **No** se escribe número de pista; si la fuente trae uno, se borra.

## Notas

- Si vuelves a correrlo, salta las canciones que ya existen: en Spotify por el nombre `Artista - Título` y en YouTube Music por el `youtube_id`. Para volver a bajar una, borra su archivo.
- Las canciones que no encuentra se listan al final con todas las búsquedas que probó.
- La calidad máxima sin YouTube Premium es Opus de ~130–160 kbps, igual en YouTube y YouTube Music.
