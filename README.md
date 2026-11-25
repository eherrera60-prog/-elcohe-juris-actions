# ELCOHE-JURIS RD

Backend de acciones para búsqueda y verificación jurisprudencial de la Suprema
Corte de Justicia (SCJ) y el Tribunal Constitucional (TC) de la República
Dominicana.

## Portal de acceso

La ruta `/` redirige al portal `/portal`. Desde allí se inicia la sesión OAuth
en `/auth/login`; la validación funcional está disponible en
`/portal/validation` únicamente con una sesión activa. El portal no expone las
operaciones `/v1/*` ni sustituye sus controles de autorización.

## Búsqueda y verificación

- `GET /v1/tc/search`: busca en un índice externo limitado a dominios oficiales
  del TC y consulta aparte referencias previamente verificadas.
- `POST /v1/registry/tc-references`: verifica y conserva una referencia PDF del
  TC; es una operación administrativa y no se publica en OpenAPI.
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
- `INDEX_SEARCH_NOT_CONFIGURED` / `INDEX_SEARCH_ACCESS_FAILED`: la búsqueda
  indexada no tiene clave configurada o el proveedor no completó la consulta.
- `NO_INDEX_RESULTS`: el índice no devolvió candidatos TC; no confirma que no
  existan sentencias.

La búsqueda SCJ usa el formulario público y el endpoint DataTables publicado
por el portal. Sala, materia y año se traducen a los valores vigentes del
formulario; los rangos de años se consultan por año y se limitan a diez años.
El archivo institucional alternativo no aplica esos filtros y su estado se
informa aparte. En el TC, la ruta oficial principal se consulta una sola vez en
tiempo real; los resultados oficiales, indexados y previamente verificados se
informan por separado. Tras HTTP 406 no se llaman las rutas alternativas ya
rechazadas.

La búsqueda TC usa el buscador público oficial del Tribunal Constitucional en
tiempo real; no requiere cuenta, clave ni pago. Si recibe HTTP 406 no reintenta
ni llama las rutas alternativas ya rechazadas. Si la búsqueda oficial no
localiza candidatos, la consulta no depende de un índice de pago: el estado de
la fuente oficial se conserva y se informa sin afirmar que no existan sentencias.

Brave Search se conserva como integración opcional, pero está desactivada por
defecto. Solo se consulta si se configura expresamente
`BRAVE_SEARCH_ENABLED=true` y `BRAVE_SEARCH_API_KEY`. La API de Brave Search
envía la consulta al índice externo, restringida a
`tribunalconstitucional.gob.do` y
`tribunalsitestorage.blob.core.windows.net`; además, descarta cualquier
resultado cuyo URL no pertenezca a esos dominios. Esta integración puede estar
sujeta a condiciones y tarifas del proveedor; no es necesaria para la búsqueda
oficial.

`/v1/tc/search` separa los resultados oficiales en tiempo real, los indexados y
los documentos previamente verificados. Los resultados de búsqueda son
candidatos, no documentos verificados. Para conservar uno, envía el URL PDF
oficial y el número esperado a `POST /v1/registry/tc-references` como
administrador. Si el resultado es una ficha HTML, usa primero
`POST /v1/resolve-official-pdf` para localizar el enlace PDF candidato. El
servicio vuelve a descargar el PDF y verifica origen,
integridad SHA-256, tribunal, número, extracción de texto y cantidad de páginas
antes de guardarlo en el registro persistente existente. La clave única por
tribunal, número y huella evita duplicados; la operación deja la revisión
humana pendiente y nunca certifica automáticamente la ratio decidendi.

Los resultados SCJ incluyen `source_url` y `decision_number` como entradas
directas para `verifyOfficialPdf`; este número se normaliza al identificador
jurisprudencial cuando el portal lo presenta con prefijos como `NÚM.`. La
verificación sigue requiriendo PDF de dominio oficial, extracción suficiente,
tribunal e identificador confirmados en el contenido documental. Los resultados
del buscador continúan siendo candidatos hasta superar esos controles.

Las solicitudes a fuentes oficiales reintentan hasta dos veces solo ante
timeouts, fallos de transporte y respuestas transitorias (408, 425, 429 y
5xx seleccionados). Los estados 403 y 404 no se reintentan ni se eluden.
`/v1/resolve-official-pdf` prioriza enlaces PDF cuyo nombre coincide con el
número solicitado; el nombre del archivo solo ayuda a localizar candidatos y
nunca verifica identidad. `/v1/extract-foundations` incluye pasajes
dispositivos extraídos con páginas de origen, pero su efecto jurídico y la
ratio decidendi requieren revisión humana. La extracción está limitada a 30
fundamentos por solicitud; los diagnósticos informan cuántos se detectaron y
cuántos se devolvieron.

## Acceso individual y OAuth para ChatGPT

