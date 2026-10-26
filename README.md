# Canales de ZappingTube

El proceso tiene una configuración, un script y un catálogo publicado:

```text
sources.json → scripts/sync.py → channels.json + channels/<id>.json → app
                                ↓
                       Reporte en los logs
```

Python 3.10 o posterior, biblioteca estándar, YouTube Data API y GitHub Actions.
El proceso es **stateless**: cada ejecución empieza desde cero. No hay cursores,
checkpoints, base de datos, caché persistente de metadatos ni historial de
candidatos descartados. La última lista publicada es contenido del producto y
se lee para acumular videos, evitar duplicados y conservar una programación válida.

## Agregar o modificar un canal

Editar solamente `sources.json`. Ejemplo de configuración (los números deben
estar libres en tu catálogo):

```json
{
  "version": 1,
  "quotaBudget": 2500,
  "limits": {
    "videosPerDay": 50,
    "maxPagesPerSource": 100
  },
  "defaults": {
    "minDurationSeconds": 420,
    "maxDurationSeconds": null,
    "maxAgeDays": null,
    "music": false,
    "regionCodes": ["AR"]
  },
  "channels": [
    {
      "id": "programacion",
      "number": 1,
      "name": "Programación",
      "updateFrequency": "daily",
      "updateMode": "replace",
      "playbackMode": "cycle",
      "videoOrder": "random",
      "sources": ["@midudev", "@HolaMundoDev"],
      "rules": {"maxAgeDays": 5}
    }
  ]
}
```

- `id`: estable, letras minúsculas, números y guiones, hasta 64 caracteres.
- `name` y `number`: se administran en esta configuración y se reflejan en el índice.
  `number` es opcional: si se omite se conserva el existente o se asigna uno libre.
- `updateFrequency`: `daily` por defecto o `weekly`. La elegís vos; no se deduce
  del nombre, género ni de los datos actuales de los canales.
- `updateMode`: `append` por defecto conserva la biblioteca y agrega videos;
  `replace` reconstruye la lista desde las fuentes y exige una revisión completa.
- `playbackMode`: `cycle` por defecto, o `live` con una única fuente de video.
  Cambiar el ID de una emisión en `sources` reemplaza el directo publicado después
  de verificarlo. No cambia automáticamente entre cycle y live un canal publicado.
- `videoOrder`: `random` por defecto mezcla sólo las altas en append y la lista
  reconstruida en replace. `ordered` conserva el orden de fuentes y páginas.
- `rules`: sobreescribe `defaults`. Límites de duración exclusivos, en segundos.
  `maxAgeDays` usa días de 24 horas desde el inicio UTC de la ejecución; ausente o
  null permite cualquier antigüedad. En append afecta sólo a las altas; en replace
  también retira contenido que salió de la ventana tras completar la revisión.
- `limits`: permite sobreescribir por canal los dos límites globales del ejemplo.
- `excludedVideoIds`: lista opcional de IDs que no deben incorporarse y que se
  retiran de la programación. Borrar un video sólo del JSON permite su reingreso.
- `guideMode`: `schedule` o `continuous`; música usa continuous por defecto.
- `active: false`: pausa las consultas y conserva el canal publicado.
  **Eliminar un canal de sources.json lo retira del índice y elimina su archivo
  en la próxima actualización**, incluso si ya no queda ningún canal para consultar.

No se editan manualmente `channels.json` ni sus listas para administrar canales.
Los canales históricos con `sources: []` conservan su biblioteca; no descubren
altas hasta configurar fuentes. No se reconstruye contenido histórico que no esté
representado en las fuentes.

Fuentes admitidas, un elemento por fuente:

| Formato | Interpretación |
| --- | --- |
| `@handle` o `UC...` de 24 caracteres | Canal de YouTube: playlist de subidas |
| ID de video de 11 caracteres o `video:ID` | Video individual |
| ID largo de playlist o `playlist:ID` | Playlist, que puede estar ordenada manualmente |

Usar IDs o handles, no URLs completas.

## Cuándo consulta YouTube

| Disparador | Alcance |
| --- | --- |
| Push a main que modifica sources.json | Sólo canales cuya configuración cambió |
| Cambio de defaults, limits u otra opción global | Todos los canales activos |
| Todos los días a las 06:17 de Argentina | Canales daily |
| Domingos a las 07:47 de Argentina | Canales weekly y después mantenimiento de todos los activos |
| Ejecución manual | Un canal o todos los activos, sin filtrar frecuencia |
| Cambios en scripts, documentación o catálogo generado | Pruebas/validación; ninguna sincronización con API por ese push |

Los horarios de Actions son aproximados. La comparación de configuración usa el
commit anterior de Git; no guarda estado de ejecuciones. Si no puede leer esa
versión, falla antes de consultar YouTube. Cambiar sólo un nombre también selecciona
ese canal, por simplicidad. Reordenar canales sin cambiar sus opciones no los selecciona.
Las bajas se aplican al índice aunque no requieran consultas.

Antes de consultar, el mismo script valida JSON, IDs, números, opciones, reglas y
archivos de reproducción. No confirma por esta vía que una fuente exista en YouTube.
La API key se lee únicamente de `YOUTUBE_API_KEY`; en Actions se configura como secret.
No se imprimen claves ni URLs de requests.

## Consultas acotadas

**Canal de YouTube con ventana:** máximo `maxAgeDays × videosPerDay` candidatos por
fuente, en páginas de hasta 50, y nunca más de `maxPagesPerSource` páginas. Se cuentan
candidatos antes de filtrar o deduplicar; no videos aprobados. Tres días con factor
50 permiten hasta 150 candidatos y tres páginas de listado, además de resolver el
canal y consultar los metadatos necesarios. El factor es una estimación configurable,
no un máximo garantizado de publicaciones diarias de YouTube.

