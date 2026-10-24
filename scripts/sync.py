#!/usr/bin/env python3
"""Sincronización de assets. Python 3.10+, biblioteca estándar, sin backend."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from http.client import HTTPException
import json
import math
import os
from pathlib import Path
import random
import re
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
VIDEO = re.compile(r"[A-Za-z0-9_-]{11}\Z")
CHANNEL = re.compile(r"UC[A-Za-z0-9_-]{22}\Z")
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
DURATION = re.compile(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?\Z")
DEFAULT_RULES = {"minDurationSeconds": 420, "maxDurationSeconds": None,
                 "music": False, "regionCodes": ["AR"], "maxAgeDays": None}


class SyncError(Exception):
    """Sólo mensajes seguros: nunca URLs, claves ni respuestas crudas."""


class QuotaError(SyncError):
    pass


def stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="auto").replace("+00:00", "Z")


def date(value: str) -> datetime:
    try:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", value):
            raise ValueError
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except (ValueError, TypeError):
        raise SyncError("Fecha UTC inválida; debe terminar en Z.") from None


def publication_date(value) -> datetime | None:
    """YouTube usa RFC 3339; normaliza también desplazamientos a UTC."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, OverflowError):
        return None


def age_cutoff(now: datetime, days: int | None) -> datetime | None:
    if days is None:
        return None
    try:
        return now - timedelta(days=days)
    except OverflowError:
        return datetime.min.replace(tzinfo=timezone.utc)


def within_window(published: datetime | None, cutoff: datetime | None, now: datetime) -> bool:
    return cutoff is None or (published is not None and cutoff <= published <= now)


def load(path: Path, default=None):
    if not path.exists() and default is not None:
        return deepcopy(default)
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise SyncError(f"No se pudo leer JSON válido: {path.name}.") from None


def save(path: Path, value) -> bool:
    """No toca bytes ni mtime si los datos son equivalentes."""
    if path.exists() and load(path) == value:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".sync-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


@contextmanager
def lock(root: Path):
    path = root / ".sync.lock"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise SyncError("Ya hay una sincronización en ejecución (.sync.lock).") from None
    try:
        os.close(fd)
        yield
    finally:
        path.unlink(missing_ok=True)


def source_type(source: str) -> tuple[str, str]:
    if not isinstance(source, str) or source != source.strip():
        raise SyncError("Cada fuente debe ser un ID o @handle sin espacios externos.")
    if source.startswith("video:") and VIDEO.fullmatch(source[6:]):
        return "video", source[6:]
    if source.startswith("playlist:") and re.fullmatch(r"[A-Za-z0-9_-]+", source[9:]):
        return "playlist", source[9:]
    if CHANNEL.fullmatch(source):
        return "channel", source
    if re.fullmatch(r"@[^\s/@?#\\]+", source):
        return "handle", source
    if VIDEO.fullmatch(source):
        return "video", source
    if re.fullmatch(r"[A-Za-z0-9_-]{12,}", source):
        return "playlist", source
    raise SyncError("Fuente inválida: usá UC..., @handle, ID de playlist o ID de video.")


def positive_int(value, name: str, minimum=1):
    if type(value) is not int or value < minimum:
        raise SyncError(f"{name} debe ser un entero >= {minimum}.")
    return value


def safe_file(root: Path, value: str) -> Path:
    if not isinstance(value, str) or not re.fullmatch(r"channels/[a-z0-9][a-z0-9-]{0,63}\.json", value):
        raise SyncError("file debe ser channels/<id>.json.")
    path = root / value
    if not path.resolve().is_relative_to(root.resolve()):
        raise SyncError("Ruta de canal fuera del repositorio.")
    return path


