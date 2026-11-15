import json
import os

if (
    os.getenv("RENDER_SERVICE_NAME") == "elcohe-juris-163-test"
    and os.getenv("ELCOHE_DB_TEST_ISOLATED") == "true"
    and os.getenv("ELCOHE_PERSISTENCE_TEST_ONCE") == "true"
):
    try:
        from prueba_persistencia_165 import ejecutar_prueba
        resultado = ejecutar_prueba()
        print(
            "ELCOHE_PERSISTENCE_165_RESULT "
            + json.dumps(resultado, ensure_ascii=False),
            flush=True,
        )
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
