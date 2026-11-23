#!/usr/bin/env python3
"""
Pipeline completo:
1. Fuente:
   - Spotify (id/URL de playlist, .spotdl o JSON): se busca cada canción en
     YouTube Music (resultados "songs") filtrando por duración, con fallback a YouTube.
   - YouTube Music / YouTube (URL de playlist, álbum o canción): se descarga directo.
2. Descarga el stream de audio original SIN recodificar (Opus de YouTube).
3. Escribe tags + cover con mutagen y ReplayGain con rsgain.
"""

from __future__ import annotations

import argparse
import base64
import functools
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path

try:
    from mutagen import File as MutagenFile
    from mutagen.flac import Picture
    from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
    from mutagen.oggopus import OggOpus
    from mutagen.oggvorbis import OggVorbis
except ImportError:
    print("Falta mutagen (python3Packages.mutagen). Usa `nix develop`.", file=sys.stderr)
    sys.exit(1)


# Extensiones que deja `yt-dlp -x` al extraer sin recodificar:
# opus (webm 251) -> .opus, aac (m4a 140) -> .m4a, vorbis -> .ogg
AUDIO_EXTS = (".opus", ".m4a", ".ogg")

# Prefiere Opus (mejor calidad por bitrate en YouTube); si no hay, lo mejor disponible.
FORMAT_SELECTOR = "bestaudio[acodec=opus]/bestaudio"

# Candidatos que se revisan por búsqueda antes de rendirse (se elige el primero
# que pase el filtro de duración).
SEARCH_CANDIDATES = 5

# Tiempo máximo (segundos) de cada llamada a yt-dlp. Si se pasa, se mata el proceso
# (y sus hijos, como ffmpeg) y se cuenta como fallo, para no bloquear la cola.
YT_DLP_TIMEOUT = 600

# Tag con el id del video de YouTube (sirve para saltar canciones ya descargadas).
YOUTUBE_ID_VORBIS = "youtube_id"
YOUTUBE_ID_MP4 = "----:com.apple.iTunes:YOUTUBE_ID"

# Tags de número de pista/disco que se eliminan si la fuente los trae.
NUMBER_TAGS_VORBIS = ("tracknumber", "tracktotal", "totaltracks", "discnumber", "disctotal")
NUMBER_TAGS_MP4 = ("trkn", "disk")


@dataclass(frozen=True)
class Track:
    title: str
    artist: str
    album: str
    cover_url: str
    duration: int | None = None  # segundos
    url: str = ""  # URL directa de YouTube (modo YouTube Music); vacío = buscar


def is_youtube_url(value: str) -> bool:
    return re.search(r"(^|//|\.)(youtube\.com|youtu\.be)/", value) is not None