def validate_playlist(playlist: dict, mode: str):
    if not isinstance(playlist, dict) or not isinstance(playlist.get("videos"), list) or not playlist["videos"]:
        raise SyncError("Un canal publicado debe tener videos.")
    for row in playlist["videos"]:
        if not isinstance(row, dict) or not VIDEO.fullmatch(row.get("youtubeVideoId", "")):
            raise SyncError("ID inválido en un archivo de reproducción.")
        if mode == "cycle":
            positive_int(row.get("durationSeconds"), "durationSeconds")
    if mode == "cycle":
        date(playlist.get("cycleStartsAt"))
        if sum(row["durationSeconds"] for row in playlist["videos"]) > 2**63 - 1:
            raise SyncError("La duración total supera el límite de la app.")
    elif (len(playlist["videos"]) != 1 or "cycleStartsAt" in playlist or
          "durationSeconds" in playlist["videos"][0]):
        raise SyncError("Un directo requiere un solo ID, sin duración ni origen de ciclo.")


def read_config(root: Path):
    config = load(root / "sources.json")
    index = load(root / "channels.json")
    if config.get("version") != 1 or not isinstance(config.get("channels"), list):
        raise SyncError("sources.json requiere version=1 y channels=[...].")
    if not isinstance(index.get("channels"), list):
        raise SyncError("channels.json requiere channels=[...].")
    positive_int(config.get("quotaBudget", 2500), "quotaBudget")
    ids, numbers, files = set(), set(), set()
    for entry in index["channels"]:
        identifier = entry.get("id", "")
        if not SLUG.fullmatch(identifier) or identifier in ids:
            raise SyncError("ID inválido o duplicado en channels.json.")
        number = positive_int(entry.get("number"), "number")
        if number in numbers or entry.get("file") in files:
            raise SyncError("Número o archivo duplicado en channels.json.")
        if not isinstance(entry.get("name"), str) or not entry["name"].strip():
            raise SyncError("Falta el nombre de un canal.")
        if entry.get("playbackMode") not in {"cycle", "live"}:
            raise SyncError("playbackMode debe ser cycle o live.")
        validate_playlist(load(safe_file(root, entry["file"])), entry["playbackMode"])
        ids.add(identifier); numbers.add(number); files.add(entry["file"])
    configured = set()
    for channel in config["channels"]:
        identifier = channel.get("id", "")
        if not SLUG.fullmatch(identifier) or identifier in configured:
            raise SyncError("ID inválido o duplicado en sources.json.")
        configured.add(identifier)
        if not isinstance(channel.get("name"), str) or not channel["name"].strip():
            raise SyncError("Cada configuración requiere name.")
        if type(channel.get("active", True)) is not bool:
            raise SyncError("active debe ser true o false.")
        mode = channel.get("playbackMode", "cycle")
        if mode not in {"cycle", "live"}:
            raise SyncError("playbackMode debe ser cycle o live.")
        existing = next((entry for entry in index["channels"] if entry["id"] == identifier), None)
        if existing and existing["playbackMode"] != mode:
            raise SyncError("No se cambia automáticamente el modo de un canal existente.")
        if "number" in channel:
            positive_int(channel["number"], "number")
            if not existing and channel["number"] in numbers:
                raise SyncError("El número solicitado ya está ocupado.")
            if not existing:
                numbers.add(channel["number"])
        excluded = channel.get("excludedVideoIds", [])
        if not isinstance(excluded, list) or any(not isinstance(identifier, str) or not VIDEO.fullmatch(identifier) for identifier in excluded):
            raise SyncError("excludedVideoIds debe ser una lista de IDs de video válidos.")
        refs = channel.get("sources")
        if not isinstance(refs, list):
            raise SyncError("sources debe ser una lista de fuentes independientes.")
        for ref in refs:
            source_type(ref)
        if len(refs) != len(set(refs)):
            raise SyncError("Hay fuentes duplicadas en un canal.")
        if mode == "live" and refs and (len(refs) != 1 or source_type(refs[0])[0] != "video"):
            raise SyncError("Para live configurá únicamente el video de la emisión.")
        update_mode = channel.get("updateMode", "append")
        if not isinstance(update_mode, str) or update_mode not in {"append", "replace"}:
            raise SyncError("updateMode debe ser append o replace.")
        if update_mode == "replace" and not refs:
            raise SyncError("updateMode=replace requiere al menos una fuente.")
        rules = {**DEFAULT_RULES, **config.get("defaults", {}), **channel.get("rules", {})}
        if rules["maxAgeDays"] is not None:
            positive_int(rules["maxAgeDays"], "maxAgeDays")
        positive_int(rules["minDurationSeconds"], "minDurationSeconds", 0)
        maximum = rules["maxDurationSeconds"]
        if maximum is not None and positive_int(maximum, "maxDurationSeconds") <= rules["minDurationSeconds"]:
            raise SyncError("maxDurationSeconds debe superar el mínimo.")
        if type(rules["music"]) is not bool or not isinstance(rules["regionCodes"], list) or not rules["regionCodes"]:
            raise SyncError("rules requiere music booleano y regionCodes no vacío.")
        if any(not isinstance(code, str) or not re.fullmatch(r"[A-Z]{2}", code) for code in rules["regionCodes"]):
            raise SyncError("regionCodes requiere códigos de país de dos letras mayúsculas.")
    return config, index


