# Canales automáticos de ZappingTube

Toda la automatización vive en este repositorio, con Python 3.10+ (sin dependencias), YouTube Data API y GitHub Actions. No necesita servidor ni base de datos.

La carpeta local se llama `zp-static-asset`; su remoto actual es [`andrescassanaz/zp-static-assets`](https://github.com/andrescassanaz/zp-static-assets). El índice y `channels/*.json` siguen el contrato actual de Android. No se modificó la aplicación.

## Activación en GitHub

1. Publicar estos archivos en la rama predeterminada `main` del repositorio.
2. Abrir **Settings → Secrets and variables → Actions → New repository secret**. Crear **`YOUTUBE_API_KEY`** con una clave de un proyecto que tenga YouTube Data API v3 habilitada. Restringir la clave a esa API. No pegarla en archivos, issues, comandos con argumentos visibles ni logs. La automatización lee exclusivamente la variable de entorno entregada por Actions; no busca archivos de claves locales.
3. Habilitar Actions y permitir escritura de `contents` para el workflow (incluida cualquier política de la organización o protección de rama que impida commits del bot).
4. Configurar fuentes reales en `sources.json`. Abrir **Actions → Sincronizar canales → Run workflow**, `mode=daily`, para comprobar la primera ejecución. El push de la configuración también dispara la sincronización.

También se puede crear el secreto con GitHub CLI, que solicita su valor sin incluirlo en los argumentos:

```powershell
gh secret set YOUTUBE_API_KEY --repo andrescassanaz/zp-static-assets
```

Los horarios son diarios a las **06:17 de Argentina** y semanales los domingos a las **07:47 de Argentina** (09:17 y 10:47 UTC). GitHub puede demorar tareas programadas. En repositorios públicos, GitHub deshabilita los horarios tras 60 días sin actividad; si ocurre, reactivarlos desde Actions. Los commits del bot no disparan otra ejecución de push, evitando un bucle.

## Agregar un canal

Agregar un objeto a `channels` de **`sources.json`**. Cada fuente ocupa un elemento independiente, que puede escribirse en una línea:

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

El siguiente push/sync consulta las fuentes, registra los candidatos y publica `channels/cocina-automatica.json` cuando hay al menos un video aprobado. Añade la entrada a `channels.json` con el siguiente número libre. `number` es opcional: puede fijarse si no está ocupado. `id` debe ser estable y usar letras minúsculas, números y guiones (hasta 64 caracteres).

No es necesario crear manualmente ni el archivo de reproducción ni la entrada del índice. Un canal sin aprobados permanece en el catálogo, sin publicar una lista vacía que Android rechazaría.

Fuentes admitidas:

| Entrada | Interpretación |
| --- | --- |
| ID de 24 caracteres que empieza con `UC` | Canal: se consulta su playlist de subidas |
| `@handle` | Canal: se resuelve una vez y se conserva su ID estable |
| ID de video de 11 caracteres | Video individual |
| ID de más de 11 caracteres | Playlist, salvo un ID de canal válido |
| `playlist:ID` / `video:ID` | Tipo explícito, útil para playlists cortas o ambiguas |

No admite URLs completas. Los videos individuales se consultan una vez y luego participan del mantenimiento. `active: false` pausa toda automatización de ese canal sin borrarlo del índice ni modificar sus archivos.

Los canales que ya existían están configurados para preservar sus listas y revisar disponibilidad semanalmente. `comida` tiene las 20 fuentes confirmadas del archivo de referencia `Script extraer IDs/canales.txt`: 8 handles y 12 playlists. Las fuentes de los demás canales quedan como `[]`; completar esas listas para habilitar su descubrimiento. El sistema no puede deducir con fiabilidad las fuentes originales a partir de listas de IDs.

`playbackMode: live` admite una única fuente de video individual: no busca ni inventa sustitutos para una emisión que termina. El JSON de un directo conserva un solo ID, sin duración ni origen de ciclo. La fuente debe tener una emisión en curso; también aplica audio español salvo `music: true`.

## Archivos y contrato público

| Archivo | Uso |
| --- | --- |
| `sources.json` | Configuración, fuentes y reglas; único archivo necesario para crear canales |
| `channels.json` | Índice público actual; se conservan entradas y campos manuales |
| `channels/{id}.json` | Orden de reproducción actual, sin metadatos editoriales adicionales |
| `catalog/{id}.json` | Videos descubiertos con estado, evidencia, fuentes y decisiones |
| `sync-state.json` | IDs ya conocidos por fuente, cursores, últimas ejecuciones y publicación |
| `scripts/sync.py` | Descubrimiento, validación, curaduría, mantenimiento y exportación |
| `.github/workflows/sync.yml` | Sincronización diaria, semanal, por push y manual |
| `.github/workflows/tests.yml` | Pruebas y validación sin claves, también para pull requests |

El catálogo inicial registra como `legacy: true` los videos de las listas existentes, respetando su curaduría y duración actuales. Esto **no certifica retroactivamente** idioma o filtros que antes no se habían comprobado. El mantenimiento verifica su disponibilidad; los candidatos nuevos sí pasan todos los filtros.

## Validaciones y curaduría manual

Para videos nuevos se exige disponibilidad pública o no listada, subida procesada, inserción permitida y acceso en **todos** los países de `regionCodes`. Se excluyen restricciones de edad que impiden la experiencia embebida. Los ciclos no incorporan directos en curso ni próximos estrenos.

Las duraciones son enteros positivos compatibles con Android. Los límites son **exclusivos**, como en `extraer_ids.py`: mínimo 420 significa más de 7 minutos. El máximo predeterminado es `null` (sin máximo). Una duración ausente o no entera queda pendiente.

Para contenido no musical, sólo se acepta `snippet.defaultAudioLanguage=es` o una variante como `es-AR`. Idioma ausente queda pendiente; otro idioma se rechaza. No se usan título, subtítulos ni `defaultLanguage` como sustitutos. `music: true` exceptúa exclusivamente el idioma.

Los estados del catálogo son:

- `approved`: video apto para publicar.
- `pending`: falta evidencia, emisión futura o retención editorial.
- `rejected`: rechazo persistente, por reglas o decisión manual.
- `unavailable`: video ausente, privado, sin inserción o restringido; se reintenta posteriormente.

Para curar un video editar su objeto en `catalog/{id}.json`, agregando por ejemplo:

```json
"manualDecision": "rejected",
"note": "Fuera de la temática del canal"
```

Los valores de `manualDecision` son `approved`, `pending` y `rejected`. Las notas y campos adicionales se conservan. `pending` retiene la publicación; `approved` permite publicarlo sólo con validación técnica aprobada. Una aprobación manual no evita los controles técnicos. Cambiar `status` a `rejected` también constituye un rechazo persistente.

Si se elimina manualmente un ID del JSON de reproducción, se registra como `rejected`, `manualRemoval: true`, comparándolo con la última lista publicada. No reaparece aunque siga presente en las fuentes. No borrar el registro de catálogo ni `publishedIds` para rechazar: son la memoria que impide reincorporaciones.

Un rechazo no se levanta automáticamente, incluso si cambian las reglas. Para una restauración intencional, quitar `manualRemoval` y `manualDecision: rejected` si existen, cambiar `status` a `pending` y ejecutar una sincronización: se volverá a validar. Para un hold editorial, usar `manualDecision: pending`.

Los nuevos IDs se deduplican dentro de cada canal, incluso entre fuentes de tipos diferentes. Un mismo video puede pertenecer intencionalmente a dos canales distintos. Se conservan repeticiones y orden ya existentes; no se reescribe la curaduría heredada.

## Descubrimiento incremental y cuota

No se usa `search.list`. Se usan `channels.list`, `playlistItems.list` y `videos.list`, con costo de una unidad por solicitud. Las consultas de metadatos se agrupan en lotes de hasta 50 IDs y se cachean dentro de la ejecución. Los reintentos también cuentan contra `quotaBudget` (2500 unidades predeterminadas por ejecución). Este presupuesto no conoce el consumo de otros clientes del mismo proyecto de Google: reservar cuota para ellos. El límite diario real lo impone YouTube.

Para canales, se consulta la cabeza de subidas y se detiene al encontrar una página cuyos IDs ya se conocen. Si el historial inicial o una ráfaga de nuevas subidas supera `maxPagesPerSource` (20 predeterminado, mínimo 2), se guarda un cursor de backfill y se retoma en las ejecuciones siguientes. Cada ejecución vuelve a consultar la cabeza antes de continuar el backfill.

**Las playlists arbitrarias no ofrecen un filtro de cambios ni garantizan que los nuevos videos se agreguen al principio.** Se consulta la cabeza y se recorre un bloque acotado y circular del resto, conservando un cursor entre ejecuciones. Se leen IDs históricos para detectar inserciones al final o en el medio, pero únicamente se consultan metadatos de IDs nuevos. Una playlist grande puede tardar varias ejecuciones en completar un recorrido; no se promete detección inmediata de cambios de posición. La paginación de YouTube tampoco constituye una instantánea transaccional: movimientos concurrentes pueden requerir otro barrido.

La ejecución diaria reevalúa pendientes y no disponibles. La semanal revalida todos los videos no rechazados, incluyendo directos, y retira los no disponibles de listas con otros videos aptos. Si un canal quedaría vacío, mantiene su último JSON válido y registra `publication: held_empty`: evita perder toda la programación, aunque la señal en YouTube pueda seguir caída. No inventa contenido de reemplazo.

Cada página avanza sólo después de completar su consulta de metadatos. Los errores de una fuente no borran las otras. Los HTTP temporales y errores de red se reintentan hasta cuatro veces con espera creciente. Un HTTP 400 reinicia el cursor de esa fuente en el próximo intento para recuperarse de tokens inválidos, manteniendo los IDs conocidos. Cuota agotada conserva el avance y termina con código 2. Las listas ya publicadas no se vacían por fallos de red ni por respuestas malformadas.

## Continuidad televisiva

Se conservan el orden, los datos y las duraciones manuales de todos los videos que continúan en la lista; los nuevos se agregan al final. No se mezcla al azar ni se reinicia el canal en la hora de ejecución.

La app calcula la posición con `elapsed % duración_total`. Por eso mantener literalmente el mismo `cycleStartsAt` al cambiar la duración total puede provocar saltos. Al publicar una lista diferente, el sincronizador calcula un origen equivalente que conserva el **mismo video y segundo** que correspondían al instante de generación. Con incorporaciones al final, ese origen coincide con el comienzo del ciclo anterior en curso. Si se retiran videos anteriores al actual, se compensa su duración. Si se retira el video actual, se alinea el siguiente video conservado con su final nominal; la app ya cargada mantiene el reproductor hasta su transición natural.

El JSON no tiene versiones programadas ni una fecha de activación futura. La garantía de fase corresponde al instante de generación: clientes que descargan distintas versiones después de completar un ciclo pueden diferir, y una propagación de GitHub que cruza el final del ciclo anterior también puede producir una transición distinta. Garantizar cambios simultáneos para todos los clientes requeriría ampliar el contrato de la aplicación. El sistema conserva el contrato solicitado y aprovecha que la app no recarga el reproductor en curso al recibir actualizaciones.

## Operación y verificación local

No hace falta una clave para probar, validar o registrar una lista existente:

```powershell
py -3 -m unittest discover -s tests -v
py -3 scripts/sync.py --mode validate
py -3 scripts/sync.py --mode bootstrap
```

`bootstrap` registra únicamente la base heredada y el estado, sin consultar YouTube ni modificar los JSON públicos. Se ejecutó para las listas actuales. `validate` no escribe datos y verifica configuración, índices, listas y catálogos antes de llamar a la API.

Actions ejecuta:

```text
python scripts/sync.py --mode daily
python scripts/sync.py --mode maintenance
python scripts/sync.py --mode daily --channel comida
```

La opción `--channel` sirve también para mantenimiento. El selector manual se pasa mediante variables de entorno y argumentos citados; no se interpola como código shell. No se ejecuta la sincronización con secretos en pull requests ni en ramas distintas de `main`.

Sólo hay una ejecución remota a la vez. Localmente `.sync.lock` bloquea procesos simultáneos; si el proceso fue terminado abruptamente, quitar ese archivo únicamente después de comprobar que ya no hay otro sincronizador activo. Cada JSON se escribe con temporal y reemplazo atómico, y el índice se guarda después de los archivos de canal. Una ejecución interrumpida entre archivos puede requerir repetir la sincronización; no hay una transacción entre todo el catálogo y el estado. No ejecutar ediciones manuales simultáneamente con un proceso local.

El workflow commitea sólo los assets y el estado cuando `git diff` detecta cambios, incluido avance parcial útil; luego marca como fallida una sincronización incompleta para hacerla visible. `lastRun`, los timestamps de revisión y los cursores también son cambios reales, aunque los JSON de reproducción permanezcan idénticos. No se regeneran archivos sólo por su formato.

Si alguien publica mientras el bot consulta YouTube, el push normal se rechaza: no se hace force-push ni rebase automático que pueda pisar decisiones manuales. Repetir la ejecución contra `main` actualizado. La copia anterior publicada sigue disponible.

## Referencias

- [Canales, handles y playlist de subidas](https://developers.google.com/youtube/data/reference/rest/v3/channels/list).
- [Listado y paginación de playlists](https://developers.google.com/youtube/data/reference/rest/v3/playlistItems/list).
- [Metadatos de video, idioma de audio, inserción y restricciones](https://developers.google.com/youtube/data/reference/rest/v3/videos).
- [Horarios y límites de eventos de GitHub Actions](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).
- [Secretos de GitHub Actions](https://docs.github.com/en/actions/security-for-github-actions/security-guides/using-secrets-in-github-actions).