def run(
    cmd: list[str], *, quiet: bool = False, timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    stdout = subprocess.PIPE if quiet else None
    stderr = subprocess.PIPE if quiet else None
    return subprocess.run(
        cmd,
        check=True,
        text=True,
        stdout=stdout,
        stderr=stderr,
        timeout=timeout,
    )


def run_killable(cmd: list[str], timeout: float) -> bool:
    """Corre `cmd` en su propio grupo de procesos. Devuelve False si se pasó del timeout.

    Al vencer el timeout (o con Ctrl+C) mata el grupo entero, no solo el proceso
    principal: yt-dlp lanza ffmpeg y no debe quedar huérfano.
    """
    proc = subprocess.Popen(cmd, text=True, start_new_session=True)
    try:
        proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        return False
    except BaseException:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        raise


def require_commands(commands: list[str]) -> None:
    missing = [cmd for cmd in commands if shutil.which(cmd) is None]
    if missing:
        print(f"Faltan dependencias: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)


def playlist_url(value: str) -> str:
    if value.startswith(("http://", "https://")):
        return value
    return f"https://open.spotify.com/playlist/{value}"


def playlist_id(value: str) -> str:
    match = re.search(r"playlist/([^?/#]+)", value)
    if match:
        return match.group(1)
    return value


def sanitize_filename(value: str) -> str:
    value = re.sub(r'[/\\:*?"<>|]', "_", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value or "unknown"


def normalize_text(value: str) -> str:
    value = value.casefold()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def search_queries(track: Track) -> list[str]:
    clean_title = re.sub(r"[^\w\s-]", " ", track.title, flags=re.UNICODE)
    clean_artist = re.sub(r"[^\w\s-]", " ", track.artist, flags=re.UNICODE)
    clean_title = re.sub(r"\s+", " ", clean_title).strip()
    clean_artist = re.sub(r"\s+", " ", clean_artist).strip()

    candidates = [
        f"{track.artist} {track.title}",
        f"{track.title} {track.artist}",
        f"{clean_artist} {clean_title}",
        f"{clean_artist} {clean_title} audio",
        f"{clean_artist} {clean_title} official",
        normalize_text(f"{track.artist} {track.title}"),
        normalize_text(f"{track.title} {track.artist}"),
    ]

    seen: set[str] = set()
    queries: list[str] = []
    for query in candidates:
        query = re.sub(r"\s+", " ", query).strip()
        if query and query not in seen:
            seen.add(query)
            queries.append(query)

    return queries


@functools.cache
def cookie_args() -> list[str]:
    # Se resuelve una sola vez: primero el directorio actual, luego junto al script.
    for cookie_file in (Path("cookies.txt"), Path(__file__).resolve().parent / "cookies.txt"):
        if cookie_file.is_file():
            return ["--cookies", str(cookie_file)]
    print("[!] No se encontró cookies.txt. Algunas descargas pueden fallar.", file=sys.stderr)
    return []


def extract_tracks_from_json(json_file: Path) -> list[Track]:
    """JSON crudo de la API de Spotify (lo que antes se filtraba con jq)."""
    with json_file.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    items = payload.get("items", []) if isinstance(payload, dict) else []
    tracks: list[Track] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        track = item.get("track") or item.get("item")
        if not isinstance(track, dict) or track.get("type") != "track":
            continue

        album = track.get("album") if isinstance(track.get("album"), dict) else {}
        images = album.get("images") or []
        cover_url = images[0].get("url", "") if images and isinstance(images[0], dict) else ""

        title = track.get("name") or ""
        artist = artist_names(track.get("artists"))
        duration_ms = track.get("duration_ms")
        duration = duration_ms // 1000 if isinstance(duration_ms, int) else None
        if title and artist:
            tracks.append(Track(title, artist, album.get("name") or "", cover_url or "", duration))

    return tracks


def artist_names(value: object) -> str:
    if isinstance(value, list):
        names: list[str] = []
        for item in value:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict):
                name = item.get("name")
                if isinstance(name, str):
                    names.append(name)
        return ", ".join(name for name in names if name)

    if isinstance(value, str):
        return value

    return ""


def first_string(mapping: dict[str, object], keys: list[str]) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str):
            return value
    return ""


def extract_spotdl_items(payload: object) -> list[dict[str, object]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    if isinstance(payload, dict):
        for key in ("songs", "tracks", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]

    return []


def extract_tracks_from_spotdl(spotdl_file: Path) -> list[Track]:
    with spotdl_file.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    tracks: list[Track] = []
    for item in extract_spotdl_items(payload):
        title = first_string(item, ["name", "title", "song_name"])
        artist = artist_names(item.get("artists") or item.get("artist"))
        album = first_string(item, ["album_name", "album", "album_title"])
        cover_url = first_string(item, ["cover_url", "cover", "thumbnail", "image"])
        duration = item.get("duration")
        duration = int(duration) if isinstance(duration, (int, float)) and duration > 0 else None

        if title and artist:
            tracks.append(Track(title, artist, album, cover_url, duration))

    return tracks


def extract_tracks_from_youtube(url: str) -> tuple[str, list[Track]]:
    """Lista rápida (flat) de una playlist/álbum/canción de YouTube Music o YouTube.

    Solo trae id, título y duración; artista/álbum se completan al descargar.
    """
    result = run(
        ["yt-dlp", *cookie_args(), "--flat-playlist", "-J", url],
        quiet=True,
        timeout=YT_DLP_TIMEOUT,
    )
    payload = json.loads(result.stdout)

    if payload.get("_type") == "playlist":
        entries = payload.get("entries") or []
        name = re.sub(r"^(Album|Single|EP) - ", "", payload.get("title") or "")
    else:  # una sola canción
        entries = [payload]
        name = ""

    tracks: list[Track] = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        duration = entry.get("duration")
        tracks.append(
            Track(
                title=entry.get("title") or entry["id"],
                artist="",
                album="",
                cover_url="",
                duration=int(duration) if isinstance(duration, (int, float)) else None,
                url=f"https://www.youtube.com/watch?v={entry['id']}",
            )
        )

    return name, tracks


def save_playlist_with_spotdl(playlist: str, spotdl_file: Path) -> None:
    spotdl_file.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            "spotdl",
            "save",
            playlist_url(playlist),
            "--save-file",
            str(spotdl_file),
        ]
    )