class YouTube:
    """Los tres endpoints usados cuestan 1 unidad por solicitud, incluidos reintentos."""
    def __init__(self, key: str, budget: int, opener=urlopen, sleep=time.sleep):
        self.key, self.budget, self.used = key, budget, 0
        self.opener, self.sleep = opener, sleep
        self.video_cache = {}

    def get(self, resource: str, **params):
        if resource not in {"channels", "playlistItems", "videos"}:
            raise SyncError("Endpoint no permitido por el presupuesto de cuota.")
        url = "https://www.googleapis.com/youtube/v3/" + resource + "?" + urlencode({**params, "key": self.key})
        for attempt in range(4):
            if self.used >= self.budget:
                raise QuotaError("Se alcanzó el presupuesto local de cuota; se conserva el avance.")
            self.used += 1
            try:
                with self.opener(url, timeout=30) as response:
                    payload = json.load(response)
                if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                    raise SyncError("Respuesta de YouTube incompleta; no se avanza esta página.")
                if any(not isinstance(item, dict) for item in payload["items"]):
                    raise SyncError("YouTube devolvió elementos inválidos; no se avanza esta página.")
                if resource == "videos":
                    requested = set(params.get("id", "").split(","))
                    for item in payload["items"]:
                        if item.get("id") not in requested or any(
                            key in item and not isinstance(item[key], dict)
                            for key in ("snippet", "contentDetails", "status", "liveStreamingDetails")
                        ):
                            raise SyncError("Metadatos de YouTube inválidos; no se modifica disponibilidad.")
                return payload
            except HTTPError as exc:
                code = exc.code
                try:
                    body = json.loads(exc.read())
                    reasons = {item.get("reason") for item in body.get("error", {}).get("errors", [])}
                except (ValueError, AttributeError, TypeError, OSError, HTTPException):
                    reasons = set()
                finally:
                    exc.close()
                if reasons & {"quotaExceeded", "dailyLimitExceeded"}:
                    raise QuotaError("YouTube agotó la cuota; se conserva el avance.") from None
                if code not in {408, 429, 500, 502, 503, 504} and not reasons & {"rateLimitExceeded", "userRateLimitExceeded"}:
                    raise SyncError(f"YouTube HTTP {code}; se conserva la programación.") from None
            except (URLError, TimeoutError, OSError, HTTPException, ValueError):
                pass
            if attempt < 3:
                self.sleep(2**attempt + random.random())
        raise SyncError("YouTube no respondió después de cuatro intentos.") from None

    def videos(self, ids: list[str]) -> dict:
        missing = list(dict.fromkeys(identifier for identifier in ids if identifier not in self.video_cache))
        for start in range(0, len(missing), 50):
            batch = missing[start:start + 50]
            payload = self.get("videos", part="snippet,contentDetails,status,liveStreamingDetails", id=",".join(batch),
                               fields="items(id,snippet(publishedAt,defaultAudioLanguage,liveBroadcastContent),contentDetails(duration,regionRestriction,contentRating),status(privacyStatus,uploadStatus,embeddable),liveStreamingDetails(actualStartTime,actualEndTime))")
            found = {item.get("id"): item for item in payload["items"]}
            for identifier in batch:
                self.video_cache[identifier] = found.get(identifier)
        return {identifier: self.video_cache[identifier] for identifier in ids}


