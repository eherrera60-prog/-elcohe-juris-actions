"""
ELCOHE-JURIS RD 1.6.5
Prueba controlada de persistencia PostgreSQL.
"""

import os
import uuid
import hashlib
from urllib.parse import urlsplit

import psycopg

HOST = "dpg-dbi0vsei0phs73ch9pdg-a"
BASE = "elcohe_juris_164_db"
PUERTO = 5432
TABLA = "elcohe_admin_persistence_test"


def ejecutar_prueba():
    if os.getenv("ELCOHE_DB_TEST_ISOLATED") != "true":
        return {"ok": False, "estado": "BLOQUEADO"}

    if os.getenv("ELCOHE_PERSISTENCE_TEST_ONCE") != "true":
        return {"ok": False, "estado": "BLOQUEADO"}

    url = os.getenv("DATABASE_URL")

    if not url:
        return {"ok": False, "estado": "SIN_DATABASE_URL"}

    destino = urlsplit(url)

    if (
        destino.hostname != HOST
        or destino.path.lstrip("/") != BASE
        or destino.port not in (None, PUERTO)
    ):
        return {"ok": False, "estado": "DESTINO_NO_AUTORIZADO"}

    identificador = str(uuid.uuid4())
    contenido = "ELCOHE-JURIS-RD-1.6.5"
    insertado = False
    recuperado = False
    eliminado = False

    def conectar():
        conexion = psycopg.connect(
            url,
            connect_timeout=8,
            sslmode="require",
        )

        with conexion.cursor() as cursor:
            cursor.execute(
                "SELECT current_database(), inet_server_port()"
            )
            base, puerto = cursor.fetchone()

        if base != BASE or puerto != PUERTO:
            conexion.close()
            raise RuntimeError("IDENTIDAD_INCORRECTA")

        return conexion

    try:
        with conectar() as conexion:
            with conexion.cursor() as cursor:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS
                    elcohe_admin_persistence_test (
                        id TEXT PRIMARY KEY,
                        contenido TEXT NOT NULL
                    )
                """)

                cursor.execute("""
                    INSERT INTO elcohe_admin_persistence_test
                    (id, contenido)
                    VALUES (%s, %s)
                """, (identificador, contenido))

            conexion.commit()
            insertado = True

        with conectar() as conexion:
            with conexion.cursor() as cursor:
                cursor.execute("""
                    SELECT contenido
                    FROM elcohe_admin_persistence_test
                    WHERE id = %s
                """, (identificador,))

                fila = cursor.fetchone()

            recuperado = (
                fila is not None
                and fila[0] == contenido
            )

    except Exception as exc:
        error = type(exc).__name__

    else:
        error = None

    finally:
        if insertado:
            try:
                with conectar() as conexion:
                    with conexion.cursor() as cursor:
                        cursor.execute("""
                            DELETE FROM elcohe_admin_persistence_test
                            WHERE id = %s
                        """, (identificador,))
                        eliminados = cursor.rowcount

                    conexion.commit()

                with conectar() as conexion:
                    with conexion.cursor() as cursor:
                        cursor.execute("""
                            SELECT COUNT(*)
                            FROM elcohe_admin_persistence_test
                            WHERE id = %s
                        """, (identificador,))
                        restantes = cursor.fetchone()[0]

                eliminado = eliminados == 1 and restantes == 0

            except Exception:
                eliminado = False

    correcto = insertado and recuperado and eliminado

    return {
        "ok": correcto,
        "estado": (
            "PERSISTENCIA_VERIFICADA"
            if correcto
            else "VERIFICACION_INCOMPLETA"
        ),
        "escritura": insertado,
        "recuperacion": recuperado,
        "eliminacion": eliminado,
        "error": error,
    }
