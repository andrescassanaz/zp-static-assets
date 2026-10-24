# Canales automáticos de ZappingTube

El sistema recorre las fuentes de YouTube, compara sus IDs con el JSON de cada canal y agrega sólo videos nuevos y válidos. Funciona con Python 3.10+, YouTube Data API y GitHub Actions, sin dependencias externas, backend ni base de datos.

## Cómo actualiza

Para cada canal activo en `sources.json`:

1. Lee los videos que ya contiene `channels/{id}.json`.
2. Recorre **todas las páginas** de cada canal o playlist configurada y los videos individuales.
3. Elimina duplicados entre las fuentes y descarta IDs que ya están en el JSON del canal.
4. Consulta metadatos únicamente de los IDs nuevos y aplica las validaciones.
5. Agrega los videos válidos al final de la lista, conservando el orden y los datos de los existentes.

Cada ejecución vuelve a recorrer las fuentes completas. No se detiene al encontrar IDs conocidos ni conserva cursores, listas de descubiertos o catálogos por video. Así también encuentra incorporaciones al medio o al final de una playlist. La paginación de YouTube puede verse afectada por modificaciones concurrentes; el siguiente recorrido vuelve a comprobarla.

Los JSON de reproducción son la referencia para saber qué videos ya fueron incorporados. Las repeticiones que ya existían en una lista se conservan, respetando la programación manual; se deduplican las nuevas incorporaciones.

## Archivos

| Archivo | Uso |
| --- | --- |
| `sources.json` | Canales, fuentes, reglas y exclusiones manuales opcionales |
| `channels.json` | Índice público de canales disponibles |
| `channels/{id}.json` | Videos que reproduce cada canal, con el formato actual de Android |
| `sync-state.json` | Resumen de las últimas ejecuciones: fecha, cantidades, cuota y errores |
| `scripts/sync.py` | Recorrido, comparación, validación y publicación |

No se utiliza `catalog/`. No se guardan registros individuales de aprobados, pendientes o rechazados. Los candidatos que no pasan las validaciones se omiten; si siguen en las fuentes, se volverán a evaluar en una ejecución posterior.

## Agregar un canal

Agregar un objeto a la lista `channels` de `sources.json`:

```json
{
  "id": "cocina-automatica",
  "name": "Cocina automática",
  "active": true,
  "playbackMode": "cycle",
  "sources": [
    "@paulinacocina",
    "@MarcosDiCesare"
  ],
  "rules": {
    "minDurationSeconds": 420,
    "maxDurationSeconds": 7200,
    "music": false,
    "regionCodes": ["AR"]
  }
}
```

Cuando encuentre videos válidos, el sistema crea `channels/cocina-automatica.json` y agrega su entrada a `channels.json`, con el siguiente número libre. `number` puede fijarse explícitamente si está disponible. Un canal sin videos válidos todavía no se publica.

`id` debe ser estable y usar letras minúsculas, números y guiones, hasta 64 caracteres. `active: false` pausa la automatización sin retirar el canal de la aplicación. Las entradas existentes del índice, sus nombres, números y campos adicionales se conservan.

Cada fuente ocupa un elemento independiente:

| Fuente | Interpretación |
| --- | --- |
| ID de 24 caracteres que empieza con `UC` | Canal: se recorre su playlist de subidas |
| `@handle` | Canal: se resuelve a su playlist de subidas |
| ID de video de 11 caracteres | Video individual |
| Otro ID de más de 11 caracteres | Playlist |
| `playlist:ID` / `video:ID` | Tipo explícito para evitar ambigüedades |

No admite URLs completas. `playbackMode: live` requiere una única fuente de video individual; no inventa sustitutos cuando termina una emisión. Su JSON contiene un solo ID, sin duración ni origen de ciclo.

`comida` ya tiene las 20 fuentes confirmadas del archivo de referencia `Script extraer IDs/canales.txt`: 8 handles y 12 playlists. Los demás canales conservan sus listas y el mantenimiento semanal; completar sus fuentes para habilitar descubrimiento.

## Validaciones y exclusiones manuales

Los nuevos videos deben estar disponibles y procesados, permitir inserción y poder reproducirse en todos los países configurados en `regionCodes` (predeterminado `AR`). Se descartan videos con restricciones de edad incompatibles con reproducción embebida. Para ciclos no se incorporan emisiones en curso ni próximas.

Las duraciones deben ser enteros positivos. Los límites son exclusivos, como en `extraer_ids.py`: mínimo 420 significa más de 7 minutos. El máximo predeterminado es `null`, sin máximo.

El contenido no musical requiere `snippet.defaultAudioLanguage=es` o una variante como `es-AR`. La ausencia del dato no se reemplaza con títulos, subtítulos o `defaultLanguage`. `music: true` exceptúa solamente el idioma. Los videos existentes no se recertifican con nuevas reglas ni se sobrescriben sus duraciones manuales.

Para impedir la incorporación de un video, agregar su ID en la configuración de ese canal:

```json
"excludedVideoIds": [
  "ID_DE_VIDEO"
]
```

Reemplazar el marcador por un ID real de 11 caracteres. Las exclusiones son opcionales; se descartan antes de consultar metadatos y también retiran ese ID de una lista existente, siempre que quede una lista reproducible. Para restaurarlo, quitarlo de `excludedVideoIds`.