def write_metadata_file(tracks: list[Track], metadata_file: Path) -> None:
    metadata_file.parent.mkdir(parents=True, exist_ok=True)
    with metadata_file.open("w", encoding="utf-8", newline="") as handle:
        for track in tracks:
            handle.write(
                f"{track.title}|{track.artist}|{track.album}|{track.cover_url}\n"
            )


def fetch_cover(track: Track) -> bytes | None:
    if not track.cover_url:
        return None

    try:
        with urllib.request.urlopen(track.cover_url, timeout=20) as response:
            data = response.read()
    except Exception:
        return None

    return data or None


def cover_mime(data: bytes) -> str:
    return "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"


def tag_audio(path: Path, track: Track, cover: bytes | None, video_id: str = "") -> None:
    """Escribe tags y portada sin tocar el stream de audio.

    No se escribe número de pista; si la fuente traía uno, se elimina.
    """
    suffix = path.suffix.lower()

    if suffix in {".opus", ".ogg"}:
        audio = OggOpus(path) if suffix == ".opus" else OggVorbis(path)
        for key in NUMBER_TAGS_VORBIS:
            audio.pop(key, None)
        if video_id:
            audio[YOUTUBE_ID_VORBIS] = video_id
        audio["title"] = track.title
        audio["artist"] = track.artist
        if track.album:
            audio["album"] = track.album
        if cover:
            picture = Picture()
            picture.type = 3  # front cover
            picture.mime = cover_mime(cover)
            picture.data = cover
            audio["metadata_block_picture"] = base64.b64encode(picture.write()).decode("ascii")
        audio.save()
        return

    if suffix == ".m4a":
        audio = MP4(path)
        for key in NUMBER_TAGS_MP4:
            audio.pop(key, None)
        if video_id:
            audio[YOUTUBE_ID_MP4] = [MP4FreeForm(video_id.encode("utf-8"))]
        audio["\xa9nam"] = track.title
        audio["\xa9ART"] = track.artist
        if track.album:
            audio["\xa9alb"] = track.album
        if cover:
            image_format = (
                MP4Cover.FORMAT_PNG if cover_mime(cover) == "image/png" else MP4Cover.FORMAT_JPEG
            )
            audio["covr"] = [MP4Cover(cover, imageformat=image_format)]
        audio.save()
        return

    raise ValueError(f"Formato no soportado para tags: {path.suffix}")


