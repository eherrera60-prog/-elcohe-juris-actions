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
  extrae fundamentos numerados con tribunal, número, fecha y página.

Los resultados de búsqueda son `CANDIDATE`, no decisiones verificadas. La
extracción distingue `VERIFIED_SOURCE`, `VERIFIED_IDENTITY`,
`VERIFIED_PASSAGE` y `NOT_VERIFIED` en campos separados. `VERIFIED_PASSAGE`
confirma que el texto extraído aparece en la página indicada del PDF; no
certifica la autoría judicial ni que el pasaje sea ratio decidendi. No se
atribuyen criterios automáticamente.

La búsqueda SCJ usa el formulario público y su endpoint DataTables publicado
por el portal. Si el portal devuelve cero registros, la API informa que la
respuesta no incluyó candidatos; esto no acredita que la sentencia no exista.
Filtros de cámara y materia deben coincidir con las opciones vigentes del
portal. Los rangos de años se limitan a diez años por consulta.
