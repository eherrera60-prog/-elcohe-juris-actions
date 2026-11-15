"""
Diagnóstico temporal PostgreSQL — ELCOHE-JURIS RD 1.6.5.

Solo lectura. No registra credenciales ni modifica datos.
"""

def verificar_postgresql():
    try:
        from auth_access import connect_db

        conexion = connect_db()

        try:
            with conexion.cursor() as cursor:
                cursor.execute("SELECT 1")
                resultado = cursor.fetchone()

            if resultado and resultado[0] == 1:
                print(
                    "ELCOHE_POSTGRESQL_165: DATABASE_CONNECTED",
                    flush=True
                )
                return True

            print(
                "ELCOHE_POSTGRESQL_165: DATABASE_UNEXPECTED_RESULT",
                flush=True
            )
            return False

        finally:
            conexion.close()

    except Exception as exc:
        print(
            "ELCOHE_POSTGRESQL_165: DATABASE_CONNECTION_FAILED "
            + type(exc).__name__,
            flush=True
        )
        return False
