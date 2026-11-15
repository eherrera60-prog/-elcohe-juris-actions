
import json
import os
import uuid
from urllib.parse import urlsplit

import psycopg

HOST = "dpg-dbi0vsei0phs73ch9pdg-a"
BASE = "elcohe_juris_164_db"
PUERTO = 5432
SERVICIO = "elcohe-juris-163-test"

MARCADOR = "ELCOHE-JURIS-165-CERTIFICACION-UNICA"


def informar(**datos):
    print(
        "ELCOHE_PERSISTENCE_165_RESULT "
        + json.dumps(datos, ensure_ascii=False),
        flush=True,
    )
    return datos


def ejecutar_prueba():

    if os.getenv("RENDER_SERVICE_NAME") != SERVICIO:
        return informar(
            ok=False,
            estado="SERVICIO_NO_AUTORIZADO",
        )

    if (
        os.getenv("ELCOHE_DB_TEST_ISOLATED") != "true"
        or os.getenv("ELCOHE_PERSISTENCE_TEST_ONCE") != "true"
    ):
        return informar(
            ok=False,
            estado="AUTORIZACION_PENDIENTE",
        )

    url = os.getenv("DATABASE_URL")

    if not url:
        return informar(
            ok=False,
            estado="DATABASE_URL_AUSENTE",
        )

    destino = urlsplit(url)

    if (
        destino.hostname != HOST
        or destino.port not in (None, PUERTO)
        or destino.path.lstrip("/") != BASE
    ):
        return informar(
            ok=False,
            estado="DESTINO_NO_AUTORIZADO",
        )

    def conectar():
        conexion = psycopg.connect(
            url,
            connect_timeout=8,
            sslmode="require",
        )

        try:
            with conexion.cursor() as cursor:
                cursor.execute(
                    "SELECT current_database(), inet_server_port()"
                )
                base, puerto = cursor.fetchone()

            if base != BASE or puerto != PUERTO:
                raise RuntimeError("IDENTIDAD_POSTGRESQL_INCORRECTA")

            return conexion

        except Exception:
            conexion.close()
            raise

    identificador = "prueba:" + str(uuid.uuid4())
    contenido = "ELCOHE-JURIS-RD-1.6.5"

    escritura = False
    recuperacion = False
    eliminacion = False
    error = None
    reservado = False

    try:
        # Reserva persistente: la clave primaria impide repetir
        # esta certificación en reinicios posteriores.
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
                    ON CONFLICT (id) DO NOTHING
                    RETURNING id
                """, (MARCADOR, "EJECUCION_RESERVADA"))

                reservado = cursor.fetchone() is not None

            conexion.commit()

        if not reservado:
            return informar(
                ok=False,
                estado="CERTIFICACION_YA_RESERVADA",
            )

        # Escritura confirmada.
        with conectar() as conexion:
            with conexion.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO elcohe_admin_persistence_test
                    (id, contenido)
                    VALUES (%s, %s)
                """, (identificador, contenido))

            conexion.commit()
            escritura = True

        # Lectura desde una nueva conexión.
        with conectar() as conexion:
            with conexion.cursor() as cursor:
                cursor.execute("""
                    SELECT contenido
                    FROM elcohe_admin_persistence_test
                    WHERE id = %s
                """, (identificador,))

                fila = cursor.fetchone()

            recuperacion = (
                fila is not None
                and fila[0] == contenido
            )

    except Exception as exc:
        error = type(exc).__name__

    finally:
        if reservado:
            try:
                with conectar() as conexion:
                    with conexion.cursor() as cursor:
                        cursor.execute("""
                            DELETE FROM elcohe_admin_persistence_test
                            WHERE id = %s
                        """, (identificador,))

                    conexion.commit()

                with conectar() as conexion:
                    with conexion.cursor() as cursor:
                        cursor.execute("""
                            SELECT COUNT(*)
                            FROM elcohe_admin_persistence_test
                            WHERE id = %s
                        """, (identificador,))

                        restantes = cursor.fetchone()[0]

                eliminacion = restantes == 0

            except Exception as exc:
                eliminacion = False
                error = "LIMPIEZA_" + type(exc).__name__

    correcto = escritura and recuperacion and eliminacion

    if reservado:
        try:
            with conectar() as conexion:
                with conexion.cursor() as cursor:
                    cursor.execute("""
                        UPDATE elcohe_admin_persistence_test
                        SET contenido = %s
                        WHERE id = %s
                    """, (
                        "PERSISTENCIA_VERIFICADA"
                        if correcto
                        else "VERIFICACION_INCOMPLETA",
                        MARCADOR,
                    ))

                conexion.commit()

        except Exception as exc:
            correcto = False
            error = "MARCADOR_" + type(exc).__name__

    return informar(
        ok=correcto,
        estado=(
            "PERSISTENCIA_VERIFICADA"
            if correcto
            else "VERIFICACION_INCOMPLETA"
        ),
        escritura_confirmada=escritura,
        recuperacion_nueva_conexion=recuperacion,
        eliminacion_confirmada=eliminacion,
        error=error,
    )