def youtube_video_id(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc.endswith("youtu.be"):
        return parsed.path.lstrip("/")
    return (urllib.parse.parse_qs(parsed.query).get("v") or [""])[0]


def read_youtube_id(path: Path) -> str:
    try:
        if path.suffix.lower() == ".m4a":
            values = MP4(path).get(YOUTUBE_ID_MP4) or []
            return bytes(values[0]).decode("utf-8") if values else ""
        audio = OggOpus(path) if path.suffix.lower() == ".opus" else OggVorbis(path)
        values = audio.get(YOUTUBE_ID_VORBIS) or []
        return values[0] if values else ""
    except Exception:
        return ""


def index_youtube_ids(out_dir: Path) -> dict[str, Path]:
    """id de YouTube -> archivo ya descargado (leído de los tags)."""
    known: dict[str, Path] = {}
    if out_dir.is_dir():
        for path in out_dir.iterdir():
            if path.suffix.lower() in AUDIO_EXTS:
                video_id = read_youtube_id(path)
                if video_id:
                    known[video_id] = path
    return known


def find_existing(out_dir: Path, track: Track, known_ids: dict[str, Path]) -> Path | None:
    if track.url:
        # Modo YouTube: el artista/título final se conocen hasta descargar,
        # así que se identifica por el id de YouTube guardado en los tags.
        return known_ids.get(youtube_video_id(track.url))

    pattern = glob.escape(track_stem(track)) + ".*"
    for candidate in sorted(out_dir.glob(pattern)):
        if candidate.suffix.lower() in AUDIO_EXTS and candidate.stat().st_size > 0:
            return candidate
    return None


def track_stem(track: Track) -> str:
    return f"{sanitize_filename(track.artist)} - {sanitize_filename(track.title)}"


def track_from_file(path: Path, fallback: Track) -> Track:
    """Lee los tags de un archivo ya descargado (para reescribir la metadata)."""
    try:
        audio = MutagenFile(path, easy=True)
    except Exception:
        audio = None
    tags = audio.tags if audio is not None and audio.tags is not None else {}

    def tag(key: str) -> str:
        values = tags.get(key) or []
        return values[0] if values else ""

    return replace(
        fallback,
        title=tag("title") or fallback.title,
        artist=tag("artist") or fallback.artist,
        album=tag("album") or fallback.album,
    )


def track_from_info(info: dict[str, object], fallback: Track) -> Track:
    """Metadata de YouTube Music (track/artists/album) desde el .info.json de yt-dlp."""
    artist = artist_names(info.get("artists"))
    if not artist:
        artist = first_string(info, ["artist", "creator"])
    if not artist:
        artist = re.sub(r" - Topic$", "", first_string(info, ["uploader", "channel"]))

    duration = info.get("duration")
    return replace(
        fallback,
        title=first_string(info, ["track", "title"]) or fallback.title,
        artist=artist or fallback.artist or "unknown",
        album=first_string(info, ["album"]) or fallback.album,
        duration=int(duration) if isinstance(duration, (int, float)) else fallback.duration,
    )


def duration_filter(track: Track, tolerance: int) -> str | None:
    if not track.duration or tolerance <= 0:
        return None
    low = max(track.duration - tolerance, 0)
    high = track.duration + tolerance
    return f"duration >= {low} & duration <= {high}"


def search_targets(track: Track) -> list[tuple[str, str]]:
    """(fuente, target de yt-dlp). Primero YouTube Music (solo canciones), luego YouTube."""
    queries = search_queries(track)
    targets = [
        ("YT Music", f"https://music.youtube.com/search?q={urllib.parse.quote_plus(q)}#songs")
        for q in queries[:3]
    ]
    targets += [("YouTube", f"ytsearch{SEARCH_CANDIDATES}:{q}") for q in queries]
    return targets


def yt_dlp_fetch(
    target: str,
    tmp_dir: Path,
    *,
    match_filter: str | None = None,
    thumbnail: bool = False,
) -> tuple[Path | None, dict[str, object]]:
    """Descarga UN resultado de `target` sin recodificar. Devuelve (audio, info.json)."""
    for old_file in tmp_dir.iterdir():
        if old_file.is_file():
            old_file.unlink()

    cmd = [
        "yt-dlp",
        *cookie_args(),
        "-P",
        str(tmp_dir),
        "-o",
        "source.%(ext)s",
        "-f",
        FORMAT_SELECTOR,
        # -x sin --audio-format = "best": copia el stream (opus -> .opus, aac -> .m4a)
        "-x",
        "--no-playlist",
        "--no-write-playlist-metafiles",
        "--write-info-json",
        # En búsquedas: revisa hasta N candidatos y se queda con el primero que pase el filtro.
        "--playlist-items",
        f"1:{SEARCH_CANDIDATES}",
        "--max-downloads",
        "1",
    ]
    if match_filter:
        cmd += ["--match-filter", match_filter]
    if thumbnail:
        cmd += ["--write-thumbnail", "--convert-thumbnails", "jpg"]
    cmd.append(target)

    # No se revisa el código de salida: yt-dlp sale con 101 al llegar a --max-downloads y
    # con 1 si algún candidato falló aunque otro sí se descargara. Lo que cuenta es el archivo.
    if not run_killable(cmd, YT_DLP_TIMEOUT):
        # Puede haber quedado un archivo a medio escribir: no se usa.
        print(f"[!] yt-dlp tardó más de {YT_DLP_TIMEOUT}s, se canceló: {target}", file=sys.stderr)
        return None, {}

    audio = next(
        (p for p in sorted(tmp_dir.iterdir()) if p.is_file() and p.suffix.lower() in AUDIO_EXTS),
        None,
    )
    info: dict[str, object] = {}
    info_file = tmp_dir / "source.info.json"
    if audio and info_file.is_file():
        try:
            info = json.loads(info_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            info = {}
    return audio, info


def square_cover(image: Path) -> bytes | None:
    """Recorta la miniatura 16:9 de YouTube al centro cuadrado (donde va la portada)."""
    output = image.with_name("cover-square.jpg")
    try:
        run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-i",
                str(image),
                "-vf",
                "crop='min(iw,ih)':'min(iw,ih)'",
                "-q:v",
                "2",
                str(output),
            ]
        )
    except subprocess.CalledProcessError:
        return None
    return output.read_bytes() if output.is_file() and output.stat().st_size > 0 else None