def duration(value: str) -> int | None:
    match = DURATION.fullmatch(value)
    if not match or not any(match.groups()):
        return None
    seconds = sum(float(part or 0) * multiplier for part, multiplier in zip(match.groups(), (86400, 3600, 60, 1)))
    return int(seconds) if math.isfinite(seconds) and seconds > 0 and seconds.is_integer() else None


def assess(item: dict | None, rules: dict, mode: str) -> tuple[str, str, dict]:
    if item is None:
        return "unavailable", "missing_or_private", {}
    snippet, details, status = (item.get(key, {}) for key in ("snippet", "contentDetails", "status"))
    live = item.get("liveStreamingDetails", {})
    metadata = {"durationSeconds": duration(details.get("duration", ""))}
    if not all(key in status for key in ("privacyStatus", "uploadStatus", "embeddable")):
        return "pending", "availability_unknown", metadata
    if status.get("privacyStatus") not in {"public", "unlisted"} or status.get("uploadStatus") != "processed":
        return "unavailable", "not_public_or_processed", metadata
    if status.get("embeddable") is not True:
        return "unavailable", "not_embeddable", metadata
    restriction = details.get("regionRestriction", {})
    for region in rules["regionCodes"]:
        if region in restriction.get("blocked", []) or ("allowed" in restriction and region not in restriction["allowed"]):
            return "unavailable", "region_restricted", metadata
    if details.get("contentRating", {}).get("ytRating") == "ytAgeRestricted":
        return "unavailable", "age_restricted", metadata
    if mode == "live":
        if not live.get("actualStartTime") or live.get("actualEndTime"):
            return "pending", "not_live", metadata
    else:
        if snippet.get("liveBroadcastContent") in {"live", "upcoming"} or (live and not live.get("actualEndTime")):
            return "pending", "live_or_upcoming", metadata
        seconds = metadata["durationSeconds"]
        if seconds is None:
            return "pending", "duration_unknown", metadata
        if seconds <= rules["minDurationSeconds"] or (rules["maxDurationSeconds"] is not None and seconds >= rules["maxDurationSeconds"]):
            return "rejected", "duration_out_of_range", metadata
    if not rules["music"]:
        audio = snippet.get("defaultAudioLanguage")
        if not audio:
            return "pending", "audio_language_unknown", metadata
        if audio.lower().split("-")[0] != "es":
            return "rejected", "audio_not_spanish", metadata
    return "approved", "validated", metadata


