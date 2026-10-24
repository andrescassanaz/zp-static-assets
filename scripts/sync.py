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
STATUSES = {"approved", "pending", "rejected", "unavailable"}
DEFAULT_RULES = {"minDurationSeconds": 420, "maxDurationSeconds": None,
                 "music": False, "regionCodes": ["AR"]}


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
    positive_int(config.get("maxPagesPerSource", 20), "maxPagesPerSource", 2)
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
        refs = channel.get("sources")
        if not isinstance(refs, list):
            raise SyncError("sources debe ser una lista de fuentes independientes.")
        for ref in refs:
            source_type(ref)
        if len(refs) != len(set(refs)):
            raise SyncError("Hay fuentes duplicadas en un canal.")
        if mode == "live" and refs and (len(refs) != 1 or source_type(refs[0])[0] != "video"):
            raise SyncError("Para live configurá únicamente el video de la emisión.")
        rules = {**DEFAULT_RULES, **config.get("defaults", {}), **channel.get("rules", {})}
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
                               fields="items(id,snippet(title,channelId,publishedAt,defaultAudioLanguage,liveBroadcastContent),contentDetails(duration,regionRestriction,contentRating),status(privacyStatus,uploadStatus,embeddable),liveStreamingDetails(actualStartTime,actualEndTime))")
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
    metadata = {"title": snippet.get("title"), "channelId": snippet.get("channelId"),
                "publishedAt": snippet.get("publishedAt"), "defaultAudioLanguage": snippet.get("defaultAudioLanguage"),
                "durationSeconds": duration(details.get("duration", ""))}
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


def hard_rejected(record: dict) -> bool:
    return (record.get("status") == "rejected" or record.get("manualDecision") == "rejected" or
            record.get("manualRemoval") is True)


def validate_catalog(catalog: dict):
    if catalog.get("version") != 1 or not isinstance(catalog.get("videos"), dict):
        raise SyncError("Catálogo inválido.")
    for identifier, record in catalog["videos"].items():
        if not VIDEO.fullmatch(identifier) or not isinstance(record, dict) or record.get("status") not in STATUSES:
            raise SyncError("Registro de catálogo inválido.")
        if record.get("manualDecision") not in {None, "approved", "pending", "rejected"}:
            raise SyncError("manualDecision debe ser approved, pending o rejected.")