def process_track(
    track: Track, out_dir: Path, known_ids: dict[str, Path], tolerance: int
) -> Track | None:
    existing = find_existing(out_dir, track, known_ids)
    if existing:
        print(f"[=] Ya existe: {existing}")
        return track_from_file(existing, track)

    with tempfile.TemporaryDirectory(prefix="sp-dl-") as tmp:
        tmp_dir = Path(tmp)

        if track.url:
            print(f"[↓] Descargando: {track.title}")
            audio, info = yt_dlp_fetch(track.url, tmp_dir, thumbnail=True)
            if audio is None:
                print(f"[!] No se pudo descargar: {track.url}", file=sys.stderr)
                return None
            track = track_from_info(info, track)
            thumbnail = tmp_dir / "source.jpg"
            cover = square_cover(thumbnail) if thumbnail.is_file() else None
        else:
            audio = None
            match_filter = duration_filter(track, tolerance)
            tried: list[str] = []
            for source, target in search_targets(track):
                tried.append(f"{source}: {target}")
                print(f"[↓] Buscando en {source}: {target}")
                audio, info = yt_dlp_fetch(target, tmp_dir, match_filter=match_filter)
                if audio:
                    chosen = track_from_info(info, track)
                    print(
                        f"[♪] Elegido ({source}): {chosen.artist} - {chosen.title}"
                        f" [{chosen.duration}s vs {track.duration}s en Spotify]"
                    )
                    break

            if audio is None:
                print(
                    f"[!] Sin resultado para: {track.artist} - {track.title}"
                    + (f" (filtro: {match_filter})" if match_filter else ""),
                    file=sys.stderr,
                )
                print("    Búsquedas probadas:", file=sys.stderr)
                for item in tried:
                    print(f"    - {item}", file=sys.stderr)
                return None
            cover = fetch_cover(track)

        out_dir.mkdir(parents=True, exist_ok=True)
        output = out_dir / f"{track_stem(track)}{audio.suffix.lower()}"
        shutil.move(audio, output)

    # Si faltó el .info.json, en modo YouTube el id se saca de la URL; sin él la
    # canción se volvería a descargar en cada corrida.
    video_id = first_string(info, ["id"]) or (youtube_video_id(track.url) if track.url else "")
    if video_id:
        # Para que un video repetido en la misma playlist no se descargue otra vez.
        known_ids[video_id] = output

    try:
        tag_audio(output, track, cover, video_id)
    except Exception as error:
        print(f"[!] No se pudieron escribir tags en {output.name}: {error}", file=sys.stderr)

    try:
        run(["rsgain", "custom", "-s", "i", "-l", "-18", "-o", "t", str(output)])
    except subprocess.CalledProcessError:
        print(f"[!] ReplayGain falló: {output}", file=sys.stderr)

    print(f"[✓] {output.name}")
    return track


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Descarga playlists de Spotify (buscando en YouTube Music) o de YouTube Music "
            "directamente, sin recodificar el audio."
        )
    )
    parser.add_argument(
        "playlist",
        help=(
            "Spotify: código/id o URL de playlist, o archivo JSON/.spotdl. "
            "YouTube Music/YouTube: URL de playlist, álbum o canción."
        ),
    )
    parser.add_argument(
        "-n",
        "--name",
        help=(
            "Nombre de playlist/subcarpeta. Por defecto: el código, el nombre del archivo "
            "o el título de la playlist/álbum de YouTube."
        ),
    )
    parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path(os.environ.get("SP_BASE_DIR", "~/Music/spotify_playlists")).expanduser(),
        help="Directorio base. Default: $SP_BASE_DIR o ~/Music/spotify_playlists",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Procesa solo las primeras N canciones útiles.",
    )
    parser.add_argument(
        "--duration-tolerance",
        type=int,
        default=10,
        metavar="SEG",
        help=(
            "Solo para Spotify: diferencia máxima en segundos entre la duración en Spotify "
            "y el resultado de YouTube. 0 desactiva el filtro. Default: 10"
        ),
    )
    parser.add_argument(
        "--metadata-only",
        action="store_true",
        help="Solo extrae metadata y escribe el .txt.",
    )
    return parser.parse_args()


