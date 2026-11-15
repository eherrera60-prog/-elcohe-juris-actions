import json
import os

servicio = os.getenv("RENDER_SERVICE_NAME")
aislamiento = os.getenv("ELCOHE_DB_TEST_ISOLATED")
autorizacion = os.getenv("ELCOHE_PERSISTENCE_TEST_ONCE")

condiciones = {
    "servicio_correcto": servicio == "elcohe-juris-163-test",
    "aislamiento_autorizado": aislamiento == "true",
    "ejecucion_autorizada": autorizacion == "true",
    "database_url_presente": bool(os.getenv("DATABASE_URL")),
}

print(
    "ELCOHE_PERSISTENCE_165_DIAGNOSTICO "
    + json.dumps(condiciones),
    flush=True,
)

if all(condiciones.values()):
    try:
        from prueba_persistencia_165 import ejecutar_prueba
        ejecutar_prueba()
    except Exception as exc:
        print(
            "ELCOHE_PERSISTENCE_165_ERROR "
            + type(exc).__name__,
            flush=True,
        )
else:
    print(
        "ELCOHE_PERSISTENCE_165: EJECUCION_NO_AUTORIZADA",
        flush=True,
    )