Todos los endpoints `/v1/*`, incluidos los administrativos, requieren un token
de acceso Auth0 firmado mediante RS256. Las operaciones jurisprudenciales
requieren además la audiencia `https://elcohe-juris-164-final`, el
permiso `access:jurisprudence` y un usuario con estado `ACTIVE` en PostgreSQL.
En Auth0, asigna ese permiso al rol `ELCOHE-PROFESIONAL`; la API valida el claim
estándar `permissions` y no necesita emitir un claim personalizado de roles.
`/health` es público; `/openapi.json` publica el
esquema de las operaciones no administrativas. La interfaz interactiva de
documentación está desactivada y las operaciones administrativas se excluyen
del esquema público. No se almacenan tokens ni contraseñas.

Configura Auth0 de la siguiente manera:

1. Crea una API con identificador
   `https://elcohe-juris-164-final` y firma **RS256**. Habilita RBAC
   y la inclusión de permisos en el token de acceso; asigna
   `access:jurisprudence` al rol de API `ELCOHE-PROFESIONAL`.
2. No hace falta incluir roles en el token: Auth0 añade al claim estándar
   `permissions` los permisos efectivos del rol. El dominio del tenant se
   configura como `AUTH0_DOMAIN` (solo el dominio, o su URL HTTPS); el emisor y
   la URL JWKS `/.well-known/jwks.json` se derivan de ese valor.
3. En el servicio de pruebas existente, configura
   `AUTH0_DOMAIN`, `DATABASE_URL` y `ELCOHE_BOOTSTRAP_ADMIN_SUBJECTS` sin crear
   otro servicio mediante un manifiesto de Render. `ELCOHE_BOOTSTRAP` se admite
   como alternativa únicamente cuando la variable plural no está definida.
   `DATABASE_URL` debe apuntar a PostgreSQL con TLS. Para conservar datos, usa
   PostgreSQL persistente.
4. Obtén el `sub` exacto del usuario administrador desde un token validado y
   colócalo en `ELCOHE_BOOTSTRAP_ADMIN_SUBJECTS` o, si usas la alternativa,
   `ELCOHE_BOOTSTRAP`. Admite varios valores separados por coma. Solo esas
   identidades pueden administrar accesos. Para retirar un administrador
   bootstrap, elimina su `sub` de la configuración del servicio.
   Una fila explícita `SUSPENDED` o `REVOKED` en PostgreSQL bloquea también
   a ese administrador bootstrap.
5. En el editor de GPT, configura **Authentication: OAuth**, el Client ID y el
   Client Secret de Auth0, la URL de autorización
   `https://<AUTH0_DOMAIN>/authorize?audience=https%3A%2F%2Felcohe-juris-164-final`,
   el endpoint `https://<AUTH0_DOMAIN>/oauth/token` y el scope
   `access:jurisprudence`.
   Registra en Auth0 el callback URL que muestre ChatGPT. No copies el Client
   Secret ni el URL de base de datos al repositorio.
6. El administrador autoriza a cada usuario con `PUT /v1/admin/users/status`
   y `{ "subject": "<sub-del-proveedor>", "status": "ACTIVE" }`. Los estados
   `SUSPENDED` y `REVOKED` bloquean el siguiente acceso; `ACTIVE` permite
   reautorizar. `GET /v1/admin/users` y `GET /v1/admin/audit` son solo para
   administradores. La asignación de permisos administrativos no se delega a
   usuarios administrables.
7. Para comprobar solo la conectividad PostgreSQL, un administrador puede
   llamar `GET /v1/admin/database/health`. Ejecuta únicamente `SELECT 1`;
   no crea tablas ni consulta o modifica registros. `/health` comprueba solo
   que la aplicación responde.

### Conexión MCP con ChatGPT

El servidor MCP remoto está disponible en `https://<dominio-publico>/mcp`.
Publica `search_scj_decisions` para ejecutar la consulta oficial (a diferencia
de `prepare_scj_search`, que solo la prepara), `recover_official_pdf` para
localizar y recuperar el PDF candidato desde una ficha oficial,
`extract_decision_passages` para fundamentos y dispositivo con páginas y
contexto, y `propose_ratio_decidendi` para presentar una hipótesis basada en
pasajes literales que siempre queda pendiente de revisión jurídica humana.
`get_verified_decisions` busca referencias cuyo PDF verificado está persistido
en PostgreSQL, y `get_verified_decision_document` recupera esos bytes tras
comprobar nuevamente su SHA-256. La búsqueda, localización, recuperación,
verificación de fuente, identidad y pasaje se reportan como estados distintos.
`verify_official_document` solo verifica: no persiste el archivo. Un
administrador puede registrar explícitamente el documento con
`register_verified_decision`; la operación reutiliza el registro PostgreSQL
existente, es idempotente para la misma identidad y SHA-256, y devuelve el
identificador que se usa en `get_verified_decision_document`. La ausencia de
coincidencias se informa como `NO_VERIFIED_DOCUMENTS`; los errores de conexión
o consulta a PostgreSQL se informan como error, no como una lista vacía.