def anchor_update(old: dict, rows: list[dict], now: datetime) -> str:
    """Mantiene el video/segundo UTC actual aunque cambie el módulo del ciclo."""
    origin = date(old["cycleStartsAt"])
    total = sum(row["durationSeconds"] for row in old["videos"])
    elapsed = math.floor((now - origin).total_seconds())
    position = elapsed % total
    offset = 0
    current = None
    current_index = 0
    occurrence = 0
    counts = {}
    for current_index, row in enumerate(old["videos"]):
        identifier = row["youtubeVideoId"]
        counts[identifier] = counts.get(identifier, 0) + 1
        if offset + row["durationSeconds"] > position:
            current = identifier
            occurrence = counts[identifier]
            break
        offset += row["durationSeconds"]
    before = 0
    count = 0
    for row in rows:
        if row["youtubeVideoId"] == current:
            count += 1
        if (row["youtubeVideoId"] == current and count == occurrence
                and position - offset < row["durationSeconds"]):
            # Origen del ciclo viejo en curso + desplazamiento antes del programa.
            return stamp(origin + timedelta(seconds=(elapsed // total) * total + offset - before))
        before += row["durationSeconds"]
    # Si se retira el programa actual, alinear el siguiente sobreviviente al final
    # nominal de ese programa. El reproductor de la app termina el video ya cargado.
    boundary = origin + timedelta(seconds=(elapsed // total) * total + offset + old["videos"][current_index]["durationSeconds"])
    kept = {row["youtubeVideoId"] for row in rows}
    following = old["videos"][current_index + 1:] + old["videos"][:current_index]
    target = next((row["youtubeVideoId"] for row in following if row["youtubeVideoId"] in kept), rows[0]["youtubeVideoId"])
    before = 0
    for row in rows:
        if row["youtubeVideoId"] == target:
            return stamp(boundary - timedelta(seconds=before))
        before += row["durationSeconds"]
    raise SyncError("No se pudo alinear la programación.")


class Sync:
    """Los JSON de reproducción son la única lista de IDs ya incorporados."""
    def __init__(self, root: Path, api: YouTube | None, now: datetime | None = None):
        self.root, self.api = root, api
        self.now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
        self.when = stamp(self.now)
        self.config, self.index = read_config(root)
        state = load(root / "sync-state.json", {"version": 1, "channels": {}})
        if state.get("version") != 1 or not isinstance(state.get("channels"), dict):
            raise SyncError("sync-state.json inválido.")
        self.state = state
        self.errors = []
        self.resolved = {}

    def rules(self, channel):
        return {**DEFAULT_RULES, **self.config.get("defaults", {}), **channel.get("rules", {})}

    def baseline(self, channel):
        identifier = channel["id"]
        entry = next((entry for entry in self.index["channels"] if entry["id"] == identifier), None)
        path = safe_file(self.root, entry["file"] if entry else f"channels/{identifier}.json")
        if not entry and path.exists():
            raise SyncError("Ya existe un archivo sin entrada de índice; incorporalo manualmente antes de sincronizar.")
        return entry, path, load(path) if entry else None

    def iter_source(self, source, cutoff=None):
        """Recorre TODAS las páginas en cada ejecución; sin cursores persistentes."""
        kind, value = source_type(source)
        if kind == "video":
            yield [value]
            return
        if kind in {"channel", "handle"}:
            if source not in self.resolved:
                result = self.api.get("channels", part="contentDetails", **({"forHandle": value} if kind == "handle" else {"id": value}),
                                      fields="items/contentDetails/relatedPlaylists/uploads")
                if not result["items"]:
                    raise SyncError("No se encontró el canal de YouTube.")
                playlist = result["items"][0].get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
                if not isinstance(playlist, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", playlist):
                    raise SyncError("El canal no tiene playlist de subidas verificable.")
                self.resolved[source] = playlist
            value = self.resolved[source]
        token = None
        seen_tokens = set()
        while True:
            page = self.api.get("playlistItems", part="contentDetails", playlistId=value, maxResults=50,
                                fields="nextPageToken,items/contentDetails(videoId,videoPublishedAt)", **({"pageToken": token} if token else {}))
            ids = list(dict.fromkeys(item.get("contentDetails", {}).get("videoId", "") for item in page["items"]))
            if any(not isinstance(identifier, str) or not VIDEO.fullmatch(identifier) for identifier in ids):
                raise SyncError("Página con IDs incompletos; no se incorporan sus videos.")
            if cutoff is not None:
                # La fecha de publicación del video es distinta de su alta en la
                # playlist. Sin fecha, consultar videos.list antes de decidir.
                ids = list(dict.fromkeys(
                    item["contentDetails"]["videoId"] for item in page["items"]
                    if (published := publication_date(item["contentDetails"].get("videoPublishedAt"))) is None
                    or within_window(published, cutoff, self.now)
                ))
            yield ids
            token = page.get("nextPageToken")
            if token is None:
                return
            if not isinstance(token, str) or not token or token in seen_tokens:
                raise SyncError("YouTube devolvió un token de paginación inválido o repetido.")
            seen_tokens.add(token)

    def discover(self, channel, rows, stats):
        excluded = set(channel.get("excludedVideoIds", []))
        seen = {row["youtubeVideoId"] for row in rows} | excluded
        discovered = set()
        rules = self.rules(channel)
        cutoff = age_cutoff(self.now, rules["maxAgeDays"])
        for source in channel["sources"]:
            stats["sourcesScanned"] += 1
            try:
                for ids in self.iter_source(source, cutoff):
                    discovered.update(ids)
                    new_ids = [identifier for identifier in ids if identifier not in seen]
                    if not new_ids:
                        continue
                    # En replace, rows empieza vacío y se revalidan también los
                    # videos que ya estaban publicados.
                    items = self.api.videos(new_ids)
                    for identifier in new_ids:
                        item = items[identifier]
                        status, _, metadata = assess(item, rules, channel.get("playbackMode", "cycle"))
                        published = publication_date(item.get("snippet", {}).get("publishedAt")) if item else None
                        if status == "approved" and within_window(published, cutoff, self.now):
                            row = {"youtubeVideoId": identifier}
                            if channel.get("playbackMode", "cycle") == "cycle":
                                row["durationSeconds"] = metadata["durationSeconds"]
                            rows.append(row)
                            stats["addedVideos"] += 1
                        else:
                            stats["filteredVideos"] += 1
                        seen.add(identifier)
            except SyncError as exc:
                stats["discoveredIds"] = len(discovered)
                stats["errors"].append(str(exc))
                if isinstance(exc, QuotaError):
                    raise
        stats["discoveredIds"] = len(discovered)

    def maintain(self, channel, rows, stats):
        # Se retiran sólo bajas verificadas; un error de red conserva el resto.
        ids = list(dict.fromkeys(row["youtubeVideoId"] for row in rows))
        for start in range(0, len(ids), 50):
            batch = ids[start:start + 50]
            items = self.api.videos(batch)
            unavailable = {identifier for identifier in batch
                           if assess(items[identifier], self.rules(channel), channel.get("playbackMode", "cycle"))[0] == "unavailable"}
            if channel.get("updateMode", "append") == "replace":
                cutoff = age_cutoff(self.now, self.rules(channel)["maxAgeDays"])
                unavailable.update(identifier for identifier in batch if items[identifier] is not None and
                                   not within_window(publication_date(items[identifier].get("snippet", {}).get("publishedAt")), cutoff, self.now))
            stats["checkedVideos"] += len(batch)
            previous_length = len(rows)
            rows[:] = [row for row in rows if row["youtubeVideoId"] not in unavailable]
            stats["removedVideos"] += previous_length - len(rows)

    def publish(self, channel, entry, path, old, rows, allow_empty=False):
        mode = channel.get("playbackMode", "cycle")
        if not rows:
            if allow_empty and entry:
                # Un reemplazo completo sin resultados retira el canal del índice:
                # Android no admite listas vacías y no debe mostrar material viejo.
                self.index["channels"].remove(entry)
                save(self.root / "channels.json", self.index)
                path.unlink(missing_ok=True)
                return "unpublished_empty"
            # Android no admite listas vacías. Mantener la última válida.
            return "held_empty" if old else "awaiting_valid_videos"
        if mode == "live":
            rows = rows[:1]
        playlist = deepcopy(old) if old else {}
        playlist["videos"] = rows
        if mode == "cycle" and (not old or old["videos"] != rows):
            playlist["cycleStartsAt"] = anchor_update(old, rows, self.now) if old else self.when
        validate_playlist(playlist, mode)
        if not entry:
            reserved = [item.get("number", 0) for item in self.config["channels"]]
            number = channel.get("number") or max([item["number"] for item in self.index["channels"]] + reserved + [0]) + 1
            entry = {"id": channel["id"], "number": number, "name": channel["name"], "playbackMode": mode,
                     "file": f"channels/{channel['id']}.json"}
            self.index["channels"].append(entry)
        save(path, playlist)
        return "published"

    def run(self, mode="daily", selected=None):
        baselines = {channel["id"]: self.baseline(channel) for channel in self.config["channels"]
                     if channel.get("active", True) and (selected is None or channel["id"] == selected)}
        if selected and selected not in baselines:
            raise SyncError("El canal seleccionado no existe o está inactivo.")
        exhausted = False
        for channel in self.config["channels"]:
            if channel["id"] not in baselines:
                continue
            entry, path, old = baselines[channel["id"]]
            excluded = set(channel.get("excludedVideoIds", []))
            rows = [deepcopy(row) for row in old["videos"] if row["youtubeVideoId"] not in excluded] if old else []
            stats = {"at": self.when, "mode": mode, "result": "success", "sourcesScanned": 0,
                     "discoveredIds": 0, "addedVideos": 0, "filteredVideos": 0, "checkedVideos": 0,
                     "removedVideos": len(old["videos"]) - len(rows) if old else 0, "errors": []}
            replace = channel.get("updateMode", "append") == "replace"
            if replace and mode == "daily":
                rows = []
            if exhausted:
                stats.update(result="skipped", errors=["Sin cuota disponible; canal pendiente."])
            else:
                try:
                    if mode == "daily":
                        self.discover(channel, rows, stats)
                    else:
                        self.maintain(channel, rows, stats)
                except SyncError as exc:
                    if str(exc) not in stats["errors"]:
                        stats["errors"].append(str(exc))
                    exhausted = isinstance(exc, QuotaError)
                if stats["errors"]:
                    stats["result"] = "partial"
            self.errors.extend(f"{channel['id']}: {error}" for error in stats["errors"])
            if replace and stats["result"] != "success":
                stats["publication"] = "held_incomplete" if old else "awaiting_complete_refresh"
                stats["addedVideos"] = stats["removedVideos"] = 0
            else:
                if replace and mode == "daily":
                    old_ids = {row["youtubeVideoId"] for row in old["videos"]} if old else set()
                    new_ids = {row["youtubeVideoId"] for row in rows}
                    stats["addedVideos"] = len(new_ids - old_ids)
                    stats["removedVideos"] = sum(row["youtubeVideoId"] not in new_ids for row in old["videos"]) if old else 0
                stats["publication"] = self.publish(channel, entry, path, old, rows, allow_empty=replace)
            self.state["channels"][channel["id"]] = stats
        save(self.root / "channels.json", self.index)
        self.state["lastRun"] = {"at": self.when, "mode": mode, "quotaUnits": self.api.used,
                                 "result": "partial" if self.errors else "success", "errors": self.errors}
        save(self.root / "sync-state.json", self.state)
        return 2 if self.errors else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["daily", "maintenance", "validate"], default="daily")
    parser.add_argument("--channel", help="ID de un canal activo; omitido procesa todos")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        with lock(args.root):
            engine = Sync(args.root, None)
            if args.mode == "validate":
                for channel in engine.config["channels"]:
                    engine.baseline(channel)
                print("Configuración e índice válidos. No se consultó YouTube.")
                return 0
            key = os.environ.get("YOUTUBE_API_KEY", "").strip()
            if not key:
                raise SyncError("Falta YOUTUBE_API_KEY en el entorno de GitHub Actions Secrets.")
            engine.api = YouTube(key, engine.config.get("quotaBudget", 2500))
            result = engine.run(args.mode, args.channel)
            print(f"Sincronización {args.mode}: {len(engine.errors)} errores; {engine.api.used} unidades de cuota.")
            for error in engine.errors:
                print(error, file=sys.stderr)
            return result
    except SyncError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception:
        # Los tracebacks de urllib pueden incluir la API key en la URL.
        print("Error interno. Se conservaron los archivos ya guardados; ejecutar las pruebas antes de reintentar.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
