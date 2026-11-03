"""Resultados parciales y resumen de Actions, sin persistir estado del sincronizador."""
from html import escape
import os
from pathlib import Path


def report_outcome(reports):
    if not any(report.get("errors") for report in reports):
        return "success"
    for report in reports:
        for stats in report.get("channels", {}).values():
            # Un canal histórico sin fuentes ni consultas no demuestra recuperación
            # si todas las consultas reales fallaron (por ejemplo, una clave inválida).
            if stats["result"] == "success" and any(stats.get(key, 0) for key in (
                    "sourcesScanned", "checkedVideos", "quotaUnits")):
                return "partial"
            if stats.get("publication") == "published" and any(stats.get(key, 0) for key in (
                    "sourcesSucceeded", "checkedVideos")):
                return "partial"
    return "failure"


def markdown(value):
    return escape(str(value)).replace("|", "&#124;").replace("\r", "").replace("\n", "<br>")


def channel_counts(reports):
    results = {}
    for report in reports:
        for channel, stats in report.get("channels", {}).items():
            results[channel] = results.get(channel, True) and stats["result"] == "success"
    return sum(results.values()), len(results)


def actions_summary(reports, outcome):
    completed, total = channel_counts(reports)
    title = {"success": "Sincronización completa", "partial": "Sincronización parcial",
             "failure": "Sincronización sin avance verificable"}[outcome]
    lines = [f"## {title}", "", f"**{completed} de {total} canales sin errores; {total - completed} con incidencias.**", "",
             "Los canales se procesan de forma independiente. El paso de publicación confirma el envío del catálogo a main.", ""]
    publications = {"published": "Listo para publicar", "held_incomplete": "Lista anterior conservada",
                    "awaiting_complete_refresh": "Pendiente de revisión completa",
                    "held_empty": "Lista anterior conservada", "awaiting_valid_videos": "Sin videos válidos aún",
                    "unpublished_empty": "Retirado por lista vacía"}
    for report in reports:
        lines += [f"### {markdown(report['mode'])}", "", "| Canal | Resultado | Catálogo | Altas | Bajas |",
                  "| --- | --- | --- | ---: | ---: |"]
        for channel, stats in report.get("channels", {}).items():
            result = {"success": "Completo", "partial": "Parcial", "skipped": "Pendiente"}[stats["result"]]
            publication = publications.get(stats.get("publication"), "Sin cambios")
            lines.append(f"| {markdown(channel)} | {result} | {publication} | {stats.get('addedVideos', 0)} | {stats.get('removedVideos', 0)} |")
        lines.append("")
        for channel, stats in report.get("channels", {}).items():
            source_errors = stats.get("sourceErrors", [])
            for error in source_errors:
                lines.append(f"- **{markdown(channel)}**, fuente {error['sourceIndex']} ({markdown(error['source'])}): {markdown(error['error'])}")
            for error in stats.get("errors", []):
                if not any(item["error"] == error for item in source_errors):
                    lines.append(f"- **{markdown(channel)}**: {markdown(error)}")
        lines.append("")
    return "\n".join(lines)


def annotation(level, message):
    # Evitar que saltos de línea o porcentajes generen comandos adicionales.
    message = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::{level}::{message}", flush=True)


def write_actions_summary(reports, outcome):
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(path).open("a", encoding="utf-8") as output:
            output.write(actions_summary(reports, outcome) + "\n")
    if outcome != "success":
        completed, total = channel_counts(reports)
        status = "sin avance verificable" if outcome == "failure" else "parcial"
        annotation("error" if outcome == "failure" else "warning",
                   f"Sincronización {status}: {completed}/{total} canales sin errores. Revisar el resumen por canal.")
        for report in reports:
            for channel, stats in report.get("channels", {}).items():
                if stats.get("errors"):
                    sources = "; ".join(f"{item['source']}: {item['error']}" for item in stats.get("sourceErrors", []))
                    annotation("warning", f"{channel} ({report['mode']}): {sources or '; '.join(stats['errors'])}")