Para MCP, conserva la API `https://elcohe-juris-164-final` para las rutas `/v1`
y crea una API Auth0 cuyo identificador sea exactamente el recurso MCP,
incluido `/mcp` (por ejemplo,
`https://elcohe-juris-163-test.onrender.com/mcp`). Habilita RBAC y la inclusión
de permisos en el token, y concede `access:jurisprudence` a esa API. En Auth0,
activa **Resource Parameter Compatibility Profile** en **Tenant Settings >
Advanced** para que el `resource` enviado por ChatGPT se convierta en la
audiencia del token. No añadas un parámetro `audience` distinto a la URL de
autorización MCP: Auth0 da precedencia a `audience` sobre `resource`.

Usa una aplicación OAuth de usuario que permita **Authorization Code** y
refresh tokens, no una aplicación exclusivamente Machine-to-Machine. En
ChatGPT configura la autorización como
`https://<AUTH0_DOMAIN>/authorize` (sin query `audience`), el token endpoint
`https://<AUTH0_DOMAIN>/oauth/token`, el scope
`access:jurisprudence offline_access` y el método de autenticación del token
`client_secret_post`. En Auth0, agrega a **Allowed Callback URLs** el callback
de producción exacto mostrado en la configuración del conector. Con la
metadata Auth0 actual, que anuncia `authorization_response_iss_parameter_supported`,
ChatGPT usa `https://chatgpt.com/connector_platform_oauth_redirect`; si la
pantalla del conector muestra otra URL, registra esa URL exacta. La metadata
OIDC debe anunciar `S256` y `offline_access`, y la aplicación debe poder emitir
refresh tokens. ChatGPT renueva el acceso mediante Auth0; ELCOHE-JURIS solo
valida access tokens y no almacena refresh tokens ni secretos.

El `sub` del usuario debe estar autorizado como `ACTIVE` en ELCOHE-JURIS.
Prueba el descubrimiento y una llamada autenticada en un entorno de
preproducción antes de publicar la app en el workspace.

### Prueba con una aplicación Machine-to-Machine

El API acepta tokens M2M y tokens de usuario mediante el mismo bearer JWT; no
requiere una ruta de autenticación M2M en la aplicación. En Auth0, crea o usa
una aplicación **Machine to Machine**, autorízala para la API con audiencia
`https://elcohe-juris-164-final` y concédele
`access:jurisprudence`. La API debe tener RS256, RBAC y **Add Permissions in
the Access Token** habilitados. La prueba requiere que el servicio tenga
configurados `AUTH0_DOMAIN` y `DATABASE_URL`.

Para obtener y usar el token, introduce `AUTH0_CLIENT_ID` y
`AUTH0_CLIENT_SECRET` localmente (por ejemplo, con el gestor de secretos del
entorno o una lectura oculta del terminal); no los guardes en el repositorio ni
los compartas. Solicita un token con `grant_type=client_credentials` y la
audiencia indicada y envíalo como `Authorization: Bearer <access_token>` a
`GET /v1/scj/search-plan?q=prueba`. No imprimas ni compartas el token.

El `sub` M2M (normalmente `<client_id>@clients`) debe además figurar como
`ACTIVE` en `elcohe_authorized_users`; un administrador ya autorizado puede
activarlo con `PUT /v1/admin/users/status`. Sin esa fila, un JWT válido con el
permiso correcto recibe 403. Un token inválido o sin token recibe 401. Una
respuesta 200 confirma la integración completa contra Auth0 y PostgreSQL.
Autorizar el `sub` M2M no lo agrega a
`ELCOHE_BOOTSTRAP_ADMIN_SUBJECTS`: la identidad conserva rol de usuario y no
puede consultar `/v1/admin/database/health` ni administrar otras identidades.
Ese endpoint de salud es solo para una identidad administradora autorizada.

Un token ausente o inválido produce HTTP 401; la falta del permiso o de la
autorización individual produce HTTP 403. El esquema de autorización crea las
tablas de acceso y auditoría al primer uso. Los cambios de estado se guardan
en la misma transacción; cada solicitud
jurisprudencial comprueba de nuevo el estado persistido, por lo que la
revocación no depende de la expiración del token OAuth. El proveedor autentica
la identidad; la base de datos de ELCOHE-JURIS controla la autorización del
servicio. Esta separación permite sustituir el proveedor OIDC o conectar
futuras suscripciones/entitlements al mismo `sub` sin guardar credenciales de
identidad en ELCOHE-JURIS; la facturación y las suscripciones no se activan en
esta versión.
