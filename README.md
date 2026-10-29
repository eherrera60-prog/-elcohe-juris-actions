# ELCOHE-JURIS RD

Backend de acciones para búsqueda y verificación jurisprudencial de la Suprema
Corte de Justicia (SCJ) y el Tribunal Constitucional (TC) de la República
Dominicana.

## Búsqueda y verificación

- `GET /v1/tc/search`: localiza enlaces candidatos en el portal oficial del TC.
- `GET /v1/scj/search`: consulta el buscador oficial de decisiones de la SCJ.
- `GET /v1/scj/search-plan`: prepara términos de búsqueda; no consulta el portal
  ni devuelve resultados.
- `POST /v1/verify-official-pdf`: valida la extracción e identidad declarada de
  un PDF oficial.
- `POST /v1/extract-foundations`: revalida el PDF oficial y su identidad, y
  extrae fundamentos numerados con los metadatos y páginas que puedan
  comprobarse en el documento.

Los resultados de búsqueda son `CANDIDATE`, no decisiones verificadas. La
extracción distingue `VERIFIED_SOURCE`, `VERIFIED_IDENTITY`,
`VERIFIED_PASSAGE` y `NOT_VERIFIED` en campos separados. `VERIFIED_PASSAGE`
confirma que el texto extraído aparece en la página indicada del PDF; no
certifica la autoría judicial ni que el pasaje sea ratio decidendi. No se
atribuyen criterios automáticamente.

Las búsquedas usan estos estados:

- `SEARCH_PLAN_ONLY`: solo se preparó una consulta; no se contactó el portal.
- `CANDIDATES_FOUND`: se localizaron enlaces o registros oficiales candidatos,
  aún pendientes de verificar.
- `NO_RESULTS_CONFIRMED`: la fuente devolvió explícitamente cero resultados para
  la consulta y los filtros indicados; no demuestra que la sentencia no exista.
- `EXTRACTION_FAILED`: hubo respuesta, pero no se pudo validar la consulta, sus
  filtros o la estructura de resultados.
- `ACCESS_FAILED`: no se completó el acceso a la fuente oficial.

La búsqueda SCJ usa el formulario público y el endpoint DataTables publicado
por el portal. Sala, materia y año se traducen a los valores vigentes del
formulario; los rangos de años se consultan por año y se limitan a diez años.
El archivo institucional alternativo no aplica esos filtros y su estado se
informa aparte. En el TC, la consulta confirma el término y el año reflejados
por la página oficial. Los números, fechas, tribunales y enlaces se devuelven
solo cuando aparecen en los datos de la fuente. Un HTTP 200 por sí solo no
confirma una búsqueda satisfactoria.

Los resultados SCJ incluyen `source_url` y `decision_number` como entradas
directas para `verifyOfficialPdf`; este número se normaliza al identificador
jurisprudencial cuando el portal lo presenta con prefijos como `NÚM.`. La
verificación sigue requiriendo PDF de dominio oficial, extracción suficiente,
tribunal e identificador confirmados en el contenido documental. Los resultados
del buscador continúan siendo candidatos hasta superar esos controles.