def youtube_default_name(url: str) -> str:
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    for key in ("list", "v"):
        if query.get(key):
            return query[key][0]
    return url.rstrip("/").rsplit("/", 1)[-1]


def main() -> int:
    args = parse_args()
    input_path = Path(args.playlist).expanduser()
    from_youtube = not input_path.is_file() and is_youtube_url(args.playlist)

    if from_youtube or not args.metadata_only:
        require_commands(["yt-dlp"])
    if not input_path.is_file() and not from_youtube:
        require_commands(["spotdl"])
    if not args.metadata_only:
        # yt-dlp usa ffmpeg/ffprobe para extraer (copiar) el stream de audio.
        require_commands(["ffmpeg", "ffprobe", "rsgain"])

    if from_youtube:
        youtube_name, tracks = extract_tracks_from_youtube(args.playlist)
        default_name = youtube_name or youtube_default_name(args.playlist)
    elif input_path.is_file():
        default_name = input_path.stem
    else:
        default_name = playlist_id(args.playlist)

    playlist_name = sanitize_filename(args.name or default_name)
    metadata_file = args.base_dir / "data" / f"{playlist_name}.txt"
    spotdl_file = args.base_dir / "data" / f"{playlist_name}.spotdl"
    out_dir = args.base_dir / "music" / "opus" / playlist_name

    if from_youtube:
        pass  # ya extraídos arriba
    elif input_path.suffix == ".json":
        tracks = extract_tracks_from_json(input_path)
    elif input_path.suffix == ".spotdl":
        tracks = extract_tracks_from_spotdl(input_path)
    else:
        save_playlist_with_spotdl(args.playlist, spotdl_file)
        tracks = extract_tracks_from_spotdl(spotdl_file)

    if args.limit:
        tracks = tracks[: args.limit]

    if not tracks:
        print("No encontré tracks útiles en la playlist.", file=sys.stderr)
        return 1

    write_metadata_file(tracks, metadata_file)
    print(f"[✓] Metadata: {metadata_file}")

    if args.metadata_only:
        return 0

    failed = 0
    resolved: list[Track] = []
    known_ids = index_youtube_ids(out_dir) if from_youtube else {}
    for track in tracks:
        result = process_track(track, out_dir, known_ids, args.duration_tolerance)
        if result is None:
            failed += 1
            resolved.append(track)
        else:
            resolved.append(result)

    # En modo YouTube el artista/álbum se conocen hasta descargar: reescribe la metadata.
    write_metadata_file(resolved, metadata_file)

    if failed:
        print(f"[!] {failed}/{len(tracks)} canciones fallaron.", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