La playlist de subidas se recorre de reciente a antiguo. Si una página entera tiene
fechas conocidas anteriores al corte, termina ese recorrido sin pedir el resto del
historial. Es una optimización basada en ese orden de las subidas; no constituye una
búsqueda exhaustiva de publicaciones retrospectivas ni una garantía ante cambios
concurrentes de YouTube. Una fecha vieja aislada, fechas faltantes o una página mixta
no causan ese corte. También termina si YouTube no ofrece una página siguiente.
Si se alcanza el límite y todavía podría haber contenido dentro de la ventana,
registra revisión incompleta; no interpreta el límite como cobertura completa.

**Canal sin ventana y playlists:** se recorren hasta finalizar o alcanzar
`maxPagesPerSource` (100 por defecto). Las playlists no usan `días × videosPerDay`
ni se detienen por fechas viejas: pueden contener videos recientes en cualquier
posición. Sí se filtran por fecha los candidatos cuando hay maxAgeDays.

Cada página aporta IDs y, cuando está disponible, fecha de publicación. Los candidatos
fuera de ventana no pasan a la consulta de detalles. Si falta su fecha, se consulta
`videos.list` antes de decidir. Duración, restricciones, título y nombre visible del
creador se obtienen juntos en lotes de hasta 50. Sólo se reutilizan respuestas dentro
de la misma ejecución; los descartados pueden volver a consultarse en la siguiente.

El presupuesto `quotaBudget` es total por invocación e incluye reintentos, descubrimiento
y mantenimiento si se ejecutan juntos. No es un presupuesto diario del proyecto.
Se usan channels.list, playlistItems.list y videos.list; no search.list.
Cada endpoint cuesta una unidad por solicitud. Si una carga no entra en los límites,
hay que ampliar el presupuesto o acotar las fuentes. Repetir no reanuda ni garantiza
que alcance el contenido restante. Una fuente grande puede agotar el presupuesto
antes de que se procesen otras; no hay reparto persistente de turnos.

## Publicación y fallos

- Append permite publicar altas verificadas aunque otra página falle. Conserva
  los videos existentes y sus datos manuales; completa títulos y creadores faltantes.
- Replace conserva exactamente la última lista ante errores, cuota agotada o límites
  que impidan completar la revisión. La primera carga queda sin publicar hasta completarse.
- Un replace completo sin videos válidos retira el canal del índice. La configuración
  permanece y permite que vuelva cuando haya contenido admisible.
- Los videos nuevos deben estar disponibles, procesados, permitir inserción, ser
  reproducibles en las regiones configuradas y no tener restricción de edad. Los
  ciclos excluyen emisiones activas o próximas. El contenido no musical requiere
  idioma de audio declarado español; música exceptúa ese requisito.
- El mantenimiento revisa los videos publicados y retira bajas confirmadas. No vuelve
  a descubrir fuentes. Los fallos de red no se interpretan como videos eliminados.
- Se conserva el programa y segundo actuales al ajustar el origen del ciclo cuando
  es posible. El formato de reproducción y el funcionamiento de Android no cambian.

Cada JSON se escribe atómicamente y minificado; si sus bytes no cambian, no se reescribe.
El índice se publica después de las listas. No hay una transacción conjunta entre
archivos ni versiones coordinadas de descarga. Un commit contiene los cambios para GitHub.
Los conflictos de publicación fallan sin force-push; repetir con main actualizado.

El reporte sale por stdout en JSON con canales, páginas, candidatos, cuota y errores.
Actions conserva sus logs según los permisos y retención del repositorio; no son un
almacenamiento privado de secretos. No se guarda ni lee `sync-state.json`.
Una ejecución sin cambios del catálogo no genera un commit de reporte.
El lock local `.sync.lock` sólo evita procesos simultáneos y se elimina al terminar;
no guarda avance. Las ejecuciones de Actions también están serializadas.

## Comandos

Desde esta carpeta:

```powershell
py -3 scripts/sync.py --mode validate
py -3 scripts/sync.py --channel programacion
py -3 scripts/sync.py --frequency daily
py -3 scripts/sync.py --frequency weekly --maintenance
py -3 scripts/sync.py --mode maintenance --channel cocina
```

Sin --channel ni --frequency actualiza todos los activos. `--changed-since SHA`
permite seleccionar por diferencias con el commit anterior, como hace el workflow.
Los modos names, titles y presentation siguen disponibles como utilidades puntuales:
completan nombres, refrescan metadatos o normalizan presentación; presentation no usa API.
`--mode daily` sigue siendo un alias de update por compatibilidad con comandos anteriores.

Código 0: ejecución completa. Código 2: error, revisión incompleta o cuota agotada.
No se necesitan claves para validar ni ejecutar las pruebas:

```powershell
py -3 -m unittest discover -s tests -v
```

## Respaldo para una versión de Android

El catálogo canónico se publica desde este repositorio. La APK lleva una copia,
que se actualiza explícitamente al preparar una versión:

```powershell
py -3 scripts/sync.py --mode backup --app-root ../App
```

Valida y copia el índice y los archivos a `App/app/src/main/assets/catalog/`, sin
consultar YouTube, cambiar metadatos, duraciones ni horarios. Revisar y confirmar
esa copia en la rama de Android antes de compilar. No hace falta copiarla para que
las instalaciones existentes reciban cambios del catálogo remoto.

`Script cargar canal` y `Script extraer IDs` son herramientas históricas de exploración
fuera de este flujo. Su salida no se publica automáticamente. El procedimiento vigente
para administrar canales es editar sources.json y ejecutar este sincronizador.