Si sólo se borra un video del JSON de reproducción y sigue en una fuente, se considera nuevo y puede volver a incorporarse. Usar `excludedVideoIds` cuando se trate de un rechazo permanente. No existe otro historial de decisiones.

## GitHub Actions

La carpeta local se llama `zp-static-asset`; su remoto es [`andrescassanaz/zp-static-assets`](https://github.com/andrescassanaz/zp-static-assets). Toda la implementación vive en ese repositorio.

1. Integrar los cambios en `main`.
2. Configurar **Settings → Secrets and variables → Actions → YOUTUBE_API_KEY**. La clave ya fue registrada durante la implementación. El script la lee exclusivamente del entorno; no lee archivos locales de claves ni la imprime.
3. Habilitar Actions y permitir al workflow escribir `contents`, respetando las políticas de protección de rama del repositorio.
4. Ejecutar **Actions → Sincronizar canales → Run workflow**, con `daily` y opcionalmente el ID de un canal. Los cambios en las fuentes y listas también disparan la sincronización.

Hay una sincronización diaria a las **06:17 de Argentina** y mantenimiento los domingos a las **07:47 de Argentina** (09:17 y 10:47 UTC). GitHub puede demorar los horarios y deshabilitarlos en repositorios públicos tras 60 días sin actividad; pueden reactivarse desde Actions.

El mantenimiento semanal consulta los videos ya publicados y retira los confirmados como eliminados, privados, restringidos o sin inserción. Conserva el resto ante errores de red. Si un canal quedaría vacío, mantiene su último JSON válido y registra `held_empty`, porque Android no admite listas vacías. La señal en YouTube puede seguir caída hasta que haya contenido válido. Un video retirado puede reincorporarse en el recorrido diario si vuelve a estar disponible en alguna fuente y no está excluido.

## Cuota y errores

Se usan `channels.list`, `playlistItems.list` y `videos.list`, con costo de una unidad por solicitud. No se utiliza `search.list`. Los metadatos se agrupan en lotes de hasta 50 IDs y se cachean dentro de la ejecución. Los IDs existentes no consumen consultas de metadatos durante el recorrido diario.

`quotaBudget`, predeterminado en 2500 unidades por ejecución, incluye reintentos. Recorrer fuentes completas puede consumir más cuota que una búsqueda incremental. No hay un tope artificial de páginas por fuente; si se agota el presupuesto o la cuota de YouTube, la ejecución queda incompleta, conserva los videos nuevos ya verificados y termina con código 2. El resumen indica que el recorrido quedó incompleto; no se guarda un cursor para reanudar. La próxima ejecución vuelve a empezar desde la primera página. Ajustar el presupuesto y la cantidad de fuentes a la cuota disponible para poder completar todos los recorridos.

Los errores temporales se reintentan hasta cuatro veces con espera creciente. Un fallo de una fuente no impide procesar las siguientes salvo agotamiento de cuota. No se publica una lista vacía por un fallo ni se incorporan videos cuyo pedido de metadatos falló.

Los commits se generan sólo cuando hay diferencias, incluyendo el resumen de ejecución. También se guarda avance parcial verificado antes de marcar la ejecución como fallida. El bot no dispara otro workflow de push con sus propios commits. Si una publicación manual se adelanta al bot, su push se rechaza: no se hace force-push ni rebase automático de listas generadas; repetir contra `main` actualizado.

## Continuidad y operación local

Los nuevos videos se agregan al final. Se conservan las duraciones, los campos manuales y el orden de los existentes. Al cambiar la duración total, se ajusta `cycleStartsAt` para conservar el mismo video y segundo correspondientes al instante de generación. Mantenerlo literalmente intacto podría cambiar el resultado del módulo que calcula la posición del ciclo.

La aplicación mantiene el reproductor en curso hasta su transición natural. El contrato actual no tiene versiones programadas: los clientes con versiones distintas, o que descargan después de cruzar el final del ciclo anterior, pueden diferir. Garantizar cambios simultáneos en todos los clientes requeriría ampliar el contrato de la app. No se modificó la aplicación.

Pruebas y validación sin clave ni cuota:

```powershell
py -3 -m unittest discover -s tests -v
py -3 scripts/sync.py --mode validate
```

Actions ejecuta:

```text
python scripts/sync.py --mode daily
python scripts/sync.py --mode daily --channel comida
python scripts/sync.py --mode maintenance
```

Sólo se ejecuta una sincronización remota a la vez. `.sync.lock` evita procesos locales simultáneos. No editar los JSON mientras corre un proceso local. Cada JSON se reemplaza atómicamente y el índice se guarda después de los archivos de canal; una interrupción entre archivos puede requerir repetir la sincronización. No hay transacción conjunta de todo el conjunto de archivos.

Los pull requests ejecutan pruebas y validación sin secretos. Las sincronizaciones con API sólo corren en `main`, usando argumentos citados y variables de entorno para las opciones manuales.

## Referencias

- [Canales, handles y playlist de subidas](https://developers.google.com/youtube/data/reference/rest/v3/channels/list).
- [Paginación de playlists](https://developers.google.com/youtube/data/reference/rest/v3/playlistItems/list).
- [Metadatos y restricciones de videos](https://developers.google.com/youtube/data/reference/rest/v3/videos).
- [Horarios de GitHub Actions](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).