def update_record(record: dict, item: dict | None, rules: dict, mode: str, now: str, availability_only=False):
    if hard_rejected(record):
        return
    status, reason, metadata = assess(item, rules, mode)
    record["checkedAt"] = now
    record["metadata"] = metadata
    # Las listas heredadas y sus duraciones no se recertifican con reglas nuevas.
    # Su mantenimiento verifica exclusivamente disponibilidad, sin borrar curaduría.
    if availability_only and record.get("legacy") and status in {"pending", "rejected"}:
        status, reason = "approved", "legacy_preserved"
    if record.get("manualDecision") == "pending" and status == "approved":
        status, reason = "pending", "manual_hold"
    record["validationStatus"] = status
    record["status"], record["reason"] = status, reason


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
        if row["youtubeVideoId"] == current and count == occurrence:
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
    def __init__(self, root: Path, api: YouTube | None, now: datetime | None = None):
        self.root, self.api = root, api
        self.now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
        self.when = stamp(self.now)
        self.config, self.index = read_config(root)
        self.state = load(root / "sync-state.json", {"version": 1, "channels": {}})
        if self.state.get("version") != 1 or not isinstance(self.state.get("channels"), dict):
            raise SyncError("sync-state.json inválido.")
        self.errors = []

    def rules(self, channel):
        return {**DEFAULT_RULES, **self.config.get("defaults", {}), **channel.get("rules", {})}

    def baseline(self, channel):
        identifier = channel["id"]
        entry = next((entry for entry in self.index["channels"] if entry["id"] == identifier), None)
        path = safe_file(self.root, entry["file"] if entry else f"channels/{identifier}.json")
        if not entry and path.exists():
            raise SyncError("Ya existe un archivo sin entrada de índice; incorporalo manualmente antes de sincronizar.")
        playlist = load(path) if entry else None
        catalog_path = self.root / "catalog" / f"{identifier}.json"
        catalog = load(catalog_path, {"version": 1, "channelId": identifier, "videos": {}})
        validate_catalog(catalog)
        if catalog.get("channelId") != identifier:
            raise SyncError("El catálogo corresponde a otro canal.")
        for record in catalog["videos"].values():
            if record.get("manualDecision") == "rejected":
                record.update(status="rejected", reason="manual_rejection")
        state = self.state["channels"].setdefault(identifier, {"sources": {}, "publishedIds": []})
        current_ids = {row["youtubeVideoId"] for row in playlist["videos"]} if playlist else set()
        for removed in set(state.get("publishedIds", [])) - current_ids:
            record = catalog["videos"].setdefault(removed, {"firstSeenAt": self.when})
            record.update(status="rejected", reason="manual_removal", manualRemoval=True)
        for row in playlist["videos"] if playlist else []:
            catalog["videos"].setdefault(row["youtubeVideoId"], {
                "status": "approved", "reason": "legacy_preserved", "legacy": True,
                "firstSeenAt": self.when, "sources": []})
        return entry, path, playlist, catalog_path, catalog, state

    def resolve(self, source, state):
        kind, value = source_type(source)
        if kind in {"channel", "handle"}:
            if "playlistId" not in state:
                result = self.api.get("channels", part="contentDetails", **({"forHandle": value} if kind == "handle" else {"id": value}),
                                      fields="items(id,contentDetails/relatedPlaylists/uploads)")
                if not result["items"]:
                    raise SyncError("No se encontró el canal de YouTube.")
                item = result["items"][0]
                playlist = item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
                if not isinstance(playlist, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", playlist):
                    raise SyncError("El canal no tiene playlist de subidas verificable.")
                state.update(channelId=item["id"], playlistId=playlist)
            return "uploads", state["playlistId"]
        return kind, value

    def discover(self, channel, source, state, catalog):
        kind, value = self.resolve(source, state)
        known = set(state.get("knownIds", []))
        if kind == "video":
            if value not in known:
                self.ingest(channel, source, [value], catalog)
                state["knownIds"] = [value]
            return
        # Uploads: cabeza hasta página conocida + backfill independiente.
        # Playlists: cabeza diaria + barrido circular para inserciones arbitrarias.
        limit = self.config.get("maxPagesPerSource", 20)
        token = None
        seen_tokens = set()
        sweep_started = False
        for page_number in range(limit):
            params = {"pageToken": token} if token else {}
            page = self.api.get("playlistItems", part="contentDetails", playlistId=value, maxResults=50,
                                fields="nextPageToken,items/contentDetails/videoId", **params)
            ids = list(dict.fromkeys(item.get("contentDetails", {}).get("videoId", "") for item in page["items"]))
            if any(not VIDEO.fullmatch(identifier) for identifier in ids):
                raise SyncError("Página con IDs incompletos; no se avanza el cursor.")
            already_known = bool(ids) and all(identifier in known for identifier in ids)
            # Sólo videos nuevos, sin volver a consultar metadatos del historial.
            self.ingest(channel, source, [identifier for identifier in ids if identifier not in known], catalog)
            known.update(ids)
            state["knownIds"] = sorted(known)
            next_token = page.get("nextPageToken")
            if next_token is not None and (not isinstance(next_token, str) or next_token in seen_tokens):
                raise SyncError("YouTube repitió un token de paginación.")
            if kind == "uploads" and not sweep_started:
                if state.get("initialized") and already_known and not state.get("backfillToken"):
                    return
                if page_number == 0 and state.get("backfillToken"):
                    token = state["backfillToken"]
                    sweep_started = True
                    continue
                if not next_token:
                    state["initialized"] = True
                    state.pop("backfillToken", None)
                    return
                state["backfillToken"] = next_token
            else:
                if page_number == 0 and state.get("scanToken"):
                    token = state["scanToken"]
                    sweep_started = True
                    continue
                key = "backfillToken" if kind == "uploads" else "scanToken"
                if not next_token:
                    state.pop(key, None)
                    state["initialized"] = True
                    return
                state[key] = next_token
            seen_tokens.add(token)
            token = next_token

    def ingest(self, channel, source, ids, catalog):
        records = catalog["videos"]
        needed = [identifier for identifier in ids if identifier not in records]
        # Una página se registra sólo después de verificar TODOS sus metadatos.
        items = self.api.videos(needed) if needed else {}
        for identifier in ids:
            record = records.setdefault(identifier, {"status": "pending", "firstSeenAt": self.when, "sources": []})
            if source not in record.setdefault("sources", []):
                record["sources"].append(source)
            if identifier in items:
                update_record(record, items[identifier], self.rules(channel), channel.get("playbackMode", "cycle"), self.when)

    def refresh(self, channel, catalog, state, mode):
        if mode == "bootstrap":
            return
        records = catalog["videos"]
        ids = [identifier for identifier, record in records.items()
               if not hard_rejected(record) and (mode == "maintenance" or record["status"] in {"pending", "unavailable"})
               and record.get("checkedAt") != self.when]
        # Cola rotativa: una cuota pequeña no deja el final del catálogo sin revisar.
        ids.sort(key=lambda identifier: (records[identifier].get("checkedAt", ""), identifier))
        for start in range(0, len(ids), 50):
            batch = ids[start:start + 50]
            items = self.api.videos(batch)
            for identifier in batch:
                update_record(records[identifier], items[identifier], self.rules(channel), channel.get("playbackMode", "cycle"), self.when,
                              availability_only=(mode == "maintenance"))
        if mode == "maintenance":
            state["lastMaintenanceAt"] = self.when

    def publish(self, channel, entry, path, old, catalog, state, bootstrap=False):
        records = catalog["videos"]
        rows = []
        # Conserva todos los campos y las duraciones de los videos existentes.
        for row in old["videos"] if old else []:
            record = records[row["youtubeVideoId"]]
            if record["status"] == "approved" and not hard_rejected(record) and record.get("manualDecision") != "pending":
                rows.append(deepcopy(row))
        mode = channel.get("playbackMode", "cycle")
        present = {row["youtubeVideoId"] for row in rows}
        for identifier, record in records.items():
            if identifier in present or record["status"] != "approved" or hard_rejected(record) or record.get("manualDecision") == "pending":
                continue
            # Una aprobación manual nunca evita los controles técnicos.
            metadata = record.get("metadata", {})
            if record.get("validationStatus") != "approved" or not record.get("checkedAt") or (mode == "cycle" and not metadata.get("durationSeconds")):
                continue
            row = {"youtubeVideoId": identifier}
            if mode == "cycle":
                row["durationSeconds"] = metadata["durationSeconds"]
            rows.append(row)
            present.add(identifier)
        if mode == "live":
            # No se inventan reemplazos de una emisión existente.
            rows = rows[:1]
        if bootstrap:
            state["publishedIds"] = [row["youtubeVideoId"] for row in old["videos"]] if old else []
            return
        if not rows:
            # La app rechaza canales vacíos: mantener la última señal permite
            # recuperarse sin perder el canal ni su programación local.
            state["publication"] = "held_empty" if old else "awaiting_approved_videos"
            return
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
        # Nunca modifica name/number/file ni campos manuales de una entrada existente.
        save(path, playlist)
        state["publishedIds"] = [row["youtubeVideoId"] for row in rows]
        state["publication"] = "published"

    def run(self, mode="daily", selected=None):
        # Validación de TODOS los catálogos antes de cualquier llamada o escritura.
        baselines = {}
        for channel in self.config["channels"]:
            if channel.get("active", True) and (selected is None or channel["id"] == selected):
                baselines[channel["id"]] = self.baseline(channel)
        if selected and selected not in baselines:
            raise SyncError("El canal seleccionado no existe o está inactivo.")
        exhausted = False
        for channel in self.config["channels"]:
            if channel["id"] not in baselines:
                continue
            entry, path, old, catalog_path, catalog, state = baselines[channel["id"]]
            if mode == "daily" and not exhausted:
                for source in channel["sources"]:
                    source_state = state.setdefault("sources", {}).setdefault(source, {})
                    try:
                        self.discover(channel, source, source_state, catalog)
                        source_state["lastSuccessAt"] = self.when
                        source_state.pop("error", None)
                    except SyncError as exc:
                        source_state["error"] = str(exc)
                        # Tokens pueden caducar: el siguiente intento reinicia el
                        # barrido, deduplicando contra IDs persistidos.
                        if "HTTP 400" in str(exc):
                            source_state.pop("backfillToken", None)
                            source_state.pop("scanToken", None)
                        self.errors.append(f"{channel['id']}: {exc}")
                        if isinstance(exc, QuotaError):
                            exhausted = True
                            break
            if not exhausted:
                try:
                    self.refresh(channel, catalog, state, mode)
                except SyncError as exc:
                    self.errors.append(f"{channel['id']}: {exc}")
                    exhausted = isinstance(exc, QuotaError)
            # Guardar decisiones antes de publicar protege las bajas manuales
            # incluso si se interrumpe la escritura del archivo de reproducción.
            save(catalog_path, catalog)
            self.publish(channel, entry, path, old, catalog, state, bootstrap=(mode == "bootstrap"))
        save(self.root / "channels.json", self.index)
        if self.api:
            self.state["lastRun"] = {"at": self.when, "mode": mode, "quotaUnits": self.api.used,
                                     "result": "partial" if self.errors else "success", "errors": self.errors}
        save(self.root / "sync-state.json", self.state)
        return 2 if self.errors else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["daily", "maintenance", "bootstrap", "validate"], default="daily")
    parser.add_argument("--channel", help="ID de un canal activo; omitido procesa todos")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        with lock(args.root):
            config, _ = read_config(args.root)
            api = None
            if args.mode not in {"bootstrap", "validate"}:
                key = os.environ.get("YOUTUBE_API_KEY", "").strip()
                if not key:
                    raise SyncError("Falta YOUTUBE_API_KEY en el entorno de GitHub Actions Secrets.")
                api = YouTube(key, config.get("quotaBudget", 2500))
            sync = Sync(args.root, api)
            if args.mode == "validate":
                for channel in config["channels"]:
                    sync.baseline(channel)
                print("Configuración, catálogo e índice válidos. No se consultó YouTube.")
                return 0
            result = sync.run(args.mode, args.channel)
            print(f"Sincronización {args.mode}: {len(sync.errors)} errores; {api.used if api else 0} unidades de cuota.")
            for error in sync.errors:
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
